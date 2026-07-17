import os
import re
from typing import Dict, Optional

class ContactVerificationAgent:
    def __init__(self):
        self.email_regex = re.compile(r'^[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}$')
        # Region-aware phone handling (PHONE_REGION=IN keeps the original
        # strict Indian-mobile behaviour; any other region uses generic rules)
        self.region = os.getenv("PHONE_REGION", "IN").upper()
        self.country_code = os.getenv("PHONE_COUNTRY_CODE", "+91")

    def normalize_phone(self, raw: str) -> Optional[str]:
        if not raw:
            return None

        # Strip everything except digits and plus
        clean = re.sub(r'[^\d+]', '', str(raw))
        digits_only = re.sub(r'\D', '', clean)

        if self.region == "IN":
            # Already correct format +91XXXXXXXXXX
            if re.match(r'^\+91[6-9]\d{9}$', clean):
                return clean
            # Extract the rightmost 10 digits and require a mobile prefix
            if len(digits_only) >= 10:
                last_10 = digits_only[-10:]
                if last_10[0] in '6789':
                    return f"+91{last_10}"
            return None

        # Generic international handling: keep an existing +country prefix,
        # otherwise prepend the configured PHONE_COUNTRY_CODE
        if clean.startswith('+') and 8 <= len(digits_only) <= 15:
            return clean
        if 8 <= len(digits_only) <= 15:
            return f"{self.country_code}{digits_only}"
        return None

    def validate_email(self, raw: str) -> Optional[str]:
        if not raw:
            return None
            
        if self.email_regex.match(str(raw).strip()):
            return str(raw).strip().lower()
            
        return None

    def verify_lead_contacts(self, lead: Dict) -> Dict:
        # 1. Normalize raw phone
        if "raw_phone" in lead:
            lead["raw_phone"] = self.normalize_phone(lead.get("raw_phone"))
            
        # 2. Normalize and validate the 3 contacts
        valid_contacts = []
        for i in range(1, 4):
            phone_key = f'contact_person_{i}_phone'
            email_key = f'contact_person_{i}_email'
            name_key = f'contact_person_{i}_name'
            desig_key = f'contact_person_{i}_designation'
            type_key = f'contact_person_{i}_contact_type'
            
            # Extract
            c_phone = lead.get(phone_key)
            c_email = lead.get(email_key)
            c_name = lead.get(name_key)
            c_desig = lead.get(desig_key)
            c_type = lead.get(type_key)
            
            # Clean
            c_phone = self.normalize_phone(c_phone)
            c_email = self.validate_email(c_email)
            
            if c_phone or c_email or c_name:
                valid_contacts.append({
                    "name": c_name or "",
                    "designation": c_desig or "",
                    "phone": c_phone or "",
                    "email": c_email or "",
                    "type": c_type or ""
                })
                
        # 3. Deduplicate
        seen_phones = set()
        seen_names = set()
        unique_contacts = []
        
        for c in valid_contacts:
            is_dup = False
            
            if c["phone"]:
                if c["phone"] in seen_phones:
                    is_dup = True
                else:
                    seen_phones.add(c["phone"])
                    
            if c["name"] and c["name"].lower().strip() != "owner/manager":
                name_lower = c["name"].lower().strip()
                if name_lower in seen_names:
                    is_dup = True
                else:
                    seen_names.add(name_lower)
                    
            if not is_dup:
                unique_contacts.append(c)
                
        # 4. Promote raw_phone if contact 1 has no phone
        if len(unique_contacts) > 0 and not unique_contacts[0]["phone"]:
            if lead.get("raw_phone"):
                unique_contacts[0]["phone"] = lead["raw_phone"]
                # Also deduplicate if it was already in list?
        elif len(unique_contacts) == 0:
            if lead.get("raw_phone"):
                unique_contacts.append({
                    "name": "Business Contact",
                    "designation": "",
                    "phone": lead["raw_phone"],
                    "email": "",
                    "type": ""
                })

        # 5. Write back up to 3 unique contacts
        for i in range(1, 4):
            phone_key = f'contact_person_{i}_phone'
            email_key = f'contact_person_{i}_email'
            name_key = f'contact_person_{i}_name'
            desig_key = f'contact_person_{i}_designation'
            type_key = f'contact_person_{i}_contact_type'
            
            if i - 1 < len(unique_contacts):
                c = unique_contacts[i-1]
                lead[name_key] = c["name"]
                lead[desig_key] = c["designation"]
                lead[phone_key] = c["phone"]
                lead[email_key] = c["email"]
                if c["type"]:
                    lead[type_key] = c["type"]
                elif type_key in lead:
                    del lead[type_key] # Cleanup if empty
            else:
                # Clear remainder
                lead[name_key] = ""
                lead[desig_key] = ""
                lead[phone_key] = ""
                lead[email_key] = ""
                if type_key in lead:
                    del lead[type_key]

        # 6. Mark phone_missing if no phones exist
        has_any_phone = any([
            lead.get('contact_person_1_phone'),
            lead.get('contact_person_2_phone'),
            lead.get('contact_person_3_phone'),
            lead.get('raw_phone')
        ])
        
        lead['phone_missing'] = not has_any_phone
        
        return lead

