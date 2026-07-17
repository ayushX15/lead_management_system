import os
import json
from typing import List, Dict

from utils.envtools import load_env, output_dir
from utils.identity import business_id


class DedupFilterAgent:
    """
    Rejects leads already generated in a previous run (history.json) or earlier
    in the current run (cross_domain_ids), BEFORE any paid enrichment happens.
    Identity comes from utils.identity.business_id - the same function the
    writer uses, so the two can never disagree.
    """

    def __init__(self):
        load_env()
        self.cross_domain_ids = set()

        self.history_file = os.path.join(output_dir(), "history.json")
        self.history = set()
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.history = set(json.load(f))
            except Exception:
                self.history = set()

        # All target domains, for cross-domain duplicate detection
        target_domains_str = os.getenv(
            "TARGET_DOMAINS",
            "real_estate,salon_spa,financial_services,educational_institutes")
        self.all_domains = [d.strip() for d in target_domains_str.split(",") if d.strip()]

    def exists_in_history(self, lead_id: str) -> bool:
        return lead_id in self.history or lead_id in self.cross_domain_ids

    def filter(self, raw_leads: List[Dict]) -> List[Dict]:
        filtered_leads = []
        for lead in raw_leads:
            name = lead.get("raw_name", "")
            city = lead.get("city", "")
            domain = lead.get("domain", "")

            lead_id = business_id(name, city, domain)
            lead["business_id"] = lead_id

            # 1. Already generated in a previous run?
            if self.exists_in_history(lead_id):
                print(f"SKIP [{lead_id[:8]}] {name} - already generated in a previous run.")
                continue

            # 2. Same business already captured under another domain?
            cross_domain_duplicate = False
            for d in self.all_domains:
                if d == domain:
                    continue
                if self.exists_in_history(business_id(name, city, d)):
                    cross_domain_duplicate = True
                    break

            if cross_domain_duplicate:
                print(f"SKIP [{lead_id[:8]}] {name} - already generated (cross-domain)")
                continue

            filtered_leads.append(lead)

        return filtered_leads
