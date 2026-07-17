import os

from utils.envtools import load_env
from pipeline.graph import build_graph

load_env()


def run_domain(domain: str):
    print(f"\n{'=' * 50}\nStarting pipeline for domain: {domain}\n{'=' * 50}")

    quota_str = os.getenv("TARGET_LEADS_PER_RUN", "10")
    try:
        quota = int(quota_str)
    except ValueError:
        quota = 10

    initial_state = {
        "domain": domain,
        "search_plan": [],
        "raw_leads": [],
        "filtered_leads": [],
        "verified_leads": [],
        "enriched_leads": [],
        "qa_passed": [],
        "quota_met": False,
        "quota": quota
    }

    graph = build_graph()

    # The discovery loop revisits nodes once per search task, so the graph
    # needs far more steps than LangGraph's default recursion limit of 25.
    recursion_limit = int(os.getenv("GRAPH_RECURSION_LIMIT", "300") or "300")

    try:
        final_state = graph.invoke(initial_state, config={"recursion_limit": recursion_limit})

        passed_count = len(final_state.get("qa_passed", []))
        print(f"\nPipeline finished for {domain}. Successfully wrote {passed_count} leads.")
        return passed_count
    except Exception as e:
        print(f"\nPipeline failed for {domain}: {e}")
        return 0
