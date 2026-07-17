import os
import re
import time
import random
import urllib.parse
from typing import Dict
import httpx
from bs4 import BeautifulSoup

# Playwright is optional. Install it (pip install playwright && playwright
# install chromium) to enable the browser-based discovery steps; without it
# those steps are skipped automatically and everything else still works.
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

class ContactDiscoveryAgent:
    def __init__(self):
        self.playwright_available = (PLAYWRIGHT_INSTALLED
                                     and os.getenv("PLAYWRIGHT_ENABLED", "1") != "0")

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
        # Initialize contact fields
        for i in range(1, 4):
            lead[f'contact_person_{i}_name'] = ""
            lead[f'contact_person_{i}_designation'] = ""
            lead[f'contact_person_{i}_phone'] = ""
            lead[f'contact_person_{i}_email'] = ""
            
        lead['contact_confidence_score'] = "LOW"
        
        all_phones = []
        if lead.get("raw_phone"):
            all_phones.append(lead.get("raw_phone"))
            
        emails = []
        decision_makers = []

        name = lead.get("raw_name", "")
        city = lead.get("city", "Delhi NCR")
        raw_website = lead.get("raw_website", "")
        
        # Step 1: Google Maps profile
        if self.playwright_available and name:
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page(user_agent=random.choice(USER_AGENTS))
                    query = f"{name} {city}"
                    page.goto(f"https://www.google.com/maps/search/{urllib.parse.quote(query)}")
                    self._random_sleep()
                    
                    try:
                        page.wait_for_selector('div[role="main"]', timeout=10000)
                    except PlaywrightTimeout:
                        pass
                        
                    page_text = page.inner_text("body")
                    
                    # Try to find website link if not already present
                    if not raw_website:
                        links = page.query_selector_all('a[href^="http"]')
                        for link in links:
                            href = link.get_attribute("href")
                            if href and "google.com" not in href:
                                raw_website = href
                                break
                    
                    phones = re.findall(r'(\+?91[\-\s]?)?0?[6-9]\d{2,4}[\-\s]?\d{3,4}[\-\s]?\d{3,4}', page_text)
                    for ph in phones:
                        if ph and len(ph) >= 10:
                            all_phones.append(ph.strip())
                            
                    browser.close()
            except Exception as e:
                print(f"Maps Step error: {e}")

        # Step 2: Official website
        if raw_website:
            if not raw_website.startswith('http'):
                raw_website = 'http://' + raw_website
            base = raw_website.rstrip('/')
            urls_to_check = [base, f"{base}/contact", f"{base}/about"]
            for u in urls_to_check:
                try:
                    resp = httpx.get(u, headers=self._get_headers(), timeout=10.0, follow_redirects=True)
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, 'html.parser')
                        text = soup.get_text()
                        
                        found_phones = re.findall(r'[6-9]\d{9}', text)
                        all_phones.extend(found_phones)
                        
                        found_emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
                        # Drop asset-filename false positives (logo@2x.png) and
                        # tracker/placeholder domains scraped from page code.
                        junk = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp')
                        found_emails = [e for e in found_emails
                                        if not e.lower().endswith(junk)
                                        and 'example.' not in e.lower()
                                        and 'sentry' not in e.lower()
                                        and 'wixpress' not in e.lower()]
                        emails.extend(found_emails)
                        
                        keywords = ['founder', 'owner', 'director', 'ceo', 'md', 'proprietor']
                        text_lower = text.lower()
                        for kw in keywords:
                            if kw in text_lower:
                                decision_makers.append({"name": "Website Contact", "title": kw.upper()})
                                break
                except Exception as e:
                    pass

        # Step 3: JustDial profile
        if name:
            try:
                jd_url = f"https://www.justdial.com/{city.replace(' ', '-').lower()}/{name.replace(' ', '-').lower()}"
                resp = httpx.get(jd_url, headers=self._get_headers(), timeout=15.0, follow_redirects=True)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, 'html.parser')
                    text = soup.get_text()
                    found_phones = re.findall(r'[6-9]\d{9}', text)
                    if found_phones:
                        all_phones.extend(found_phones)
            except Exception as e:
                pass

        # Step 4: LinkedIn public profile
        if not decision_makers and self.playwright_available and name:
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page(user_agent=random.choice(USER_AGENTS))
                    query = f"{name} {city} founder OR owner OR director"
                    page.goto(f"https://www.linkedin.com/search/results/people/?keywords={urllib.parse.quote(query)}")
                    self._random_sleep()
                    
                    try:
                        page.wait_for_selector('li.reusable-search__result-container', timeout=10000)
                    except PlaywrightTimeout:
                        pass
                        
                    results = page.query_selector_all('li.reusable-search__result-container')
                    for res in results[:3]:
                        name_el = res.query_selector('span[dir="ltr"]')
                        title_el = res.query_selector('div.entity-result__primary-subtitle')
                        
                        dm_name = name_el.inner_text().strip() if name_el else ""
                        dm_title = title_el.inner_text().strip() if title_el else ""
                        if dm_name:
                            decision_makers.append({"name": dm_name, "title": dm_title})
                            
                    browser.close()
            except Exception as e:
                print(f"LinkedIn Step error: {e}")

        # Step 5: Assign contacts
        # Normalize and filter phones
        unique_phones = []
        for p in all_phones:
            if not p: continue
            clean = re.sub(r'\D', '', p)
            if len(clean) >= 10 and clean not in [re.sub(r'\D', '', up) for up in unique_phones]:
                unique_phones.append(p)
                
        emails = list(dict.fromkeys(e.lower() for e in emails))
        
        has_mobile = any(self._is_mobile(p) for p in unique_phones)
        
        idx = 1
        for dm in decision_makers[:3]:
            lead[f'contact_person_{idx}_name'] = dm.get("name", "")
            lead[f'contact_person_{idx}_designation'] = dm.get("title", "")
            lead[f'contact_person_{idx}_phone'] = unique_phones[idx-1] if len(unique_phones) >= idx else ""
            lead[f'contact_person_{idx}_email'] = emails[idx-1] if len(emails) >= idx else ""
            idx += 1
            
        # fallback if no DMs or more phones than DMs
        if not decision_makers and unique_phones:
            for bp in unique_phones:
                if idx <= 3:
                    lead[f'contact_person_{idx}_phone'] = bp
                    idx += 1
                    
        # Set confidence score
        if decision_makers and has_mobile:
            lead['contact_confidence_score'] = "HIGH"
        elif decision_makers and unique_phones:
            lead['contact_confidence_score'] = "MEDIUM"
        else:
            lead['contact_confidence_score'] = "LOW"
            
        return lead

