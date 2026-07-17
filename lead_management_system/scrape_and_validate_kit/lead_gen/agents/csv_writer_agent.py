import os
import json
from typing import List, Dict

from utils.csv_writer import CSVWriter
from utils.envtools import output_dir
from utils.identity import business_id
from models.lead import Lead


class WriterAgent:
    """Final gate: soft-QA the lead, reject duplicates, append to the domain CSV."""

    def __init__(self):
        self.csv_writer = CSVWriter()

        # History enforces strictly unique leads across multiple runs.
        # Same file + same id function as DedupFilterAgent.
        self.history_file = os.path.join(output_dir(), "history.json")
        self.history = set()
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.history = set(json.load(f))
            except Exception:
                self.history = set()

    def _save_history(self):
        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump(list(self.history), f, indent=2)

    def write(self, lead: Dict) -> bool:
        raw_name = lead.get("raw_name", lead.get("business_name", ""))
        city = lead.get("city", "")
        domain = lead.get("domain", lead.get("subcategory", ""))

        lead_id = business_id(raw_name, city, domain)

        # Uniqueness check (safety net - dedup normally catches this earlier)
        if lead_id in self.history:
            print(f"DUPLICATE DETECTED: [{domain}] {raw_name} - {city} already exists in a previous run.")
            return False

        # Soft QA: warn (never reject) on suspicious contact data
        for warning in Lead.from_pipeline(lead).warnings():
            print(f"QA WARN [{raw_name}]: {warning}")

        try:
            self.csv_writer.append(lead, domain)
        except Exception as e:
            print(f"CSV write failed: {e}")
            return False

        self.history.add(lead_id)
        self._save_history()

        print(f"WRITTEN [{domain}] {raw_name} - {city}")
        return True

    def write_batch(self, leads: List[Dict]) -> int:
        success_count = 0
        domains_written = set()

        for lead in leads:
            if self.write(lead):
                success_count += 1
                domains_written.add(lead.get("domain", lead.get("subcategory", "unknown")))

        for domain in domains_written:
            print(f"Batch complete: {success_count} leads written to {domain}_leads.csv")

        if not domains_written:
            print("Batch complete: 0 unique leads written (all were duplicates and rejected).")

        return success_count
