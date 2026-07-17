from typing import TypedDict, List, Dict
from langgraph.graph import StateGraph, END

# Import Agents
from agents.domain_research import DomainResearchAgent
from agents.lead_discovery import LeadDiscoveryAgent
from agents.dedup_filter import DedupFilterAgent
from agents.business_verification import BusinessVerificationAgent
from agents.contact_discovery import ContactDiscoveryAgent
from agents.decision_maker_intel import DecisionMakerIntelAgent
from agents.contact_verification import ContactVerificationAgent
from agents.data_enrichment import DataEnrichmentAgent
from agents.csv_writer_agent import WriterAgent

class PipelineState(TypedDict):
    domain: str
    search_plan: List[Dict]
    raw_leads: List[Dict]
    filtered_leads: List[Dict]
    verified_leads: List[Dict]
    enriched_leads: List[Dict]
    qa_passed: List[Dict]
    quota_met: bool
    quota: int
    
# Global instances for stateful agents if needed, but it's fine to instantiate in nodes
# DedupFilterAgent is stateful (cross_domain_ids), so a global instance is better 
# for a single graph run to remember what was added in this run.
dedup_agent = DedupFilterAgent()

def node_domain_research(state: PipelineState) -> PipelineState:
    agent = DomainResearchAgent()
    # Restrict to just the target domain
    if state["domain"] in agent.domains_map:
        agent.domains_map = {state["domain"]: agent.domains_map[state["domain"]]}
    else:
        agent.domains_map = {}
        
    plan = agent.build_search_plan()
    
    return {
        **state,
        "search_plan": plan,
        "raw_leads": [],
        "filtered_leads": [],
        "verified_leads": [],
        "enriched_leads": [],
        "qa_passed": [],
        "quota_met": False
    }

def node_lead_discovery(state: PipelineState) -> PipelineState:
    agent = LeadDiscoveryAgent()
    plan = state.get("search_plan", [])
    
    if not plan:
        return state
        
    # Pop one task to execute
    task = plan.pop(0)
    print(f"\n[node_lead_discovery] Executing search task: {task.get('search_query', '')}")
    
    new_raw = agent.discover(task)
    
    return {
        **state,
        "search_plan": plan, # updated plan
        "raw_leads": new_raw # replace raw_leads with new batch
    }

def node_dedup_filter(state: PipelineState) -> PipelineState:
    raw = state.get("raw_leads", [])
    filtered = state.get("filtered_leads", [])
    passed = state.get("qa_passed", [])
    quota = state.get("quota", 20)
    
    new_filtered = dedup_agent.filter(raw)
    
    # We need (quota - len(passed)) MORE leads.
    target = quota - len(passed)
    
    for lead in new_filtered:
        if len(filtered) < target:
            filtered.append(lead)
            dedup_agent.cross_domain_ids.add(lead.get("business_id"))
            
    quota_met = len(filtered) >= target
    
    return {
        **state,
        "filtered_leads": filtered,
        "quota_met": quota_met,
        "raw_leads": [] # Clear raw leads after processing
    }

def dedup_router(state: PipelineState) -> str:
    quota_met = state.get("quota_met", False)
    plan_exhausted = len(state.get("search_plan", [])) == 0
    
    if not quota_met and not plan_exhausted:
        return "node_lead_discovery"
    return "node_biz_verify"

def node_biz_verify(state: PipelineState) -> PipelineState:
    agent = BusinessVerificationAgent()
    filtered = state.get("filtered_leads", [])
    
    if not filtered:
        return {**state, "verified_leads": []}
        
    print(f"\n[node_biz_verify] Verifying {len(filtered)} leads...")
    verified = agent.batch_verify(filtered)
    
    return {**state, "verified_leads": verified}

def node_contact_discovery(state: PipelineState) -> PipelineState:
    agent = ContactDiscoveryAgent()
    verified = state.get("verified_leads", [])
    
    discovered = []
    print(f"\n[node_contact_discovery] Discovering contacts for {len(verified)} leads...")
    for lead in verified:
        discovered.append(agent.discover_contacts(lead))
        
    return {**state, "verified_leads": discovered}

def node_decision_maker_intel(state: PipelineState) -> PipelineState:
    agent = DecisionMakerIntelAgent()
    leads = state.get("verified_leads", [])
    
    intel_leads = []
    print(f"\n[node_decision_maker_intel] Enhancing decision makers for {len(leads)} leads...")
    for lead in leads:
        intel_leads.append(agent.discover_contacts(lead))  # merges with Agent 5's findings (never resets them)
        
    return {**state, "verified_leads": intel_leads}

def node_contact_verify(state: PipelineState) -> PipelineState:
    agent = ContactVerificationAgent()
    leads = state.get("verified_leads", [])
    
    verified_contacts = []
    print(f"\n[node_contact_verify] Normalizing contacts for {len(leads)} leads...")
    for lead in leads:
        verified_contacts.append(agent.verify_lead_contacts(lead))
        
    return {**state, "verified_leads": verified_contacts}

def node_enrichment(state: PipelineState) -> PipelineState:
    agent = DataEnrichmentAgent()
    leads = state.get("verified_leads", [])
    
    enriched = []
    print(f"\n[node_enrichment] Enriching data for {len(leads)} leads via Gemini...")
    for lead in leads:
        enriched.append(agent.enrich(lead))
        
    return {**state, "enriched_leads": enriched}

def node_qa_gate(state: PipelineState) -> PipelineState:
    enriched = state.get("enriched_leads", [])
    passed = state.get("qa_passed", [])
    quota = state.get("quota", 20)
    
    print(f"\n[node_qa_gate] QA Gate checking {len(enriched)} leads...")
    
    for lead in enriched:
        # Check QA criteria
        score = lead.get("lead_quality_score", 5)
        phone_missing = lead.get("phone_missing", False)
        
        if not phone_missing and score > 0:
            if len(passed) < quota:
                passed.append(lead)
        else:
            reason = "phone missing" if phone_missing else "quality score 0"
            print(f"QA REJECT [{lead.get('raw_name')}] reason: {reason}")
            
    quota_met = len(passed) >= quota
    
    return {
        **state,
        "qa_passed": passed,
        "quota_met": quota_met,
        "enriched_leads": [], # Clear intermediate processing pipeline
        "filtered_leads": [], # Clear the batch so dedup can gather new ones if we loop back
        "verified_leads": []
    }

def qa_router(state: PipelineState) -> str:
    # if failed leads > 0 AND quota not met -> loop back to lead_discovery to fetch replacements; else -> writer
    quota_met = state.get("quota_met", False)
    plan_exhausted = len(state.get("search_plan", [])) == 0
    
    if not quota_met and not plan_exhausted:
        print("\n[QA Router] Quota not met. Looping back to discovery for replacements...")
        return "node_lead_discovery"
    
    if not quota_met and plan_exhausted:
        print("\n[QA Router] Quota not met, but search plan exhausted. Moving to writer.")
        
    return "node_writer"

def node_writer(state: PipelineState) -> PipelineState:
    agent = WriterAgent()
    passed = state.get("qa_passed", [])
    
    print(f"\n[node_writer] Writing {len(passed)} QA-passed leads to DB and CSV...")
    agent.write_batch(passed)
    
    return state

def build_graph():
    workflow = StateGraph(PipelineState)
    
    workflow.add_node("node_domain_research", node_domain_research)
    workflow.add_node("node_lead_discovery", node_lead_discovery)
    workflow.add_node("node_dedup_filter", node_dedup_filter)
    workflow.add_node("node_biz_verify", node_biz_verify)
    workflow.add_node("node_contact_discovery", node_contact_discovery)
    workflow.add_node("node_contact_verify", node_contact_verify)
    workflow.add_node("node_decision_maker_intel", node_decision_maker_intel)
    workflow.add_node("node_enrichment", node_enrichment)
    workflow.add_node("node_qa_gate", node_qa_gate)
    workflow.add_node("node_writer", node_writer)
    
    workflow.set_entry_point("node_domain_research")
    
    workflow.add_edge("node_domain_research", "node_lead_discovery")
    workflow.add_edge("node_lead_discovery", "node_dedup_filter")
    
    workflow.add_conditional_edges("node_dedup_filter", dedup_router, {
        "node_lead_discovery": "node_lead_discovery",
        "node_biz_verify": "node_biz_verify"
    })
    
    workflow.add_edge("node_biz_verify", "node_contact_discovery")
    workflow.add_edge("node_contact_discovery", "node_decision_maker_intel")
    workflow.add_edge("node_decision_maker_intel", "node_contact_verify")
    workflow.add_edge("node_contact_verify", "node_enrichment")
    workflow.add_edge("node_enrichment", "node_qa_gate")
    
    workflow.add_conditional_edges("node_qa_gate", qa_router, {
        "node_lead_discovery": "node_lead_discovery",
        "node_writer": "node_writer"
    })
    
    workflow.add_edge("node_writer", END)
    
    return workflow.compile()
