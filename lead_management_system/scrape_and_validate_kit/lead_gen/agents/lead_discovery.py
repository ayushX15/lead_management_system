import os
import time
import random
import json
import datetime
from typing import List, Dict
import httpx
from bs4 import BeautifulSoup

from utils.envtools import load_env, output_dir

load_env()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36"
]

class LeadDiscoveryAgent:
    def __init__(self):
        self.serpapi_key = os.getenv("SERPAPI_KEY", "")
        
        # Stateful pagination tracker lives with the run outputs (GEN_OUTPUT_DIR)
        self.state_file = os.path.join(output_dir(), "search_state.json")
        self.search_state = self._load_state()
        
    def _load_state(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}
        
    def _save_state(self):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.search_state, f, indent=2)
            
    def _random_sleep(self):
        delay = random.uniform(2.0, 4.0)
        time.sleep(delay)
        
    def _get_headers(self):
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    def _format_lead(self, raw_name, raw_address, raw_phone, raw_website, source, task) -> Dict:
        return {
            "raw_name": str(raw_name).strip() if raw_name else "",
            "raw_address": str(raw_address).strip() if raw_address else "",
            "raw_phone": str(raw_phone).strip() if raw_phone else "",
            "raw_website": str(raw_website).strip() if raw_website else "",
            "source": source,
            "search_query": task.get("search_query", ""),
            "city": task.get("city", ""),
            "domain": task.get("domain", ""),
            "subcategory": task.get("subcategory", ""),
            "date_scraped": datetime.date.today().isoformat()
        }

    def discover(self, search_task: Dict[str, str]) -> List[Dict[str, str]]:
        query = search_task.get("search_query", "")
        
        # Determine the current pagination offset for this specific query
        current_offset = self.search_state.get(query, 0)
        
        all_leads = []
        
        # Primary Source: Google Maps (SerpAPI) - Flawless Pagination API
        if self.serpapi_key:
            maps_leads = self._scrape_serpapi(search_task, query, current_offset)
            all_leads.extend(maps_leads)
            
            # Increment and save state (SerpAPI returns 20 results per page)
            if maps_leads:
                self.search_state[query] = current_offset + 20
                self._save_state()
        
        # Fallback Source: JustDial (Limited pagination)
        if not all_leads:
            jd_leads = self._scrape_justdial(search_task)
            all_leads.extend(jd_leads)
        
        # Deduplicate combined list by raw_name + city (Initial sanitize)
        seen = set()
        deduped = []
        for lead in all_leads:
            name_norm = lead.get('raw_name', '').lower().strip()
            city_norm = lead.get('city', '').lower().strip()
            phone_norm = lead.get('raw_phone', '').strip()
            
            if not name_norm and not phone_norm:
                continue  # nothing identifying - drop the empty lead

            key = f"{name_norm}_{city_norm}"
            if key not in seen:
                seen.add(key)
                deduped.append(lead)
                
        return deduped
            
    def _scrape_serpapi(self, task: Dict, query: str, offset: int) -> List[Dict]:
        print(f"Scraping Google Maps via SerpAPI (Offset: {offset}) for: {query}")
        self._random_sleep()
        leads = []
        url = "https://serpapi.com/search"
        params = {
            "engine": "google_maps",
            "q": query,
            "hl": "en",
            "gl": "in",
            "start": offset,
            "api_key": self.serpapi_key
        }
        
        # Exponential Backoff for Anti-Ban Protection
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = httpx.get(url, params=params, timeout=30.0)
                if resp.status_code == 200:
                    data = resp.json()
                    for res in data.get("local_results", []):
                        leads.append(self._format_lead(
                            raw_name=res.get("title"),
                            raw_address=res.get("address"),
                            raw_phone=res.get("phone"),
                            raw_website=res.get("website"),
                            source="Google Maps",
                            task=task
                        ))
                    break # Success
                elif resp.status_code == 429:
                    print(f"SerpAPI Rate Limit Hit (429). Backing off for {5 * (attempt+1)}s...")
                    time.sleep(5 * (attempt + 1))
                elif resp.status_code in (401, 403):
                    print("SerpAPI auth failed (401/403) - check SERPAPI_KEY. Falling back to free scraping.")
                    break
                else:
                    print(f"SerpAPI returned status {resp.status_code}")
                    break
            except httpx.RequestError as e:
                print(f"SerpAPI Network Error: {e}. Retrying...")
                time.sleep(3)
                
        return leads

    def _scrape_justdial(self, task: Dict) -> List[Dict]:
        city = task.get("city", "").replace(" ", "-").lower()
        subcat = task.get("subcategory", "").replace(" ", "-").lower()
        url = f"https://www.justdial.com/{city}/{subcat}"
        
        print(f"Scraping JustDial Fallback: {url}")
        self._random_sleep()
        leads = []
        try:
            resp = httpx.get(url, headers=self._get_headers(), follow_redirects=True, timeout=30.0)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                cards = soup.find_all('div', class_='resultbox_info')
                if not cards:
                    cards = soup.find_all('div', class_='jsx-b152d1ffdc76856a') 
                for card in cards[:10]:
                    name_tag = card.find('h2')
                    name = name_tag.text.strip() if name_tag else ""
                    
                    address = ""
                    text_blocks = card.get_text(separator='|').split('|')
                    if len(text_blocks) > 1:
                        address = text_blocks[1].strip()
                        
                    if name:
                        leads.append(self._format_lead(
                            raw_name=name,
                            raw_address=address,
                            raw_phone="", 
                            raw_website="",
                            source="JustDial",
                            task=task
                        ))
        except Exception as e:
            print(f"JustDial error: {e}")
        return leads
