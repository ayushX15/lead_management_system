import os
from typing import Dict
from utils.llm import GeminiClient

class DataEnrichmentAgent:
    def __init__(self):
        self.llm = GeminiClient()

    def enrich(self, lead: Dict) -> Dict:
        # Prevent double billing
        if lead.get('lead_quality_score') is not None and lead.get('lead_quality_score') != "":
            return lead
            
        # Extract first 200 chars of description if available
        raw_desc = lead.get("raw_description", "") or lead.get("description", "")
        if raw_desc and len(str(raw_desc)) > 200:
            raw_desc = str(raw_desc)[:200] + "..."
            
        # Build token-minimal payload
        compact_dict = {
            "business_name": lead.get("raw_name") or lead.get("business_name") or "",
            "subcategory": lead.get("subcategory") or lead.get("domain") or "",
            "city": lead.get("city", ""),
            "review_count": lead.get("review_count", ""),
            "rating": lead.get("rating", ""),
            "contact_person_1_name": lead.get("contact_person_1_name", ""),
            "raw_description": raw_desc
        }
        
        # Log rough token estimate (1 token ~= 4 chars)
        payload_str = str(compact_dict)
        est_tokens = len(payload_str) // 4
        print(f"Gemini call: ~{est_tokens} input tokens")
        
        try:
            result = self.llm.enrich_lead(compact_dict)
            
            # Map back to lead
            if result.get("size_class"):
                lead["size_class"] = result["size_class"]
                
            if result.get("decision_maker_title"):
                # Only overwrite if currently empty
                current_title = lead.get("contact_person_1_designation", "")
                if not current_title or current_title.strip() == "":
                    lead["contact_person_1_designation"] = result["decision_maker_title"]
                    
            if result.get("lead_quality_score"):
                lead["lead_quality_score"] = int(result["lead_quality_score"])
            else:
                lead["lead_quality_score"] = 5
                
        except Exception as e:
            print(f"Warning: DataEnrichmentAgent API failure after retries. {e}")
            lead["lead_quality_score"] = 5
            
        return lead

