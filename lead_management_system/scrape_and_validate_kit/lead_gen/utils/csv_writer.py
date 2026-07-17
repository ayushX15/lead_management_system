import os
import csv

from utils.envtools import output_dir

# Strict export schema
EXPORT_COLUMNS = [
    "business name",
    "business address",
    "city",
    "domain",
    "c1 name",
    "c1 designation",
    "c1 number",
    "c1 email",
    "c2 name",
    "c2 designation",
    "c2 number",
    "c2 email",
    "c3 name",
    "c3 designation",
    "c3 number",
    "c3 email",
    "ivr risk score",
    "data scraped date"
]


class CSVWriter:
    def __init__(self):
        # One shared output folder (GEN_OUTPUT_DIR) for CSVs + run state
        self.output_dir = output_dir()

    def init_csv(self, domain: str) -> str:
        """Creates the per-domain CSV with a header row if it does not exist."""
        safe_domain = domain.lower().replace(" ", "_").replace("/", "_")
        filename = f"{safe_domain}_leads.csv"
        filepath = os.path.join(self.output_dir, filename)

        if not os.path.exists(filepath):
            with open(filepath, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(EXPORT_COLUMNS)

        return filepath

    def append(self, lead, domain: str):
        """Opens the domain CSV in append mode and writes one row."""
        filepath = self.init_csv(domain)

        if hasattr(lead, "model_dump"):
            lead = lead.model_dump()

        # IVR risk: a verified direct mobile is low-risk; a business/IVR line
        # is high-risk for cold calling. (contact_source is set by the
        # DecisionMakerIntel agent.)
        ivr_score = lead.get("ivr_risk_score", "")
        if ivr_score == "":
            ivr_score = {"direct_mobile": "2", "ivr_risk": "7"}.get(
                lead.get("contact_source", ""), "")

        mapping = {
            "business name": lead.get("raw_name", lead.get("business_name", "")),
            "business address": lead.get("raw_address", lead.get("business_address", "")),
            "city": lead.get("city", ""),
            "domain": lead.get("domain", domain),
            "c1 name": lead.get("contact_person_1_name", ""),
            "c1 designation": lead.get("contact_person_1_designation", ""),
            "c1 number": lead.get("contact_person_1_phone", ""),
            "c1 email": lead.get("contact_person_1_email", ""),
            "c2 name": lead.get("contact_person_2_name", ""),
            "c2 designation": lead.get("contact_person_2_designation", ""),
            "c2 number": lead.get("contact_person_2_phone", ""),
            "c2 email": lead.get("contact_person_2_email", ""),
            "c3 name": lead.get("contact_person_3_name", ""),
            "c3 designation": lead.get("contact_person_3_designation", ""),
            "c3 number": lead.get("contact_person_3_phone", ""),
            "c3 email": lead.get("contact_person_3_email", ""),
            "ivr risk score": ivr_score,
            "data scraped date": lead.get("date_scraped", lead.get("scrape_date", ""))
        }

        row = [mapping[col] if mapping[col] is not None else "" for col in EXPORT_COLUMNS]

        with open(filepath, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(row)
