"""
Business domain/subdomain classifier with multi-model rotation and taxonomy
canonicalization.

Taxonomy sources (first found wins):
  1. scrape_and_validate_kit/llm_files/domain_taxonomy.json          (local override)
  2. ../lead_enrichment_system/lead_clean/configs/domains_subdomains.json
     (reuses the active pipeline's taxonomy so both systems tag identically)

Model behaviour:
  - GEMINI_MODELS (comma separated) round-robins; a quota error (429) puts
    the failing model on a cooldown and the next model takes over, so a
    single free-tier limit never stalls a long validation run.
  - Per-model pacing derives from GEMINI_RPM (default 15 -> one call per 4s
    per model), so the free tier is never tripped.
  - The LLM answer is snapped to the closest allowed taxonomy entry, so a
    near-miss spelling can never produce an off-taxonomy tag.
"""

import os
import re
import time
import json
import difflib

from google import genai
from google.genai import types

from utils.envtools import load_env, ARCHIVED_ROOT, WORKSPACE_ROOT

load_env()

QUOTA_COOLDOWN_SECONDS = 65
TRANSIENT_COOLDOWN_SECONDS = 20

TAXONOMY_PATHS = [
    os.path.join(ARCHIVED_ROOT, "llm_files", "domain_taxonomy.json"),
    os.path.join(WORKSPACE_ROOT, "lead_enrichment_system", "lead_clean",
                 "configs", "domains_subdomains.json"),
]


def _is_quota_error(msg: str) -> bool:
    msg = msg.lower()
    return ("429" in msg or "quota" in msg
            or "resource_exhausted" in msg or "resource exhausted" in msg)


def _load_taxonomy() -> dict:
    """Returns {domain: [subdomains]} from the first taxonomy file found."""
    for path in TAXONOMY_PATHS:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[TAXONOMY] Failed to read {path}: {e}")
            continue
        if isinstance(data, dict) and "taxonomy" in data:
            out = {t.get("domain", ""): t.get("subdomains", [])
                   for t in data["taxonomy"] if t.get("domain")}
        elif isinstance(data, dict):
            out = {k: list(v) for k, v in data.items()}
        elif isinstance(data, list):
            out = {t.get("domain", ""): t.get("subdomains", [])
                   for t in data if isinstance(t, dict) and t.get("domain")}
        else:
            continue
        print(f"[TAXONOMY] Loaded {len(out)} domains from {path}")
        return out
    print("[TAXONOMY] ERROR: no taxonomy file found. Expected one of:\n  "
          + "\n  ".join(TAXONOMY_PATHS))
    return {}


def _closest(raw, options):
    """Snap a value to the closest allowed option (never invents categories)."""
    s = str(raw or "").strip()
    if not s or not options:
        return None
    lmap = {o.lower(): o for o in options}
    if s.lower() in lmap:
        return lmap[s.lower()]
    close = difflib.get_close_matches(s, options, n=1, cutoff=0.6)
    if close:
        return close[0]
    return max(options, key=lambda o: difflib.SequenceMatcher(None, s.lower(), o.lower()).ratio())


class ClassifierClient:
    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            print("WARNING: GEMINI_API_KEY not set - classification will fail "
                  "until a key is added to scrape_and_validate_kit/.env")
        self.client = genai.Client(vertexai=False, api_key=api_key) if api_key else None

        self.taxonomy = _load_taxonomy()
        self.domains = list(self.taxonomy.keys())

        models_str = os.getenv("GEMINI_MODELS", "gemma-4-31b-it,gemini-3.1-flash-lite")
        self.models = [m.strip() for m in models_str.split(",") if m.strip()]
        rpm = int(os.getenv("GEMINI_RPM", "15") or "15")
        self.min_interval = 60.0 / max(1, rpm)
        self._idx = 0
        self._last_call = {m: 0.0 for m in self.models}
        self._cooldown_until = {m: 0.0 for m in self.models}

        taxonomy_min = json.dumps(self.taxonomy, separators=(",", ":"))
        self.system_instruction = (
            "You are an expert business classification engine. "
            "Map the business strictly to one of the provided domains and subdomains. "
            "If it doesn't fit neatly into any, pick the closest matching category."
            f"\n\nAllowed Taxonomy (domain -> subdomains):\n{taxonomy_min}"
        )
        self.schema = {
            "type": "OBJECT",
            "properties": {
                "domain": {"type": "STRING", "description": "Top-level domain from the taxonomy"},
                "subdomain": {"type": "STRING", "description": "Subdomain under the chosen domain"}
            },
            "required": ["domain", "subdomain"]
        }

    # ------------------------------------------------------ model rotation

    def _next_model(self) -> str:
        now = time.time()
        for _ in range(len(self.models)):
            m = self.models[self._idx % len(self.models)]
            self._idx += 1
            if now >= self._cooldown_until.get(m, 0.0):
                return m
        soonest = min(self._cooldown_until[m] for m in self.models)
        wait = max(1.0, soonest - time.time())
        print(f"  [LLM] All models cooling down. Waiting {wait:.0f}s...")
        time.sleep(wait)
        return self._next_model()

    def _pace(self, model: str):
        elapsed = time.time() - self._last_call.get(model, 0.0)
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call[model] = time.time()

    @staticmethod
    def _parse_json(text: str) -> dict:
        text = (text or "").strip()
        if text.startswith("```"):
            text = re.sub(r'^```(json)?\s*|\s*```$', '', text, flags=re.DOTALL).strip()
        return json.loads(text)

    # -------------------------------------------------------- classification

    def classify(self, context_text: str, business_name: str) -> dict:
        failed = {"domain": "Classification Failed", "subdomain": "Classification Failed"}
        if not self.client or not self.taxonomy:
            return failed

        prompt = (f"Business Name: {business_name}\nBusiness Context: {context_text}\n\n"
                  'Return a JSON object with keys "domain" and "subdomain", '
                  "chosen from the allowed taxonomy.")

        result = None
        for _ in range(2 * len(self.models)):
            model = self._next_model()
            self._pace(model)
            try:
                if model.startswith("gemma"):
                    # gemma: system prompt inlined, no schema enforcement
                    contents = f"{self.system_instruction}\n\n{prompt}"
                    config = types.GenerateContentConfig(
                        temperature=0.0, response_mime_type="application/json")
                else:
                    contents = prompt
                    config = types.GenerateContentConfig(
                        temperature=0.0, response_mime_type="application/json",
                        response_schema=self.schema,
                        system_instruction=self.system_instruction)
                response = self.client.models.generate_content(
                    model=model, contents=contents, config=config)
                if not response.text:
                    raise ValueError("empty response")
                result = self._parse_json(response.text)
                break
            except Exception as e:
                msg = str(e)
                cool = QUOTA_COOLDOWN_SECONDS if _is_quota_error(msg) else TRANSIENT_COOLDOWN_SECONDS
                self._cooldown_until[model] = time.time() + cool
                print(f"  [LLM] '{model}' failed ({msg[:100]}). Cooling {cool}s, trying next model...")

        if not isinstance(result, dict):
            return failed

        domain = _closest(result.get("domain", ""), self.domains)
        if not domain:
            return failed
        subs = self.taxonomy.get(domain, [])
        subdomain = _closest(result.get("subdomain", ""), subs) or (subs[0] if subs else "")
        return {"domain": domain, "subdomain": subdomain}


def classify_business(website_text: str, business_name: str, fetch_status: str = 'success') -> dict:
    """Back-compat helper kept from the original API."""
    if fetch_status == 'unreachable':
        return {'domain': 'Unreachable Website', 'subdomain': 'Unreachable Website'}
    if fetch_status == 'invalid_url':
        return {'domain': 'Invalid URL', 'subdomain': 'Invalid URL'}
    if fetch_status == 'no_website':
        return {'domain': 'No Website Provided', 'subdomain': 'No Website Provided'}
    return ClassifierClient().classify(website_text, business_name)
