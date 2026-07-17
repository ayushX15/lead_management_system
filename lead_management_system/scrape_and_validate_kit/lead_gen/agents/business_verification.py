from typing import List, Dict

class BusinessVerificationAgent:
    def verify(self, raw_lead: Dict) -> bool:
        name = raw_lead.get("raw_name", "Unknown Business")
        source = raw_lead.get("source", "")
        
        # Helper to get all text values for closed check
        all_text = " ".join(str(v).lower() for v in raw_lead.values() if v)
        
        # Check 2 - Not closed
        if 'permanently closed' in all_text or 'closed permanently' in all_text:
            print(f"REJECT [{name}] reason: marked as permanently closed")
            return False
            
        # Check 3 - Has contact signal
        phone = str(raw_lead.get("raw_phone") or "").strip()
        website = str(raw_lead.get("raw_website") or "").strip()
        if not phone and not website:
            print(f"REJECT [{name}] reason: no contact signal (phone and website missing)")
            return False
            
        # Checks 1 & 4 - Google Maps specific signals. Review/rating data is
        # not reliably present in scrape results, so these FLAG only - the
        # contact-discovery and QA gates downstream do the hard rejection.
        if "Google Maps" in source:
            try:
                rating = float(raw_lead.get("rating") or 0)
            except (TypeError, ValueError):
                rating = 0.0
            if 0 < rating < 3.0:
                print(f"FLAG [{name}] low Google rating ({rating}) - review before outreach.")
                
        # Check 5 - Size proxy / Generic names (does not reject, just flags)
        domain = raw_lead.get("domain", "")
        name_lower = name.lower()
        if domain == "real_estate" and ('residency' in name_lower or 'villa' in name_lower):
            pass # explicitly keep
        elif len(name_lower.split()) <= 1:
            print(f"FLAG [{name}] Generic single-word name detected, manual review suggested.")

        return True
        
    def batch_verify(self, leads: List[Dict]) -> List[Dict]:
        verified = []
        for lead in leads:
            if self.verify(lead):
                verified.append(lead)
        return verified

