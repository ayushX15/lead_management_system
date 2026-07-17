import os
import json
import random
from typing import List, Dict

from utils.envtools import load_env


class DomainResearchAgent:
    """Builds the search plan: one task per (domain, subcategory, city)."""

    def __init__(self, config_path: str = None):
        load_env()

        # Config path is anchored to this file, never the working directory,
        # so the agent works no matter where Python was launched from.
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "domains.json")

        self.domains_map = {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for domain_obj in data.get("domains", []):
                self.domains_map[domain_obj["name"]] = domain_obj["subcategories"]
        except FileNotFoundError:
            print(f"ERROR: domain config not found at {config_path}. "
                  "No searches can run without it - add your domains/subcategories there.")
        except Exception as e:
            print(f"ERROR: failed to load domains from {config_path}: {e}")

        # Target cities from .env (comma-separated string -> list)
        cities_str = os.getenv("TARGET_CITIES", "")
        self.cities = [c.strip() for c in cities_str.split(",") if c.strip()]
        if not self.cities:
            print("WARNING: TARGET_CITIES not set in .env - the search plan will be empty.")

        # Optional query suffix (e.g. "Delhi NCR") to bias results to a region
        self.region_suffix = os.getenv("TARGET_REGION_SUFFIX", "").strip()

        # Optional TARGET_DOMAINS filter over the config
        target_domains_str = os.getenv("TARGET_DOMAINS", "")
        if target_domains_str:
            target_domains = [d.strip() for d in target_domains_str.split(",") if d.strip()]
            unknown = [d for d in target_domains if d not in self.domains_map]
            if unknown:
                print(f"WARNING: TARGET_DOMAINS entries missing from config/domains.json: {unknown}")
            self.domains_map = {k: v for k, v in self.domains_map.items() if k in target_domains}

    def build_search_plan(self) -> List[Dict[str, str]]:
        search_plan = []

        for domain, subcategories in self.domains_map.items():
            for subcategory in subcategories:
                for city in self.cities:
                    search_query = f'"{subcategory}" {city}'
                    if self.region_suffix:
                        search_query += f" {self.region_suffix}"
                    search_plan.append({
                        "domain": domain,
                        "subcategory": subcategory,
                        "city": city,
                        "search_query": search_query
                    })

        # Shuffle so runs do not always start with the same city/domain combo
        random.shuffle(search_plan)

        print(f"{len(search_plan)} search tasks generated across "
              f"{len(self.domains_map)} domains and {len(self.cities)} cities")

        return search_plan
