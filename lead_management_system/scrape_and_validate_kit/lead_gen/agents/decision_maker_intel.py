import os
import re
import time
import random
import urllib.parse
from typing import Dict, List
import httpx
from bs4 import BeautifulSoup

# Playwright is optional. Install it (pip install playwright && playwright
# install chromium) to enable the browser-based steps (IndiaMART, LinkedIn,
# Google mining, WhatsApp); without it those steps are skipped automatically.
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_INSTALLED = True
except ImportError:
    PLAYWRIGHT_INSTALLED = False

    class PlaywrightTimeout(Exception):
        pass


from utils.envtools import load_env

load_env()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36"
]

# Placeholder names that a real decision maker may safely overwrite
GENERIC_NAMES = {"", "website contact", "jd contact person", "whatsapp business",
                 "business contact", "indiamart seller", "owner/manager"}
_CONF_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "HIGHEST": 3, "VERIFIED_GOVT": 4}


class DecisionMakerIntelAgent:
    def __init__(self):
        self.playwright_available = (PLAYWRIGHT_INSTALLED
                                     and os.getenv("PLAYWRIGHT_ENABLED", "1") != "0")
        self.serpapi_key = os.getenv("SERPAPI_KEY", "")
        self.hunter_api_key = os.getenv("HUNTER_API_KEY", "")
        self.apollo_api_key = os.getenv("APOLLO_API_KEY", "")

    def _random_sleep(self):
        delay = random.uniform(1.0, 3.0)
        time.sleep(delay)

    def _get_headers(self):
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    def _is_mobile(self, phone: str) -> bool:
        clean = re.sub(r'\D', '', phone)
        if len(clean) >= 10 and clean[-10] in '6789':
            return True
        return False

    def discover_contacts(self, lead: Dict) -> Dict:
        # Ensure contact fields exist WITHOUT wiping what ContactDiscoveryAgent
        # already found (the old reset here destroyed the previous agent's work).
        for i in range(1, 4):
            for suffix in ("name", "designation", "phone", "email"):
                lead.setdefault(f'contact_person_{i}_{suffix}', "")
        lead.setdefault('contact_confidence_score', "LOW")

        name = lead.get("raw_name", "")
        city = lead.get("city", "Delhi NCR")
        website = lead.get("raw_website", "")

        if not name:
            return lead

        # Phase A: Identity Resolution
        decision_makers, confidence = self._resolve_identity(name, city)

        lead['decision_maker_verified'] = confidence in ["HIGH", "HIGHEST", "VERIFIED_GOVT"]

        # Merge identities: upgrade only empty/placeholder names so real
        # contacts found by the previous agent are preserved.
        for idx, dm in enumerate(decision_makers[:3], 1):
            current = str(lead.get(f'contact_person_{idx}_name', "")).strip().lower()
            if current in GENERIC_NAMES:
                lead[f'contact_person_{idx}_name'] = dm.get("name", "")
                lead[f'contact_person_{idx}_designation'] = dm.get("title", "")

        # Never downgrade a confidence score set by an earlier agent
        prev_conf = str(lead.get('contact_confidence_score', "LOW"))
        if _CONF_RANK.get(confidence, 0) >= _CONF_RANK.get(prev_conf, 0):
            lead['contact_confidence_score'] = confidence
        
        # Phase B: Direct Contact Resolution
        contacts_found = self._resolve_contact(name, city, decision_makers, website)
        
        # Default contact_source (business/IVR line) unless already known
        lead.setdefault('contact_source', "ivr_risk")
        
        # Merge Phase B contacts (phones) into the decision makers or append them
        for idx, contact in enumerate(contacts_found):
            if idx < 3:
                current_idx = idx + 1
                if lead[f'contact_person_{current_idx}_name'] == "" and contact.get("name"):
                    lead[f'contact_person_{current_idx}_name'] = contact.get("name")
                    lead[f'contact_person_{current_idx}_designation'] = contact.get("title", "Owner")
                
                # Only set phone/email if not already set
                phone_val = contact.get("phone") or contact.get("apollo_phone") or ""
                email_val = contact.get("email") or contact.get("apollo_email") or ""
                
                if not lead[f'contact_person_{current_idx}_phone'] and phone_val:
                    lead[f'contact_person_{current_idx}_phone'] = phone_val
                    
                    # Update contact_source if this phone is a direct mobile
                    if contact.get("source") in ["indiamart", "apollo"]:
                        # Always ensure the direct mobile goes to contact_person_1_phone if possible
                        if current_idx == 1:
                            lead['contact_source'] = "direct_mobile"
                    
                if not lead[f'contact_person_{current_idx}_email'] and email_val:
                    lead[f'contact_person_{current_idx}_email'] = email_val
                    
                if contact.get("contact_type"):
                    lead[f'contact_person_{current_idx}_contact_type'] = contact.get("contact_type")
                    
        # Final pass: If contact 1 has a phone from direct source, ensure contact_source is direct_mobile
        # (Already handled above, but this guarantees the flag is set appropriately)
        return lead
        
    def _resolve_contact(self, name: str, city: str, decision_makers: List[Dict] = None, website: str = ""):
        contacts = []
        if decision_makers is None:
            decision_makers = []
            
        # Step B1: IndiaMART seller profile
        im_contacts = self._indiamart_search(name, city, decision_makers)
        if im_contacts:
            contacts.extend(im_contacts)
            
        # Step B2: JustDial contact person
        if not contacts:
            jd_contacts = self._justdial_search(name, city)
            if jd_contacts:
                contacts.extend(jd_contacts)
                
        # We only run B3/B4 if we have a decision maker name from Phase A
        if decision_makers:
            for dm in decision_makers:
                dm_name = dm.get("name")
                if not dm_name: continue
                
                # Check if we already found this person's info in B1/B2
                already_found = any(c.get("name") == dm_name for c in contacts)
                if already_found: continue
                
                # Step B3: LinkedIn profile -> contact info tab
                li_contact = self._linkedin_contact_search(dm_name, name)
                if li_contact:
                    contacts.extend(li_contact)
                    continue
                    
                # Step B4: Hunter.io email finder
                if website and self.hunter_api_key:
                    hunter_contact = self._hunter_email_search(dm_name, website)
                    if hunter_contact:
                        contacts.extend(hunter_contact)
                        continue
                        
                # Step B5: Apollo.io free tier (Sparingly)
                if self.apollo_api_key:
                    apollo_contact = self._apollo_search(dm_name, name)
                    if apollo_contact:
                        contacts.extend(apollo_contact)
                        continue
                        
                # Step B6: Google Search - direct number mining
                google_contact = self._google_number_mining(dm_name, name, website)
                if google_contact:
                    contacts.extend(google_contact)
                    continue
                    
        # Step B7: Last resort - WhatsApp Business number from Google Maps
        if not any(c.get("phone") for c in contacts):
            wa_contact = self._whatsapp_maps_search(name, city)
            if wa_contact:
                contacts.extend(wa_contact)
                        
        return contacts

    def _whatsapp_maps_search(self, business_name: str, city: str):
        contacts = []
        if not self.playwright_available:
            return contacts
            
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=random.choice(USER_AGENTS))
                
                query = f"{business_name} {city}"
                page.goto(f"https://www.google.com/maps/search/{urllib.parse.quote(query)}")
                self._random_sleep()
                
                # Check if page has wa.me links or WhatsApp API links
                page_html = page.content()
                
                # Look for wa.me/91xxxxxxxxxx or api.whatsapp.com/send?phone=91xxxxxxxxxx
                wa_match = re.search(r'(wa\.me/|api\.whatsapp\.com/send\?phone=)\+?91([6-9]\d{9})', page_html)
                if wa_match:
                    clean_phone = wa_match.group(2)
                    contacts.append({
                        "name": "WhatsApp Business",
                        "phone": clean_phone,
                        "title": "Owner (WhatsApp)",
                        "contact_type": "whatsapp"
                    })
                    
                browser.close()
        except Exception as e:
            print(f"WhatsApp maps search failed: {e}")
            
        return contacts

    def _google_number_mining(self, dm_name: str, business_name: str, website: str):
        contacts = []
        if not self.playwright_available:
            return contacts
            
        queries = [
            f"{dm_name} {business_name} contact mobile"
        ]
        
        if website:
            domain = website.replace("http://", "").replace("https://", "").replace("www.", "").split("/")[0]
            if domain:
                queries.append(f'"{dm_name}" site:{domain}')
                
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=random.choice(USER_AGENTS))
                
                for query in queries:
                    if len(contacts) > 0: break
                    
                    search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
                    page.goto(search_url)
                    self._random_sleep()
                    
                    # Extract text directly from search results snippet
                    page_text = page.inner_text("body")
                    
                    # Match Indian mobiles
                    phone_match = re.search(r'(\+?91[\-\s]?)?0?[6-9]\d{2,4}[\-\s]?\d{3,4}[\-\s]?\d{3,4}', page_text)
                    if phone_match:
                        raw_phone = phone_match.group(0).strip()
                        clean_phone = re.sub(r'\D', '', raw_phone)
                        if len(clean_phone) >= 10:
                            contacts.append({
                                "name": dm_name,
                                "phone": clean_phone[-10:],
                                "title": "Google Mined"
                            })
                            break
                            
                browser.close()
        except Exception as e:
            print(f"Google number mining failed: {e}")
            
        return contacts

    def _apollo_search(self, dm_name: str, business_name: str):
        contacts = []
        try:
            parts = dm_name.split(" ")
            first_name = parts[0]
            last_name = parts[-1] if len(parts) > 1 else ""
            
            url = "https://api.apollo.io/v1/people/match"
            payload = {
                "api_key": self.apollo_api_key,
                "first_name": first_name,
                "last_name": last_name,
                "organization_name": business_name
            }
            
            resp = httpx.post(url, json=payload, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json().get("person", {})
                if data:
                    email = data.get("email")
                    # Apollo sometimes returns an array of phone numbers
                    phone = ""
                    for p in data.get("phone_numbers", []):
                        if p.get("type") in ["mobile", "work"]:
                            phone = re.sub(r'\D', '', p.get("raw_number", ""))
                            if len(phone) >= 10:
                                phone = phone[-10:]
                                break
                    
                    if email or phone:
                        contacts.append({
                            "name": dm_name,
                            "apollo_email": email or "",
                            "apollo_phone": phone,
                            "title": "Apollo Verified",
                            "source": "apollo"
                        })
        except Exception as e:
            print(f"Apollo API search failed: {e}")
            
        return contacts

    def _hunter_email_search(self, dm_name: str, website: str):
        import json
        import datetime
        contacts = []
        try:
            # Clean domain
            domain = website.replace("http://", "").replace("https://", "").replace("www.", "").split("/")[0]
            if not domain or "." not in domain:
                return contacts
                
            # Monthly usage tracker: anchored to the lead_gen folder, not the CWD
            logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
            usage_file = os.path.join(logs_dir, "hunter_usage.json")
            os.makedirs(logs_dir, exist_ok=True)
            
            current_month = datetime.datetime.now().strftime("%Y-%m")
            usage_data = {}
            if os.path.exists(usage_file):
                try:
                    with open(usage_file, 'r') as f:
                        usage_data = json.load(f)
                except Exception:
                    pass
                    
            monthly_usage = usage_data.get(current_month, 0)
            if monthly_usage >= 23:
                print("Hunter.io monthly limit reached (23 calls). Skipping.")
                return contacts
                
            # Split name
            parts = dm_name.split(" ")
            first_name = parts[0]
            last_name = parts[-1] if len(parts) > 1 else ""
            
            url = "https://api.hunter.io/v2/email-finder"
            params = {
                "domain": domain,
                "first_name": first_name,
                "last_name": last_name,
                "api_key": self.hunter_api_key
            }
            
            resp = httpx.get(url, params=params, timeout=10.0)
            if resp.status_code == 200:
                # Increment and save usage
                usage_data[current_month] = monthly_usage + 1
                with open(usage_file, 'w') as f:
                    json.dump(usage_data, f)
                    
                data = resp.json()
                email = data.get("data", {}).get("email")
                score = data.get("data", {}).get("score", 0)
                
                if email:
                    contacts.append({
                        "name": dm_name,
                        "email": email,
                        "title": "Hunter Verified",
                        "contact_confidence": f"HUNTER_{score}"
                    })
        except Exception as e:
            print(f"Hunter API search failed: {e}")
            
        return contacts

    def _linkedin_contact_search(self, dm_name: str, business_name: str):
        contacts = []
        try:
            if not self.playwright_available:
                return contacts
                
            query = f"{dm_name} {business_name}"
            # Google search to find the LinkedIn profile
            search_url = f"https://www.google.com/search?q=site:linkedin.com/in+{urllib.parse.quote(query)}"
            
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=random.choice(USER_AGENTS))
                page.goto(search_url)
                self._random_sleep()
                
                # Try to get the first LinkedIn profile link
                li_link = page.query_selector('div.g a[href*="linkedin.com/in"]')
                if li_link:
                    profile_url = li_link.get_attribute("href")
                    if profile_url:
                        # Go to profile
                        page.goto(profile_url)
                        self._random_sleep()
                        
                        # The public profile sometimes has a contact-info section
                        page_text = page.inner_text("body")
                        
                        # Extract email
                        email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', page_text)
                        li_email = email_match.group(0) if email_match else ""
                        
                        # Extract phone
                        phone_match = re.search(r'(\+?91[\-\s]?)?0?[6-9]\d{2,4}[\-\s]?\d{3,4}[\-\s]?\d{3,4}', page_text)
                        li_phone = phone_match.group(0).strip() if phone_match else ""
                        
                        if li_email or li_phone:
                            clean_phone = re.sub(r'\D', '', li_phone)
                            if len(clean_phone) >= 10:
                                clean_phone = clean_phone[-10:]
                            else:
                                clean_phone = ""
                                
                            contacts.append({
                                "name": dm_name,
                                "phone": clean_phone,
                                "email": li_email,
                                "title": "LinkedIn Contact"
                            })
                            
                browser.close()
        except Exception as e:
            print(f"LinkedIn Contact search failed: {e}")
            
        return contacts

    def _justdial_search(self, name: str, city: str):
        contacts = []
        try:
            jd_url = f"https://www.justdial.com/{city.replace(' ', '-').lower()}/{name.replace(' ', '-').lower()}"
            resp = httpx.get(jd_url, headers=self._get_headers(), timeout=15.0, follow_redirects=True)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                text = soup.get_text()
                
                found_phones = re.findall(r'[6-9]\d{9}', text)
                # Justdial can have multiple phones, take the first valid one
                if found_phones:
                    # In Justdial, the contact person is sometimes listed under specific headers
                    # For now, we heuristically extract phone
                    contacts.append({
                        "name": "JD Contact Person", # Generic fallback if we can't extract the precise name
                        "phone": found_phones[0],
                        "title": "Contact Person"
                    })
        except Exception as e:
            print(f"JustDial search failed: {e}")
            
        return contacts

    def _indiamart_search(self, name: str, city: str, decision_makers: List[Dict]):
        import difflib
        contacts = []
        try:
            if not self.playwright_available:
                return contacts
                
            query = f"{name} {city}"
            url = f"https://dir.indiamart.com/search.mp?ss={urllib.parse.quote(query)}"
            
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=random.choice(USER_AGENTS))
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                self._random_sleep()
                
                try:
                    page.wait_for_selector('.ls_box', timeout=10000)
                except PlaywrightTimeout:
                    pass
                    
                elements = page.query_selector_all('.ls_box')
                if elements:
                    # Click first matching result to open seller profile page
                    link_el = elements[0].query_selector('a.lcname')
                    if link_el:
                        profile_url = link_el.get_attribute('href')
                        if profile_url and not profile_url.startswith('http'):
                            profile_url = 'https:' + profile_url
                        elif profile_url:
                            # It's already absolute
                            pass
                        
                        if profile_url:
                            page.goto(profile_url, wait_until="domcontentloaded", timeout=15000)
                            self._random_sleep()
                            
                            # Try to click "View Mobile" or similar button
                            try:
                                # Common Indiamart classes for the view mobile button
                                view_btn = page.query_selector('button:has-text("View"), a:has-text("View Mobile"), .pns_h')
                                if view_btn:
                                    view_btn.click()
                                    self._random_sleep()
                            except Exception:
                                pass
                                
                            page_text = page.inner_text("body")
                            # Extract phone
                            phone_match = re.search(r'[6-9]\d{9}', page_text)
                            if phone_match:
                                clean_phone = phone_match.group(0)
                                
                                # Try to extract seller name. Often found in specific seller name div
                                seller_name = ""
                                name_els = page.query_selector_all('.contact-name, .contact_name, .seller-name')
                                if name_els:
                                    seller_name = name_els[0].inner_text().strip()
                                
                                # Fallback fuzzy matching if seller_name is missing but we have DMs
                                match_confidence = "MEDIUM"
                                dm_matched_name = seller_name or "IndiaMART Seller"
                                
                                if decision_makers and seller_name:
                                    for dm in decision_makers:
                                        dm_name = dm.get("name", "")
                                        if dm_name:
                                            sim = difflib.SequenceMatcher(None, seller_name.lower(), dm_name.lower()).ratio()
                                            if sim >= 0.70:
                                                match_confidence = "HIGH_DIRECT"
                                                dm_matched_name = dm_name
                                                break
                                                
                                contacts.append({
                                    "name": dm_matched_name,
                                    "phone": clean_phone,
                                    "title": "Owner/Manager",
                                    "contact_confidence": match_confidence,
                                    "source": "indiamart"
                                })
                                
                browser.close()
        except Exception as e:
            print(f"IndiaMART search failed: {e}")
            
        return contacts

    def _resolve_identity(self, name: str, city: str):
        decision_makers = []
        confidence = "LOW"
        cin = None
        
        # Step A1: ZaubaCorp search
        zc_dms, zc_cin, zc_is_high_quality = self._zaubacorp_search(name, city)
        if zc_dms:
            decision_makers.extend(zc_dms)
            confidence = "HIGH" if zc_is_high_quality else "MEDIUM"
            cin = zc_cin
            
        # Step A2: MCA21 company search (if ZaubaCorp found CIN)
        if cin:
            mca_dms = self._mca21_search(cin)
            if mca_dms:
                # Replace ZaubaCorp DMs with MCA21 DMs as they are HIGHEST quality
                decision_makers = mca_dms
                confidence = "VERIFIED_GOVT"
                
        # Step A3: Google Search identity fallback
        if not decision_makers:
            gs_dms = self._google_identity_fallback(name, city)
            if gs_dms:
                decision_makers = gs_dms
                confidence = "MEDIUM"
                
        return decision_makers, confidence

    def _zaubacorp_search(self, name: str, city: str):
        dms = []
        cin = None
        is_high_quality = False
        
        try:
            # New Strategy: use company-search endpoint
            query = f"{name} {city}".replace(" ", "-").lower()
            url = "https://www.zaubacorp.com/company-search/"
            
            resp = httpx.post(url, data={"search": query}, headers=self._get_headers(), timeout=10.0)
            # Or if it's GET: resp = httpx.get(f"{url}{urllib.parse.quote(query)}", ...)
            # Let's try GET to {url}{query}
            resp = httpx.get(f"https://www.zaubacorp.com/company-search/{urllib.parse.quote(query)}", headers=self._get_headers(), timeout=10.0)
            
            if resp.status_code == 200 and "Just a moment" not in resp.text:
                soup = BeautifulSoup(resp.text, 'html.parser')
                text = soup.get_text().lower()
                
                # Check if it's a Pvt Ltd / LLP
                if "pvt ltd" in text or "private limited" in text or "llp" in text:
                    is_high_quality = True
                    
                # Extract CIN
                cin_match = re.search(r'([LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6})', text)
                if cin_match:
                    cin = cin_match.group(1)
                    
                # Extract Directors table
                raw_dms = []
                tables = soup.find_all('table')
                for table in tables:
                    if 'Director Name' in table.text or 'DIN' in table.text:
                        rows = table.find_all('tr')
                        for row in rows[1:]: # Skip header
                            cols = row.find_all('td')
                            if len(cols) >= 3:
                                d_din = cols[0].text.strip()
                                d_name = cols[1].text.strip()
                                d_desig = cols[2].text.strip() if len(cols) > 2 else "Director"
                                if d_name:
                                    raw_dms.append({
                                        "name": d_name, 
                                        "title": d_desig, 
                                        "din": d_din,
                                        "source": "zaubacorp"
                                    })
                        break
                        
                # Prioritise designations: Managing Director -> Director -> CEO -> CFO
                def priority_score(title):
                    title = title.lower()
                    if "managing director" in title: return 1
                    if "director" in title: return 2
                    if "ceo" in title: return 3
                    if "cfo" in title: return 4
                    return 5
                    
                raw_dms.sort(key=lambda x: priority_score(x["title"]))
                dms = raw_dms
                
        except Exception as e:
            print(f"ZaubaCorp search failed: {e}")
            
        return dms, cin, is_high_quality

    def _mca21_search(self, cin: str):
        dms = []
        try:
            url = "https://www.mca.gov.in/mcafoportal/viewCompanyMasterData.do"
            params = {"cin": cin}
            resp = httpx.get(url, params=params, headers=self._get_headers(), timeout=15.0)
            
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                
                # Look for the Signatory Details table (often has DIN/PAN, Name, Designation, Date of Appointment)
                tables = soup.find_all('table')
                for table in tables:
                    if 'DIN' in table.text or 'Name' in table.text:
                        rows = table.find_all('tr')
                        for row in rows[1:]:
                            cols = row.find_all('td')
                            # MCA table typically: DIN/PAN, Name, Designation, Date of Appointment
                            if len(cols) >= 4:
                                d_din = cols[0].text.strip()
                                d_name = cols[1].text.strip()
                                d_desig = cols[2].text.strip()
                                d_date = cols[3].text.strip()
                                
                                if d_name:
                                    dms.append({
                                        "name": d_name,
                                        "title": d_desig,
                                        "din": d_din,
                                        "appointment_date": d_date,
                                        "source": "mca21"
                                    })
                        break
                        
                # Prefer most recently appointed MD/Director
                # Sort first by designation priority, then by appointment date descending
                def priority_score(title):
                    title = title.lower()
                    if "managing director" in title or re.search(r'\bmd\b', title): return 1
                    if "director" in title: return 2
                    return 3
                    
                # Date format is usually DD/MM/YYYY, convert to sortable YYYYMMDD
                def parse_date(date_str):
                    try:
                        parts = date_str.split('/')
                        if len(parts) == 3:
                            return f"{parts[2]}{parts[1]}{parts[0]}"
                    except:
                        pass
                    return "00000000"
                    
                dms.sort(key=lambda x: (priority_score(x["title"]), -int(parse_date(x["appointment_date"]))))
                
        except Exception as e:
            print(f"MCA21 search failed: {e}")
            
        return dms

    def _google_identity_fallback(self, name: str, city: str):
        dms = []
        if not self.playwright_available:
            return dms
            
        queries = [
            f'"{name}" "{city}" founder OR owner OR proprietor'
        ]
        
        snippets = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=random.choice(USER_AGENTS))
                
                for query in queries:
                    page.goto(f"https://www.google.com/search?q={urllib.parse.quote(query)}")
                    self._random_sleep()
                    
                    try:
                        page.wait_for_selector('div.g', timeout=10000)
                    except PlaywrightTimeout:
                        continue
                        
                    results = page.query_selector_all('div.g')
                    for res in results[:3]:
                        # Combine title and snippet for maximum context
                        text = res.inner_text()
                        if text:
                            snippets.append(text)
                                
                browser.close()
        except Exception as e:
            print(f"Google Fallback error: {e}")
            
        if not snippets:
            return dms
            
        # Use Gemini 2.5 Flash for NLP extraction via modern SDK
        try:
            from google import genai
            from google.genai import types
            import json
            
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), vertexai=False)
            
            combined_text = "\n---\n".join(snippets)
            prompt = f"Extract the owner, founder, or proprietor name from this text. If no person is found, return empty strings. Text:\n{combined_text}"
            
            schema = {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "designation": {"type": "STRING"},
                    "confidence": {"type": "STRING", "enum": ["HIGH", "MEDIUM", "LOW"]}
                },
                "required": ["name", "designation", "confidence"]
            }
            
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.0
                )
            )
            
            if response.text:
                data = json.loads(response.text)
                if data.get("name") and data.get("name").lower() not in name.lower():
                    dms.append({
                        "name": data["name"],
                        "title": data.get("designation", "Owner/Founder"),
                        "source": "google_search",
                        "confidence": data.get("confidence", "MEDIUM").lower()
                    })
        except Exception as e:
            print(f"Gemini Extraction Error: {e}")
                
        return dms
