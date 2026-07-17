"""
Lead Clean Pipeline orchestrator.

Flow (per user spec):
  Part 1  - every CSV in the S3 bucket is streamed ONCE, untouched (only a
            row-level UUID added), into the raw_data_leads table.
  Part 2A - each row is standardized (column mapping, name fixing, garbage
            string cleanup) and given a PERMANENT lead User_ID.
  Part 2B - the multi-model router enriches Position / Domain / Sub Domain /
            Company Scale, strictly matched against the config JSONs.
  Part 2C - only fully-enriched leads are saved to cleaned_data_leads.
            Re-occurrences of an already-seen lead go to duplicate_raw_leads,
            grouped under the original lead's User_ID.

State model (the fix for the old tracker bugs):
  - lead_registry.jsonl : append-only journal. A lead is written as "pending"
    BEFORE enrichment and "enriched" only AFTER DynamoDB confirms the save.
    Keys are the lead's identity fingerprint, never a row number.
  - On startup every "pending" lead is verified against cleaned_data_leads:
    fully enriched -> promoted; partial -> deleted from the table and redone;
    absent -> redone. The same User_ID is reused, so IDs are permanent.
  - processed_files.json : per-CSV ETag + progress, so a file is never raw-
    streamed twice and an already-finished CSV is never re-processed.
  - All state files are backed up to s3://<bucket>/state/ during and after
    every run.

Rows are processed strictly in CSV order. A row whose enrichment fails stays
"pending" and is retried on the next run; it is never saved half-empty.
"""

import os
import re
import sys
import glob
import json
import time
import uuid
import difflib
import asyncio
import datetime

import pandas as pd  # type: ignore
import requests
from bs4 import BeautifulSoup
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from smart_llm_router import SmartLLMRouter, QuotaExhaustedAllModels

# --------------------------------------------------------------------- schema

TARGET_COLUMNS = [
    "User_ID", "First Name", "Last Name", "Title", "Position", "Company Name",
    "Company Email", "Contact Number_1", "Contact Number_2", "No of Employees",
    "Industry", "Keywords", "Person LinkedIn", "Company LinkedIn", "Company Website",
    "Company Facebook", "Company Twitter", "City", "State", "Country",
    "Annual Revenue", "Last Revenue", "Total Funding", "Last Funding",
    "Domain", "Sub Domain", "Company Scale"
]

ENRICH_FIELDS = ["Position", "Domain", "Sub Domain", "Company Scale"]
DEDUP_FIELDS = ["First Name", "Last Name", "Title", "Position", "Company Name", "Company Email"]

STATE_S3_PREFIX = "state/"
# Rows between S3 state backups. The registry lives primarily on the Modal
# volume (persists on its own); S3 is only disaster insurance, and even a lost
# tail is rebuilt by reconciliation from DynamoDB. So a large interval is safe
# and avoids re-uploading the whole (growing) journal many times per run.
# The run also ALWAYS backs up once at the end (finally block).
REGISTRY_FLUSH_EVERY = 1000
PROGRESS_EVERY = 250            # rows between progress prints
RAW_CHECKPOINT_EVERY = 10000    # rows between raw-stream bookmarks (must be a
                                # multiple of 25 so the batch_writer buffer is
                                # empty at each checkpoint - bookmark == committed)


class EnrichmentError(Exception):
    """LLM output missing/invalid - the lead stays pending and is retried next run."""


# ----------------------------------------------------------------- aws clients

def build_aws(env):
    kwargs = dict(
        aws_access_key_id=env.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=env.get("AWS_SECRET_ACCESS_KEY"),
        region_name=env.get("AWS_DEFAULT_REGION", "us-east-1"),
    )
    dynamodb = boto3.resource('dynamodb', **kwargs)
    s3 = boto3.client('s3', **kwargs)
    return {
        "s3": s3,
        "cleaned": dynamodb.Table(env.get("AWS_DYNAMODB_TABLE_NAME", "cleaned_data_leads")),
        "raw": dynamodb.Table(env.get("AWS_DYNAMODB_RAW_TABLE_NAME", "raw_data_leads")),
        "duplicates": dynamodb.Table("duplicate_raw_leads"),
        "scrape": dynamodb.Table("cleaned_leads_scrape_context"),
    }


# ---------------------------------------------------------------------- state

class StateManager:
    def __init__(self, data_dir, s3_client=None, s3_bucket=None):
        self.state_dir = os.path.join(data_dir, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.registry_path = os.path.join(self.state_dir, "lead_registry.jsonl")
        self.files_path = os.path.join(self.state_dir, "processed_files.json")
        self.model_state_path = os.path.join(self.state_dir, "model_state.json")
        self.s3 = s3_client
        self.bucket = s3_bucket
        self._appends_since_flush = 0

        self.leads = {}        # dedup_key -> {"u": user_id, "s": status, "o": origin}
        self.dup_origins = set()   # "filekey:rowidx" copies already written to duplicate table
        self.dup_keys = set()      # dedup_keys that have at least one duplicate copy
        self.files = {"files": {}, "done_etags": []}

    # ---- load / persist

    def _s3_download_if_missing(self, local_path):
        if os.path.exists(local_path) or not (self.s3 and self.bucket):
            return
        key = STATE_S3_PREFIX + os.path.basename(local_path)
        try:
            self.s3.download_file(self.bucket, key, local_path)
            print(f"[STATE] Restored {os.path.basename(local_path)} from S3 backup.")
        except Exception:
            pass  # no backup yet

    def load(self):
        for p in (self.registry_path, self.files_path, self.model_state_path):
            self._s3_download_if_missing(p)

        if os.path.exists(self.registry_path):
            with open(self.registry_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # torn last line from a crash - safe to ignore
                    if rec.get("t") == "lead":
                        self.leads[rec["k"]] = {"u": rec["u"], "s": rec["s"], "o": rec["o"]}
                    elif rec.get("t") == "dup":
                        self.dup_origins.add(rec["o"])
                        self.dup_keys.add(rec["k"])
        if os.path.exists(self.files_path):
            with open(self.files_path, 'r', encoding='utf-8') as f:
                self.files = json.load(f)
        n_enriched = sum(1 for v in self.leads.values() if v["s"] == "enriched")
        print(f"[STATE] Registry loaded: {len(self.leads)} leads known "
              f"({n_enriched} enriched), {len(self.dup_origins)} duplicate copies recorded.")

    def _append(self, rec):
        with open(self.registry_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + "\n")
        self._appends_since_flush += 1
        if self._appends_since_flush >= REGISTRY_FLUSH_EVERY:
            self.backup_to_s3()

    def record_lead(self, key, user_id, status, origin):
        self.leads[key] = {"u": user_id, "s": status, "o": origin}
        self._append({"t": "lead", "k": key, "u": user_id, "s": status, "o": origin})

    def record_dup(self, key, origin):
        self.dup_origins.add(origin)
        self.dup_keys.add(key)
        self._append({"t": "dup", "k": key, "o": origin})

    def save_files_state(self):
        tmp = self.files_path + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.files, f, indent=2)
        os.replace(tmp, self.files_path)

    def backup_to_s3(self):
        self._appends_since_flush = 0
        if not (self.s3 and self.bucket):
            return
        self.save_files_state()
        for p in (self.registry_path, self.files_path, self.model_state_path):
            if os.path.exists(p):
                try:
                    self.s3.upload_file(p, self.bucket, STATE_S3_PREFIX + os.path.basename(p))
                except Exception as e:
                    print(f"[STATE] WARNING: S3 backup of {os.path.basename(p)} failed: {e}")


# -------------------------------------------------------------- configuration

def load_configs(script_dir):
    cfg_dir = os.path.join(script_dir, "configs")

    with open(os.path.join(cfg_dir, "column_mapping.json"), 'r', encoding='utf-8') as f:
        mapping = json.load(f)
    reverse_map = {}
    for std_name, messy_list in mapping.items():
        for messy in messy_list:
            reverse_map[str(messy).lower().strip()] = std_name
    # every standard column name always maps to itself, so CSVs that already
    # use the standard headers work even if the alias list doesn't repeat them
    for std_name in TARGET_COLUMNS:
        reverse_map.setdefault(std_name.lower(), std_name)

    with open(os.path.join(cfg_dir, "positions.json"), 'r', encoding='utf-8') as f:
        positions = list(json.load(f).keys())
    positions_map = {p.lower(): p for p in positions}

    with open(os.path.join(cfg_dir, "domains_subdomains.json"), 'r', encoding='utf-8') as f:
        taxonomy = json.load(f).get("taxonomy", [])
    domain_map = {}          # lower -> canonical domain
    subdomain_maps = {}      # canonical domain -> {lower sub -> canonical sub}
    for item in taxonomy:
        d = item.get("domain", "")
        if not d:
            continue
        domain_map[d.lower()] = d
        subdomain_maps[d] = {s.lower(): s for s in item.get("subdomains", [])}
    taxonomy_min = json.dumps({t["domain"]: t.get("subdomains", []) for t in taxonomy},
                              separators=(',', ':'))

    return {
        "reverse_map": reverse_map,
        "positions": positions,
        "positions_map": positions_map,
        "domain_map": domain_map,
        "subdomain_maps": subdomain_maps,
        "taxonomy_min": taxonomy_min,
    }


# ------------------------------------------------------------- standardization

def standardize_dataframe(df_raw, reverse_map):
    """Part 2A: map columns, fix names, clean garbage strings. Row order preserved."""
    df = pd.DataFrame(index=df_raw.index, columns=TARGET_COLUMNS, data="")
    for col in df_raw.columns:
        std = reverse_map.get(str(col).lower().strip())
        if std in TARGET_COLUMNS:
            df[std] = df_raw[col].astype(str)
    df = df.fillna("")

    def fix_row(row):
        # whitespace + literal nan cleanup on every field
        for c in TARGET_COLUMNS:
            v = str(row[c]).strip()
            if v.lower() in ("nan", "none"):
                v = ""
            row[c] = v
        # split "Full Name" sitting in First Name
        fname, lname = row["First Name"], row["Last Name"]
        if not lname and " " in fname:
            parts = fname.split(" ")
            row["First Name"], row["Last Name"] = parts[0], " ".join(parts[1:])
        # location / industry garbage cleanup
        garbage_words = {"#error!", "n/a", "true", "false", "nan", "none"}
        for c in ("City", "State", "Country", "Industry"):
            val = row[c]
            low = val.lower()
            if low in garbage_words or len(val) <= 1:
                row[c] = ""
                continue
            if re.search(r'[\d@#%^&*<>{}\[\]|\\~_!$?+]', val) or re.search(r'https?://\S+|www\.\S+', val):
                row[c] = ""
                continue
            if low in ("us", "usa", "u.s.a.", "u.s.", "united states of america"):
                val = "United States"
            elif low in ("uk", "u.k.", "united kingdom"):
                val = "United Kingdom"
            row[c] = val.title()
        return row

    return df.apply(fix_row, axis=1)


def dedup_key_for(row, file_key, idx):
    parts = [str(row.get(c, "")).strip().lower() for c in DEDUP_FIELDS]
    key = "|".join(parts)
    if key == "|" * (len(DEDUP_FIELDS) - 1):
        # identity fields completely blank - treat as unique per row, never groupable
        return f"__blank__|{file_key}|{idx}"
    return key


# ------------------------------------------------------------------- scraping

def scrape_website(url: str) -> str:
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        response = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            raw_text = ' '.join(p.get_text() for p in soup.find_all(['title', 'meta', 'p', 'h1', 'h2']))
            clean_text = re.sub(r'\s+', ' ', raw_text).strip()
            clean_text = re.sub(r'[^\w\s.,!?&\-@/]', '', clean_text)
            return clean_text[:1500]
    except Exception:
        pass
    return ""


# ----------------------------------------------------------------- enrichment

def build_prompt(row, website_text, cfg):
    return f"""
You are an expert Business Intelligence AI cleaning CRM data.
Return ONLY a JSON object with EXACTLY these 4 keys: "position", "company_scale", "domain", "sub_domain".

Lead context:
Title: {row['Title']}
Employees: {row['No of Employees']} | Revenue: {row['Annual Revenue']} | Funding: {row['Total Funding']}
Industry: {row['Industry']} | Keywords: {row['Keywords']}
Website Scrape: {website_text}

Strict rules:
1. "position": pick EXACTLY one value from this list (copy it verbatim): {', '.join(cfg['positions'])}.
   ('Business Owner' is ONLY for an offline business providing physical services.)
2. "company_scale": exactly one of "Tier 1", "Tier 2", "Tier 3", "Tier 4".
   Tier 1 = large enterprise, Tier 2 = mid-market, Tier 3 = small business, Tier 4 = very small / startup.
   Judge from employees, revenue and funding.
3. "domain": pick EXACTLY one top-level key from the taxonomy below (copy it verbatim).
4. "sub_domain": pick EXACTLY one entry from the list under your chosen domain (copy it verbatim).
Taxonomy (domain -> allowed sub_domains): {cfg['taxonomy_min']}
"""


def parse_llm_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(json)?\s*|\s*```$', '', text, flags=re.DOTALL).strip()
    return json.loads(text)


def _acronym_candidate(s_low):
    """
    Safely derive a C-suite acronym from a spelled-out title, anchored to the
    literal 'Chief ... Officer' shape so it can't misfire on ordinary titles
    (e.g. 'Customer Experience Officer' is NOT turned into 'CEO').
      'Chief Marketing Officer'            -> 'cmo'
      'Chief Executive Officer & Founder'  -> 'ceo'
      'Chief Information Security Officer'  -> 'ciso'
    Returns the lowercase acronym or None.
    """
    m = re.search(r'\bchief\s+(.+?)\s+officer\b', s_low)
    if not m:
        return None
    stop = {'of', 'the', 'and', 'for', '&', '-'}
    mid = [w for w in re.split(r'[\s/&\-]+', m.group(1)) if w and w not in stop]
    if not mid:
        return None
    return 'c' + ''.join(w[0] for w in mid) + 'o'


def _closest_match(raw, options, options_lower_map, default=None):
    """
    Resolve an LLM value to the NEAREST allowed option instead of rejecting it:
      1. exact (case-insensitive)
      2. C-suite acronym  ('Chief Marketing Officer' -> 'CMO')
      3. whole-word substring, most specific  ('Executive Vice President' -> 'Vice President')
      4. fuzzy similarity (difflib)
      5. `default` bucket (e.g. 'Other'), else the single closest by ratio
    Always returns a valid option for non-empty input, so a deterministic LLM
    answer can never trap a lead in an endless validation-retry loop.
    Returns None only for empty input / empty option list.
    """
    s = str(raw).strip()
    if not s or not options:
        return None
    low = s.lower()
    if low in options_lower_map:
        return options_lower_map[low]
    ac = _acronym_candidate(low)
    if ac and ac in options_lower_map:
        return options_lower_map[ac]
    subs = [opt for opt in options
            if re.search(r'\b' + re.escape(opt.lower()) + r'\b', low)
            or re.search(r'\b' + re.escape(low) + r'\b', opt.lower())]
    if subs:
        return max(subs, key=len)          # most specific match
    close = difflib.get_close_matches(s, options, n=1, cutoff=0.6)
    if close:
        return close[0]
    if default and default.lower() in options_lower_map:
        return options_lower_map[default.lower()]
    return max(options, key=lambda o: difflib.SequenceMatcher(None, low, o.lower()).ratio())


def validate_enrichment(data, cfg):
    """
    Resolve LLM output to the closest allowed config value (never a hard reject
    on a near-miss). EnrichmentError only for a genuinely empty position/domain.
    """
    pos = _closest_match(data.get("position", ""), cfg["positions"], cfg["positions_map"], default="Other")
    if not pos:
        raise EnrichmentError(f"empty position in {data}")

    domains = list(cfg["domain_map"].values())
    dom = _closest_match(data.get("domain", ""), domains, cfg["domain_map"])
    if not dom:
        raise EnrichmentError(f"empty domain in {data}")

    sub_map = cfg["subdomain_maps"][dom]
    sub_options = list(sub_map.values())
    sub = _closest_match(data.get("sub_domain", ""), sub_options, sub_map)
    if not sub:                                    # domain had no sub-list / blank
        sub = sub_options[0] if sub_options else ""

    m = re.search(r'([1-4])', str(data.get("company_scale", "")))
    scale = f"Tier {m.group(1)}" if m else "Tier 4"   # default rather than loop

    return {"Position": pos, "Domain": dom, "Sub Domain": sub, "Company Scale": scale}


async def enrich_lead(row, user_id, router, cfg, tables, telemetry_file):
    """Scrape + LLM + strict validation. Returns the 4 enrichment fields.

    Blocking I/O (website scrape, boto3 put_item) is run in threads so that under
    concurrency one worker's 6-second scrape never freezes the whole event loop.
    """
    website_text = await asyncio.to_thread(scrape_website, row.get("Company Website", ""))
    if website_text:
        try:
            await asyncio.to_thread(tables["scrape"].put_item, Item={
                'User_ID': user_id,
                'Company Name': row.get("Company Name", ""),
                'Company Website': row.get("Company Website", ""),
                'Scraped_Text': website_text,
            })
        except Exception as e:
            print(f"[SCRAPE] WARNING: could not save scrape context for {user_id}: {e}")

    prompt = build_prompt(row, website_text, cfg)
    response_text = await router.generate_content(
        prompt=prompt, user_id=user_id,
        telemetry_file=telemetry_file, response_mime_type="application/json")
    try:
        data = parse_llm_json(response_text)
    except Exception as e:
        raise EnrichmentError(f"LLM returned unparseable JSON: {e}")
    return validate_enrichment(data, cfg)


# -------------------------------------------------------------- dynamo writes

def to_dynamo_item(row, user_id, enrichment, is_duplicate_flag):
    item = {"User_ID": user_id, "is_duplicate": is_duplicate_flag}
    merged = dict(row)
    merged.update(enrichment)
    for col in TARGET_COLUMNS:
        if col == "User_ID":
            continue
        v = str(merged.get(col, "")).strip()
        if v:
            item[col] = v
    # normalized lowercase companions powering case-insensitive person lookups:
    # Name_Search backs the NameSearch-Index GSI + partial-name scans,
    # Company_Search backs the organization filter.
    name_search = f"{item.get('First Name', '')} {item.get('Last Name', '')}".strip().lower()
    if name_search:
        item["Name_Search"] = name_search
    company_search = str(item.get("Company Name", "")).strip().lower()
    if company_search:
        item["Company_Search"] = company_search
    return item


def save_duplicate(tables, row, original_user_id, file_key, idx):
    item = {
        "User_ID": original_user_id,          # groups all copies with the original
        "Duplicate_ID": str(uuid.uuid4()),
        "Source_File": file_key,
        "Row_Index": str(idx),
        "Detected_At": datetime.datetime.now().isoformat(),
    }
    for col in TARGET_COLUMNS:
        if col == "User_ID":
            continue
        v = str(row.get(col, "")).strip()
        if v:
            item[col] = v
    tables["duplicates"].put_item(Item=item)


def mark_original_as_duplicated(tables, user_id):
    """Flip is_duplicate to Yes on the already-enriched original (if it exists)."""
    try:
        tables["cleaned"].update_item(
            Key={"User_ID": user_id},
            UpdateExpression="SET is_duplicate = :y",
            ConditionExpression="attribute_exists(User_ID)",
            ExpressionAttributeValues={":y": "Yes"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


# -------------------------------------------------------------- reconciliation

def reconcile_pending(state, tables):
    """
    Startup safety pass: verify every 'pending' lead against cleaned_data_leads.
    Fully enriched -> promote. Partial -> delete the half-row and redo. Absent -> redo.
    The lead keeps its original User_ID in every case.
    """
    pending = [(k, v) for k, v in state.leads.items() if v["s"] == "pending"]
    if not pending:
        return
    print(f"[RECONCILE] Verifying {len(pending)} pending lead(s) against cleaned_data_leads...")
    promoted = removed = 0
    for key, rec in pending:
        try:
            resp = tables["cleaned"].get_item(Key={"User_ID": rec["u"]})
        except Exception as e:
            print(f"[RECONCILE] WARNING: lookup failed for {rec['u']}: {e}")
            continue
        item = resp.get("Item")
        if item and all(str(item.get(f, "")).strip() for f in ENRICH_FIELDS):
            state.record_lead(key, rec["u"], "enriched", rec["o"])
            promoted += 1
        elif item:
            tables["cleaned"].delete_item(Key={"User_ID": rec["u"]})
            removed += 1
    print(f"[RECONCILE] Done: {promoted} promoted to enriched, {removed} partial rows deleted for redo.")


# ------------------------------------------------------- concurrent processing

async def run_file_workers(df_std, file_key, state, router, cfg, tables,
                           telemetry_file, out_of_time, max_new_leads,
                           enriched_holder, concurrency):
    """
    Process one file's rows with a pool of `concurrency` workers.

    Safety design:
    - A single locked DISPATCHER hands out rows strictly in index order and
      reserves each new lead's dedup key the instant it is handed out. So even
      though enrichment runs in parallel, duplicate detection stays exact (a
      later copy always sees the earlier one already reserved).
    - Google is never hammered: every LLM call still goes through the router's
      per-model pacing gate, which serialises calls to each model to one per
      (60/rpm) seconds regardless of how many workers are waiting. Extra workers
      simply queue at the gate - they cannot fire faster.
    - Only cheap in-memory state ops hold the lock; the slow work (scrape, LLM,
      DynamoDB writes) happens outside it, so workers truly overlap.

    Returns: 'done' (all rows finished), 'incomplete' (validation retries left),
    or 'halt' (quota/time/max reached -> stop the whole run).
    """
    total = len(df_std)
    lock = asyncio.Lock()
    shared = {"idx": 0, "halt": None, "pending_left": False}

    async def take_next():
        """Locked, in-order dispatch. Returns a task dict, or None when done/halted."""
        async with lock:
            while True:
                if shared["halt"]:
                    return None
                i = shared["idx"]
                if i >= total:
                    return None
                if out_of_time():
                    shared["halt"] = "time"
                    return None
                shared["idx"] = i + 1
                row = df_std.iloc[i].to_dict()
                key = dedup_key_for(row, file_key, i)
                origin = f"{file_key}:{i}"
                rec = state.leads.get(key)

                if rec and rec["o"] != origin:
                    # extra copy of a lead seen elsewhere -> duplicate table
                    if origin not in state.dup_origins:
                        need_mark = rec["s"] == "enriched"
                        state.record_dup(key, origin)      # reserve under lock
                        return {"type": "dup", "row": row, "uid": rec["u"],
                                "idx": i, "need_mark": need_mark}
                    continue                                # already recorded, skip

                if rec and rec["s"] == "enriched":
                    continue                                # already fully processed

                # new lead (or a pending one being retried) -> reserve + assign
                user_id = rec["u"] if rec else str(uuid.uuid4())
                if not rec:
                    state.record_lead(key, user_id, "pending", origin)
                return {"type": "enrich", "row": row, "key": key,
                        "origin": origin, "uid": user_id, "idx": i}

    async def worker():
        while True:
            task = await take_next()
            if task is None:
                return

            if task["type"] == "dup":
                await asyncio.to_thread(save_duplicate, tables, task["row"],
                                        task["uid"], file_key, task["idx"])
                if task["need_mark"]:
                    await asyncio.to_thread(mark_original_as_duplicated, tables, task["uid"])
                continue

            try:
                enrichment = await enrich_lead(task["row"], task["uid"], router,
                                               cfg, tables, telemetry_file)
            except QuotaExhaustedAllModels:
                async with lock:
                    shared["halt"] = "quota"
                return
            except EnrichmentError as e:
                print(f"[ENRICH] Row {task['idx']} ({task['uid']}) failed validation, "
                      f"will retry next run: {e}")
                async with lock:
                    shared["pending_left"] = True
                continue

            is_dup_flag = "Yes" if task["key"] in state.dup_keys else "No"
            await asyncio.to_thread(
                tables["cleaned"].put_item,
                Item=to_dynamo_item(task["row"], task["uid"], enrichment, is_dup_flag))

            async with lock:
                state.record_lead(task["key"], task["uid"], "enriched", task["origin"])
                enriched_holder["n"] += 1
                n = enriched_holder["n"]
                if n % 25 == 0:
                    print(f"[PIPELINE] enriched this run: {n} (row ~{task['idx'] + 1}/{total})")
                if max_new_leads and n >= max_new_leads:
                    shared["halt"] = "max"
                    return

    await asyncio.gather(*[asyncio.create_task(worker()) for _ in range(concurrency)])

    if shared["halt"] in ("quota", "time", "max"):
        return "halt"
    return "incomplete" if shared["pending_left"] else "done"


# ------------------------------------------------------------------ discovery

def discover_files(s3, bucket, raw_dir, local_mode):
    """Returns ordered [(file_key, etag, local_path_or_None)]."""
    found = []
    if local_mode:
        for path in sorted(glob.glob(os.path.join(raw_dir, "*.csv"))):
            if path.endswith(".standardized.csv"):
                continue  # our own cache files are never inputs
            st = os.stat(path)
            found.append((os.path.basename(path), f"local-{st.st_size}-{int(st.st_mtime)}", path))
    else:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix="raw/")
        for obj in sorted(resp.get("Contents", []), key=lambda o: o["Key"]):
            if obj["Key"].endswith(".csv"):
                found.append((obj["Key"], obj.get("ETag", "").strip('"'), None))
    return found


def stream_raw_to_dynamo(tables, df_raw, file_key, reverse_map, fstate, state, out_of_time):
    """
    Part 1: pure replica upload. Cell values are untouched; only the column
    HEADERS are renamed to the standard names (so the raw search API can query
    Title/Industry/Keywords/City/State/Country) and a row-level UUID is added.

    Resumable: 'raw_rows_done' bookmarks how many rows are committed. On restart
    we skip straight past them, so an interruption (timeout/crash/redeploy) costs
    at most the last RAW_CHECKPOINT_EVERY rows - never a re-stream from row 0.

    Returns True if the whole file was streamed, False if it paused on the time
    budget (bookmark saved; the next run resumes exactly here).
    """
    df_renamed = df_raw.rename(
        columns={c: reverse_map[str(c).lower().strip()]
                 for c in df_raw.columns if str(c).lower().strip() in reverse_map})
    total = len(df_renamed)
    start_idx = int(fstate.get("raw_rows_done", 0))
    if start_idx >= total:
        return True  # already fully streamed in a previous run

    print(f"[RAW] Streaming rows {start_idx}->{total} of '{file_key}' to raw_data_leads...")
    done = start_idx
    with tables["raw"].batch_writer() as batch:
        for rec in df_renamed.iloc[start_idx:].to_dict(orient="records"):
            item = {"User_ID": str(uuid.uuid4())}
            for k, v in rec.items():
                v = str(v).strip()
                if v and v.lower() != "nan":
                    item[str(k)] = v
            batch.put_item(Item=item)
            done += 1
            # RAW_CHECKPOINT_EVERY is a multiple of the batch flush size, so at
            # this point the buffer is flushed and 'done' == rows in DynamoDB.
            if done % RAW_CHECKPOINT_EVERY == 0:
                fstate["raw_rows_done"] = done
                state.save_files_state()
                print(f"[RAW]   {done}/{total} rows streamed (bookmark saved).")
                if out_of_time():
                    print(f"[RAW]   Soft time budget reached at row {done}; resuming here next run.")
                    return False

    fstate["raw_rows_done"] = total
    print(f"[RAW] Done: {total} rows in raw_data_leads.")
    return True


# ------------------------------------------------------------------- pipeline

async def run_pipeline(data_dir, script_dir, max_new_leads=None, local_raw_dir=None):
    env = os.environ
    cfg = load_configs(script_dir)
    tables = build_aws(env)

    local_mode = bool(local_raw_dir)
    s3_bucket = None if local_mode else env.get("AWS_S3_RAW_BUCKET_NAME")
    raw_cache = local_raw_dir if local_mode else os.path.join(data_dir, "raw_cache")
    os.makedirs(raw_cache, exist_ok=True)

    state = StateManager(data_dir, tables["s3"] if s3_bucket else None, s3_bucket)
    state.load()
    router = SmartLLMRouter(os.path.join(script_dir, "configs", "model_limits.json"),
                            state_dir=state.state_dir)
    telemetry_file = os.path.join(data_dir, "ai_ops_telemetry.csv")

    reconcile_pending(state, tables)

    # ---- file discovery. PIPELINE_FILE_DISCOVERY=daily lists the S3 bucket
    # only on the first run of each day; the other 23 hourly runs reuse the
    # cached list. Default (hourly) lists on every run.
    discovery_mode = os.environ.get("PIPELINE_FILE_DISCOVERY", "hourly").lower()
    today = datetime.date.today().isoformat()
    cached = state.files.get("discovery", {})
    if (not local_mode and discovery_mode == "daily"
            and cached.get("date") == today and cached.get("files")):
        files = [(k, e, None) for k, e in cached["files"]]
        print(f"[S3] Daily discovery mode: reusing today's cached file list ({len(files)} file(s)).")
    else:
        try:
            files = discover_files(tables["s3"], s3_bucket, raw_cache, local_mode)
        except Exception as e:
            print(f"[S3] ERROR: could not list bucket: {e}")
            return
        if not local_mode:
            state.files["discovery"] = {"date": today, "files": [[k, e] for k, e, _ in files]}
            state.save_files_state()
    if not files:
        print("[PIPELINE] No CSV files found. Nothing to do.")
        return

    # ---- soft time budget: stop cleanly before the next cron fires so a run
    # is never killed mid-row by the hard container timeout.
    run_start = time.time()
    budget = os.environ.get("MAX_RUN_SECONDS")
    budget = int(budget) if budget else None

    def out_of_time():
        return budget is not None and (time.time() - run_start) > budget

    # ---- concurrency: how many leads to enrich in parallel. Bounded per active
    # model (more than ~3x models just idles at the pacing gate). Google is not
    # hammered - the router's per-model pace still spaces every call.
    concurrency = int(os.environ.get("PIPELINE_CONCURRENCY", "0")) or max(3, 3 * len(router.models))
    concurrency = min(concurrency, 30)
    # give asyncio.to_thread enough threads for all workers' blocking I/O
    import concurrent.futures
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=concurrency * 2 + 4))
    print(f"[PIPELINE] Concurrency: {concurrency} parallel workers over {len(router.models)} model(s).")

    enriched_holder = {"n": 0}
    try:
        for file_key, etag, local_path in files:
            fstate = state.files["files"].get(file_key, {})

            # a CSV whose exact content was fully processed before (even under
            # another name) must never be re-processed
            if fstate.get("etag") == etag and fstate.get("part2_done"):
                print(f"[PIPELINE] '{file_key}' already fully processed. Skipping.")
                continue
            if etag in state.files["done_etags"] and fstate.get("etag") != etag:
                print(f"[PIPELINE] '{file_key}' has identical content to an already-processed file. Skipping.")
                continue

            if fstate.get("etag") != etag:
                # brand-new file (or changed content): reset its progress
                fstate = {"etag": etag, "raw_streamed": False, "raw_rows_done": 0, "part2_done": False}
                state.files["files"][file_key] = fstate
                state.save_files_state()

            # ---- fetch csv
            if local_mode:
                csv_path = local_path
            else:
                csv_path = os.path.join(raw_cache, etag.replace("/", "_") + "_" + os.path.basename(file_key))
                if not os.path.exists(csv_path):
                    print(f"[S3] Downloading '{file_key}'...")
                    tables["s3"].download_file(s3_bucket, file_key, csv_path)

            df_raw = pd.read_csv(csv_path, dtype=str, keep_default_na=False, low_memory=False)
            print(f"\n[PIPELINE] === '{file_key}': {len(df_raw)} rows ===")

            # ---- Part 1: raw replica (once per file, resumable)
            if not fstate.get("raw_streamed"):
                completed = stream_raw_to_dynamo(
                    tables, df_raw, file_key, cfg["reverse_map"], fstate, state, out_of_time)
                if not completed:
                    # paused on the time budget mid-stream; bookmark is saved,
                    # the next run resumes exactly where it stopped
                    state.save_files_state()
                    return
                fstate["raw_streamed"] = True
                state.save_files_state()
                state.backup_to_s3()

            # ---- Part 2A: standardize (cached per file version so the heavy
            # row cleanup runs once per ETag, not on all 24 hourly runs)
            std_cache = csv_path + ".standardized.csv"
            df_std = None
            if os.path.exists(std_cache):
                df_std = pd.read_csv(std_cache, dtype=str, keep_default_na=False, low_memory=False)
                if len(df_std) == len(df_raw) and list(df_std.columns) == TARGET_COLUMNS:
                    print(f"[PIPELINE] Loaded standardized cache ({len(df_std)} rows).")
                else:
                    df_std = None  # stale/corrupt cache -> rebuild
            if df_std is None:
                print("[PIPELINE] Standardizing rows (Part 2A)...")
                df_std = standardize_dataframe(df_raw, cfg["reverse_map"])
                try:
                    df_std.to_csv(std_cache, index=False)
                except Exception as e:
                    print(f"[PIPELINE] WARNING: could not write standardized cache: {e}")

            # ---- Part 2B/2C: concurrent processing with in-order dispatch
            status = await run_file_workers(
                df_std, file_key, state, router, cfg, tables, telemetry_file,
                out_of_time, max_new_leads, enriched_holder, concurrency)

            if status == "halt":
                # quota exhausted / time budget / max_new_leads -> stop the run
                return
            if status == "done":
                fstate["part2_done"] = True
                if etag not in state.files["done_etags"]:
                    state.files["done_etags"].append(etag)
                state.save_files_state()
                print(f"[PIPELINE] '{file_key}' fully processed [DONE]")
            # status == "incomplete": validation retries remain; retry next run

    except QuotaExhaustedAllModels as e:
        print(f"[PIPELINE] {e}")
        print("[PIPELINE] Suspending until the next run; progress is saved.")
    finally:
        state.backup_to_s3()
        print(f"[PIPELINE] Run finished. Newly enriched this run: {enriched_holder['n']}. State backed up.")


# --------------------------------------------------------------- entry points

def run_orchestrator_in_cloud(data_dir, script_dir):
    """Kept for modal_app.py compatibility (cloud deployment happens later)."""
    max_rows = os.environ.get("MAX_NEW_LEADS")
    asyncio.run(run_pipeline(data_dir, script_dir,
                             max_new_leads=int(max_rows) if max_rows else None))


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.environ.get("PIPELINE_DATA_DIR",
                              os.path.join(os.path.dirname(script_dir), "pipeline_data"))
    os.makedirs(data_dir, exist_ok=True)
    max_rows = os.environ.get("MAX_NEW_LEADS")
    local_dir = os.environ.get("PIPELINE_LOCAL_RAW_DIR")  # set to bypass S3 (testing)
    asyncio.run(run_pipeline(
        data_dir, script_dir,
        max_new_leads=int(max_rows) if max_rows else None,
        local_raw_dir=local_dir,
    ))
