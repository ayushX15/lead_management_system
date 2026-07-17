"""
Gemini client with multi-model rotation for the lead_gen enrichment step.

- Models come from GEMINI_MODELS (comma separated). On a quota error (429)
  the failing model cools down and the next model takes over, so a single
  free-tier limit never stalls the run.
- Per-model pacing (GEMINI_RPM, default 15 rpm) keeps every model under the
  free-tier requests-per-minute limit.
- No API key -> the client degrades gracefully: enrichment returns default
  values and the scraping pipeline keeps working. Paste a working key into
  scrape_and_validate_kit/.env (or lead_enrichment_system/.env) and it is picked up
  on the next run with zero code changes.
"""

import os
import re
import time
import json

from google import genai
from google.genai import types

from utils.envtools import load_env

QUOTA_COOLDOWN_SECONDS = 65
TRANSIENT_COOLDOWN_SECONDS = 20


def _is_quota_error(msg: str) -> bool:
    msg = msg.lower()
    return ("429" in msg or "quota" in msg
            or "resource_exhausted" in msg or "resource exhausted" in msg)


class GeminiClient:
    def __init__(self):
        load_env()
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.client = None
        if api_key:
            self.client = genai.Client(vertexai=False, api_key=api_key)
        else:
            print("[LLM] WARNING: GEMINI_API_KEY not set. Enrichment will return defaults "
                  "until you add a key to scrape_and_validate_kit/.env")

        models_str = os.getenv("GEMINI_MODELS", "gemma-4-31b-it,gemini-3.1-flash-lite")
        self.models = [m.strip() for m in models_str.split(",") if m.strip()]
        rpm = int(os.getenv("GEMINI_RPM", "15") or "15")
        self.min_interval = 60.0 / max(1, rpm)

        self._idx = 0
        self._last_call = {m: 0.0 for m in self.models}
        self._cooldown_until = {m: 0.0 for m in self.models}

        self.system_instruction = (
            "You are a B2B lead qualification engine. "
            "Evaluate the business data and return JSON."
        )
        self.response_schema = {
            "type": "OBJECT",
            "properties": {
                "size_class": {
                    "type": "STRING",
                    "enum": ["small", "mid", "upper-mid", "large"]
                },
                "decision_maker_title": {"type": "STRING"},
                "lead_quality_score": {"type": "INTEGER"},
                "reasoning": {"type": "STRING"}
            },
            "required": ["size_class", "decision_maker_title", "lead_quality_score", "reasoning"]
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
        print(f"[LLM] All models cooling down. Waiting {wait:.0f}s...")
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

    def generate_json(self, prompt: str, schema=None, system_instruction=None):
        """One JSON completion with rotation + failover. Returns dict or None."""
        if not self.client:
            return None
        for _ in range(2 * len(self.models)):
            model = self._next_model()
            self._pace(model)
            try:
                if model.startswith("gemma"):
                    # gemma: system prompt inlined, no schema enforcement -
                    # markdown fences are stripped by _parse_json instead
                    contents = f"{system_instruction}\n\n{prompt}" if system_instruction else prompt
                    config = types.GenerateContentConfig(
                        temperature=0.1, response_mime_type="application/json")
                else:
                    contents = prompt
                    config = types.GenerateContentConfig(
                        temperature=0.1, response_mime_type="application/json",
                        response_schema=schema, system_instruction=system_instruction)
                response = self.client.models.generate_content(
                    model=model, contents=contents, config=config)
                if not response.text:
                    raise ValueError("empty response")
                return self._parse_json(response.text)
            except Exception as e:
                msg = str(e)
                cool = QUOTA_COOLDOWN_SECONDS if _is_quota_error(msg) else TRANSIENT_COOLDOWN_SECONDS
                self._cooldown_until[model] = time.time() + cool
                print(f"[LLM] '{model}' failed ({msg[:120]}). Cooling {cool}s, failing over...")
        return None

    # ---------------------------------------------------- lead enrichment

    def enrich_lead(self, raw_data: dict) -> dict:
        default = {
            "size_class": "unknown",
            "decision_maker_title": "",
            "lead_quality_score": 5,   # neutral: an LLM outage must never QA-reject a lead
            "reasoning": "Enrichment unavailable (no key or all models failed).",
        }
        prompt = f"Business data: {json.dumps(raw_data)}. Classify this business."
        result = self.generate_json(prompt, schema=self.response_schema,
                                    system_instruction=self.system_instruction)
        if not isinstance(result, dict):
            return default
        return {**default, **result}
