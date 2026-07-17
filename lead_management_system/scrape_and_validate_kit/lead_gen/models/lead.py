"""
Typed record for an exported lead row.

Used by WriterAgent as a soft QA check right before the CSV write: it never
rejects a lead, it only reports warnings for suspicious contact data. Phone
expectations follow PHONE_REGION (default IN) instead of the old hardcoded
Indian-only regex, so the same code works on international datasets.
"""

import os
import re
from typing import List, Dict

from pydantic import BaseModel

EMAIL_RE = re.compile(r'^[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}$')
IN_PHONE_RE = re.compile(r'^(\+91)?[6-9]\d{9}$')
GENERIC_PHONE_RE = re.compile(r'^\+?\d{8,15}$')


class Lead(BaseModel):
    business_name: str = ""
    business_address: str = ""
    city: str = ""
    domain: str = ""
    contacts: List[Dict] = []

    @classmethod
    def from_pipeline(cls, d: dict) -> "Lead":
        contacts = []
        for i in range(1, 4):
            c = {
                "name": d.get(f"contact_person_{i}_name") or "",
                "designation": d.get(f"contact_person_{i}_designation") or "",
                "phone": d.get(f"contact_person_{i}_phone") or "",
                "email": d.get(f"contact_person_{i}_email") or "",
            }
            if any(c.values()):
                contacts.append(c)
        return cls(
            business_name=d.get("raw_name") or d.get("business_name") or "",
            business_address=d.get("raw_address") or d.get("business_address") or "",
            city=d.get("city") or "",
            domain=d.get("domain") or d.get("subcategory") or "",
            contacts=contacts,
        )

    def warnings(self) -> List[str]:
        region = os.getenv("PHONE_REGION", "IN").upper()
        phone_re = IN_PHONE_RE if region == "IN" else GENERIC_PHONE_RE

        out = []
        if not self.business_name:
            out.append("missing business name")

        has_phone = False
        for idx, c in enumerate(self.contacts, 1):
            digits = re.sub(r'[^\d+]', '', c.get("phone") or "")
            if digits:
                has_phone = True
                if not phone_re.match(digits):
                    out.append(f"contact {idx} phone looks invalid for region {region}: {c['phone']}")
            email = (c.get("email") or "").strip()
            if email and not EMAIL_RE.match(email):
                out.append(f"contact {idx} email looks invalid: {email}")

        if not has_phone:
            out.append("no phone number on any contact")
        return out
