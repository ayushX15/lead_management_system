import os
import json
import time
import asyncio
import datetime
from typing import List, Dict, Optional
from google import genai
from google.genai import types
from google.oauth2 import service_account
from dotenv import load_dotenv

# How long a model sits out after its FIRST 429 in a run before we try it once
# more. A second 429 after the cooldown means the daily quota (not the
# per-minute quota) is gone, and the model is benched until the next day.
QUOTA_COOLDOWN_SECONDS = 70

# Google's free-tier daily quotas reset at midnight PACIFIC time, so the
# "quota day" (benches + daily counters) must be computed in that timezone,
# not UTC - otherwise a benched model sits out up to 7 extra hours.
try:
    from zoneinfo import ZoneInfo
    QUOTA_TZ = ZoneInfo("America/Los_Angeles")
except Exception:                                    # tz database unavailable
    QUOTA_TZ = datetime.timezone(datetime.timedelta(hours=-8))


class QuotaExhaustedAllModels(RuntimeError):
    pass


class SmartLLMRouter:
    """
    Round-robin router over the models declared in model_limits.json.

    Availability rules (per user spec):
    - Pacing: each model is called at most `rpm` times per minute.
    - Daily budget: each model is called at most `rpd` times per calendar day.
    - 429/quota error: first one puts the model in a short cooldown (it may
      just be the per-minute quota); a second 429 benches it for the day.
    - Any other error (500/503/network): the model is benched for the REST OF
      THIS RUN only and retried on the next run. If it also failed last run,
      it is benched for the day.
    - Benches and daily counters persist in model_state.json so hourly runs
      share the same view of the day. Everything resets on date change.

    Adding/removing models only requires editing model_limits.json.
    """

    def __init__(self, limits_json_path: str, state_dir: Optional[str] = None):
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

        # 1. AI Studio client
        self.client_api = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))

        # 2. Vertex AI client (service key). Project/location come from the env
        # (VERTEX_PROJECT / VERTEX_LOCATION) so no GCP identifier is hardcoded in
        # source. If VERTEX_PROJECT is unset, the Vertex client is simply skipped
        # and only the AI-Studio (vertex:false) models are used.
        key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs", "research_lab_service_key.json")
        vertex_project = os.environ.get("VERTEX_PROJECT", "").strip()
        vertex_location = os.environ.get("VERTEX_LOCATION", "us-central1").strip()
        if not vertex_project:
            self.client_vertex = None
        else:
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    key_path, scopes=["https://www.googleapis.com/auth/cloud-platform"])
                self.client_vertex = genai.Client(
                    vertexai=True,
                    project=vertex_project,
                    location=vertex_location,
                    credentials=credentials
                )
            except Exception as e:
                print(f"[ROUTER INIT ERROR] Failed to load Vertex AI Service Key from {key_path}: {e}")
                self.client_vertex = None

        with open(limits_json_path, 'r') as f:
            self.models: List[Dict] = json.load(f).get("models", [])
        if not self.models:
            raise ValueError("model_limits.json declares no models")
        roster = ", ".join(f"{m['model_name']}(rpm={m['limits']['rpm']},rpd={m['limits'].get('rpd')})"
                           for m in self.models)
        print(f"[ROUTER] {len(self.models)} models loaded: {roster}")

        self.state_path = os.path.join(state_dir, "model_state.json") if state_dir else None
        self.state = self._load_state()

        # In-memory, per-run only
        self.down_this_run = set()          # non-quota failure this run
        self.cooldown_until: Dict[str, float] = {}
        self.had_429_this_run = set()
        self.consecutive_429s: Dict[str, int] = {}   # vertex capacity-429 streak
        self.last_called_time = {m["model_name"]: 0.0 for m in self.models}
        self.current_model_idx = 0
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------------ state

    @staticmethod
    def _today() -> str:
        # the "quota day" follows Google's reset clock (midnight Pacific)
        return datetime.datetime.now(QUOTA_TZ).date().isoformat()

    def _blank_model_state(self) -> Dict:
        return {"requests_today": 0, "exhausted_for_day": False, "failed_last_run": False}

    def _load_state(self) -> Dict:
        state = {"date": self._today(), "models": {}}
        if self.state_path and os.path.exists(self.state_path):
            try:
                with open(self.state_path, 'r') as f:
                    state = json.load(f)
            except Exception as e:
                print(f"[ROUTER WARNING] Could not read model_state.json ({e}); starting fresh.")
        if state.get("date") != self._today():
            state = {"date": self._today(), "models": {}}
        for m in self.models:
            state["models"].setdefault(m["model_name"], self._blank_model_state())
        return state

    def _save_state(self):
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)

    def _check_date_rollover(self):
        if self.state.get("date") != self._today():
            print("[ROUTER] New day detected. Resetting all daily model benches and counters.")
            self.state = {"date": self._today(), "models": {m["model_name"]: self._blank_model_state() for m in self.models}}
            self.down_this_run.clear()
            self.cooldown_until.clear()
            self.had_429_this_run.clear()
            self._save_state()

    # ------------------------------------------------------------- selection

    def _model_state(self, name: str) -> Dict:
        return self.state["models"].setdefault(name, self._blank_model_state())

    def _is_available(self, model: Dict, now: float) -> bool:
        name = model["model_name"]
        st = self._model_state(name)
        if st["exhausted_for_day"]:
            return False
        if name in self.down_this_run:
            return False
        if now < self.cooldown_until.get(name, 0.0):
            return False
        rpd = model.get("limits", {}).get("rpd")
        if isinstance(rpd, int) and st["requests_today"] >= rpd:
            return False
        return True

    async def _get_next_available_model(self) -> Dict:
        while True:
            async with self.lock:
                self._check_date_rollover()
                now = time.time()
                for _ in range(len(self.models)):
                    model = self.models[self.current_model_idx]
                    self.current_model_idx = (self.current_model_idx + 1) % len(self.models)
                    if self._is_available(model, now):
                        return model
                # None available right now. If some are only cooling down, wait for them.
                cooling = [self.cooldown_until[m["model_name"]] - now
                           for m in self.models
                           if m["model_name"] in self.cooldown_until
                           and self.cooldown_until[m["model_name"]] > now
                           and not self._model_state(m["model_name"])["exhausted_for_day"]
                           and m["model_name"] not in self.down_this_run]
            if cooling:
                wait = max(1.0, min(cooling))
                print(f"[ROUTER] All models busy/cooling. Waiting {wait:.0f}s for a cooldown to expire...")
                await asyncio.sleep(wait)
                continue
            raise QuotaExhaustedAllModels(
                "ALL MODELS EXHAUSTED: every model is benched (daily quota, daily rpd budget, or repeated errors this run).")

    async def _enforce_pacing(self, model: Dict):
        model_name = model["model_name"]
        target_rpm = model["limits"]["rpm"]
        delay_required = 60.0 / target_rpm
        async with self.lock:
            elapsed = time.time() - self.last_called_time[model_name]
            sleep_time = max(0.0, delay_required - elapsed)
            self.last_called_time[model_name] = time.time() + sleep_time
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

    # ------------------------------------------------------------------ call

    def _sync_generate_content(self, model_name: str, prompt: str, schema: Optional[types.Schema] = None,
                               response_mime_type: str = "text/plain", use_vertex: bool = False):
        config = types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type=response_mime_type,
            response_schema=schema
        )
        client_to_use = self.client_vertex if use_vertex and self.client_vertex else self.client_api
        response = client_to_use.models.generate_content(
            model=model_name,
            contents=prompt,
            config=config
        )
        tokens = response.usage_metadata.total_token_count if response.usage_metadata else 0
        return response.text, tokens

    def _log_telemetry(self, file_path: str, user_id: str, model: str, latency: int, status: str, tokens: int):
        timestamp = datetime.datetime.now().isoformat()
        file_exists = os.path.exists(file_path)
        with open(file_path, "a", encoding="utf-8") as f:
            if not file_exists:
                f.write("Timestamp,User_ID,Model_Used,Latency_ms,Status_Code,Total_Tokens\n")
            f.write(f"{timestamp},{user_id},{model},{latency},{status},{tokens}\n")

    @staticmethod
    def _is_quota_error(error_msg: str) -> bool:
        return ("429" in error_msg or "quota" in error_msg
                or "resource_exhausted" in error_msg or "resource exhausted" in error_msg)

    async def generate_content(self, prompt: str, user_id: str = "", telemetry_file: str = "",
                               schema: Optional[types.Schema] = None,
                               response_mime_type: str = "text/plain") -> str:
        """
        Routes one request. Fails over across models, so a single model error
        never loses the lead. Raises QuotaExhaustedAllModels only when no
        model can serve.
        """
        # 2 passes over the list is enough: one 429-cooldown retry per model max.
        for _ in range(2 * len(self.models)):
            model = await self._get_next_available_model()
            model_name = model["model_name"]

            await self._enforce_pacing(model)

            start_time = time.time()
            try:
                use_vertex = model.get("vertex", False)
                result_text, tokens = await asyncio.to_thread(
                    self._sync_generate_content, model_name, prompt, schema, response_mime_type, use_vertex
                )
                latency = round((time.time() - start_time) * 1000)
                async with self.lock:
                    st = self._model_state(model_name)
                    st["requests_today"] += 1
                    st["failed_last_run"] = False
                    # a success clears the 429 strike, so an isolated per-minute
                    # 429 blip can never accumulate into a wrongful day-bench
                    self.had_429_this_run.discard(model_name)
                    self.consecutive_429s[model_name] = 0
                    self._save_state()
                if telemetry_file:
                    self._log_telemetry(telemetry_file, user_id, model_name, latency, "200", tokens)
                return result_text

            except Exception as e:
                latency = round((time.time() - start_time) * 1000)
                error_msg = str(e).lower()
                async with self.lock:
                    st = self._model_state(model_name)
                    st["requests_today"] += 1
                    if self._is_quota_error(error_msg):
                        if model.get("vertex", False):
                            # Vertex pay-as-you-go has NO daily quota - every 429
                            # is transient shared-capacity. Never bench for the
                            # day; escalate the cooldown instead (30s->60s->120s->300s).
                            streak = self.consecutive_429s.get(model_name, 0) + 1
                            self.consecutive_429s[model_name] = streak
                            cool = min(300, 30 * (2 ** (streak - 1)))
                            print(f"[ROUTER] '{model_name}' Vertex capacity 429 (streak {streak}). Cooling {cool}s.")
                            self.cooldown_until[model_name] = time.time() + cool
                        elif model_name in self.had_429_this_run:
                            print(f"[ROUTER] '{model_name}' hit its quota again after cooldown -> benched for the day.")
                            st["exhausted_for_day"] = True
                        else:
                            print(f"[ROUTER] '{model_name}' hit a quota limit (429). Cooling down {QUOTA_COOLDOWN_SECONDS}s before one retry.")
                            self.had_429_this_run.add(model_name)
                            self.cooldown_until[model_name] = time.time() + QUOTA_COOLDOWN_SECONDS
                        status = "429"
                    else:
                        if st["failed_last_run"]:
                            print(f"[ROUTER] '{model_name}' failed again ({e}) after failing last run -> benched for the day.")
                            st["exhausted_for_day"] = True
                        else:
                            print(f"[ROUTER] '{model_name}' failed ({e}). Benched for this run; will retry next run.")
                            st["failed_last_run"] = True
                        self.down_this_run.add(model_name)
                        status = "500"
                    self._save_state()
                if telemetry_file:
                    self._log_telemetry(telemetry_file, user_id, model_name, latency, status, 0)
                # fail over to the next model for this same lead
                continue

        raise QuotaExhaustedAllModels("ALL MODELS EXHAUSTED: tried every available model and none could serve.")
