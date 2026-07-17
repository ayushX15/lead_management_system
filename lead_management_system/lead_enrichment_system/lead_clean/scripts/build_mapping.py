"""
Build a two-way People <-> Organization mapping from CSV chunks in S3.

Upload convention (person just drops chunks here):
    s3://<LEADS_MAPPING_BUCKET>/people/*.csv
    s3://<LEADS_MAPPING_BUCKET>/organization/*.csv

The script builds and INCREMENTALLY maintains two layers:
    Layer 1  person_map.json : person_id -> {name,title,email,linkedin,location, organization_ids}
    Layer 2  org_map.json    : org_id    -> {name,industry,website,employees_claimed, people_ids}
                                            (people_ids = the reverse index the raw CSVs lack)

Efficiency & correctness:
    - ETag tracking: a chunk whose (key,etag) was already merged is SKIPPED.
    - Idempotent merge: maps are keyed by id and people_ids is a SET, so re-runs
      never duplicate an org, a person, or a person inside the same org.
    - Incremental: new chunks add new orgs/people and extend existing orgs'
      employee lists; nothing is rebuilt from scratch.
    - Effective employee count = max(employees_claimed, #linked people), so when
      the people we actually have exceed the org's stated headcount, the count grows.

State (maps + processed-etags) lives in S3 under mapping/ and is mirrored locally
(mapping_data/) for the Streamlit UI.

Usage:
    python lead_clean/scripts/build_mapping.py            # read from S3 (default)
    python lead_clean/scripts/build_mapping.py --local "lead_clean/CSV DATA"  # dev/test
"""

import os
import csv
import ast
import json
import glob
import argparse

import boto3
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))
csv.field_size_limit(10 * 1024 * 1024)

PEOPLE_PREFIX = "people/"
ORG_PREFIX = "organization/"
MAPPING_PREFIX = "mapping/"
LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapping_data")


# --------------------------------------------------------------- field parsing

def clean_list_str(v):
    v = (v or "").strip()
    if v.startswith("["):
        try:
            items = ast.literal_eval(v)
            if isinstance(items, list):
                return ", ".join(str(x) for x in items if str(x).strip())
        except Exception:
            pass
    return v


def org_ids_of(v):
    """All organization ids a person belongs to (list, primary first)."""
    v = (v or "").strip()
    if v.startswith("["):
        try:
            ids = ast.literal_eval(v)
            if isinstance(ids, list):
                return [str(x).strip() for x in ids if str(x).strip()]
        except Exception:
            pass
    v = v.strip("[]'\" ")
    return [v] if v else []


def to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except Exception:
        return 0


# --------------------------------------------------------------------- storage

class Store:
    """Reads the People/Organization CSV chunks and persists the maps - from S3 or a local folder."""

    def __init__(self, local_dir=None):
        self.local = local_dir
        if not local_dir:
            self.bucket = os.environ["LEADS_MAPPING_BUCKET"]
            self.s3 = boto3.client(
                "s3",
                aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            )
        os.makedirs(LOCAL_DIR, exist_ok=True)

    def list_chunks(self, kind):
        """kind in {'people','organization'} -> [(key, etag)]."""
        if self.local:
            folder = os.path.join(self.local, "People" if kind == "people" else "Organization")
            out = []
            for p in sorted(glob.glob(os.path.join(folder, "*.csv"))):
                st = os.stat(p)
                out.append((p, f"local-{st.st_size}-{int(st.st_mtime)}"))
            return out
        prefix = PEOPLE_PREFIX if kind == "people" else ORG_PREFIX
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return sorted((o["Key"], o.get("ETag", "").strip('"'))
                      for o in resp.get("Contents", []) if o["Key"].endswith(".csv"))

    def open_chunk(self, key):
        if self.local:
            return open(key, encoding="utf-8", newline="")
        local_tmp = os.path.join(LOCAL_DIR, "_dl_" + os.path.basename(key))
        self.s3.download_file(self.bucket, key, local_tmp)
        return open(local_tmp, encoding="utf-8", newline="")

    def load_state(self):
        for fn in ("person_map.json", "org_map.json", "processed_etags.json"):
            local_path = os.path.join(LOCAL_DIR, fn)
            if not self.local and not os.path.exists(local_path):
                try:
                    self.s3.download_file(self.bucket, MAPPING_PREFIX + fn, local_path)
                except Exception:
                    pass
        def read(fn, default):
            p = os.path.join(LOCAL_DIR, fn)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            return default
        return read("person_map.json", {}), read("org_map.json", {}), read("processed_etags.json", {})

    def save_state(self, person_map, org_map, etags):
        for fn, obj in (("person_map.json", person_map),
                        ("org_map.json", org_map),
                        ("processed_etags.json", etags)):
            p = os.path.join(LOCAL_DIR, fn)
            with open(p, "w", encoding="utf-8") as f:
                # indented + real unicode so the files are human-readable;
                # json.load reads this identically, so nothing else changes
                json.dump(obj, f, indent=2, ensure_ascii=False)
            if not self.local:
                self.s3.upload_file(p, self.bucket, MAPPING_PREFIX + fn)


# ------------------------------------------------------------------- merging

def merge_org_chunk(fh, org_map):
    added = 0
    for row in csv.DictReader(fh):
        oid = (row.get("organization_id") or "").strip()
        if not oid:
            continue
        entry = org_map.get(oid)
        if entry is None:
            entry = {"name": "", "industry": "", "website": "",
                     "employees_claimed": 0, "people_ids": [], "from_people_only": False}
            org_map[oid] = entry
            added += 1
        entry["name"] = row.get("organization_name", "") or entry["name"]
        entry["industry"] = clean_list_str(row.get("organization_industries", "")) or entry["industry"]
        entry["website"] = (row.get("organization_website_url", "")
                            or row.get("organization_domain", "") or entry["website"])
        entry["employees_claimed"] = to_int(row.get("organization_num_current_employees", "")) or entry["employees_claimed"]
        entry["from_people_only"] = False
        
        # New columns requested
        entry["revenue"] = to_int(row.get("organization_revenue_in_thousands_int", "")) or entry.get("revenue", 0)
        entry["total_funding"] = to_int(row.get("organization_total_funding_long", "")) or entry.get("total_funding", 0)
        entry["latest_funding"] = to_int(row.get("organization_latest_funding_round_amount_long", "")) or entry.get("latest_funding", 0)
        entry["founded_year"] = to_int(row.get("organization_founded_year", "")) or entry.get("founded_year", 0)
    return added


def merge_people_chunk(fh, person_map, org_map):
    added = 0
    for row in csv.DictReader(fh):
        pid = (row.get("_id") or "").strip()
        if not pid:
            continue
        oids = org_ids_of(row.get("current_organization_ids", ""))
        if pid not in person_map:
            added += 1
        person_map[pid] = {
            "name": row.get("person_name", ""),
            "title": row.get("person_title", ""),
            "email": row.get("person_email", ""),
            "linkedin": row.get("person_linkedin_url", ""),
            "location": row.get("person_location_city_with_state_or_country", "")
                        or row.get("person_location_country", ""),
            "organization_ids": oids,
        }
        # ensure a stub org exists for ids not present in the organization file,
        # so no person is ever dropped (reverse index is filled in rebuild step)
        for oid in oids:
            if oid not in org_map:
                org_map[oid] = {
                    "name": row.get("sanitized_organization_name_unanalyzed", "") or "(unknown)",
                    "industry": "", "website": "", "employees_claimed": 0,
                    "people_ids": [], "from_people_only": True,
                    "revenue": 0, "total_funding": 0, "latest_funding": 0,
                    "founded_year": 0
                }
    return added


def rebuild_people_sets(person_map, org_map):
    """Recompute people_ids from person_map so it's always consistent & de-duplicated."""
    for entry in org_map.values():
        entry["people_ids"] = set()
    for pid, p in person_map.items():
        for oid in p.get("organization_ids", []):
            if oid in org_map:
                org_map[oid]["people_ids"].add(pid)
    for entry in org_map.values():
        entry["people_ids"] = sorted(entry["people_ids"])


# ---------------------------------------------------------------------- driver

def run(local_dir=None):
    store = Store(local_dir)
    person_map, org_map, etags = store.load_state()
    print(f"[MAP] Loaded existing state: {len(person_map)} people, {len(org_map)} orgs.")

    # Process organizations first so people can attach to real org records.
    for kind in ("organization", "people"):
        for key, etag in store.list_chunks(kind):
            sig = f"{kind}/{os.path.basename(key)}"
            if etags.get(sig) == etag:
                print(f"[MAP] skip (already merged): {sig}")
                continue
            print(f"[MAP] merging {sig} ...")
            with store.open_chunk(key) as fh:
                if kind == "organization":
                    merge_org_chunk(fh, org_map)
                else:
                    merge_people_chunk(fh, person_map, org_map)
            etags[sig] = etag

    # Rebuild reverse index from the authoritative person_map (idempotent).
    rebuild_people_sets(person_map, org_map)

    store.save_state(person_map, org_map, etags)

    total_linked = sum(len(o["people_ids"]) for o in org_map.values())
    print("\n========== MAPPING SUMMARY ==========")
    print(f"Unique organizations : {len(org_map)}")
    print(f"Unique people        : {len(person_map)}")
    print(f"Total person<->org links: {total_linked}")
    print(f"Orgs known only from people (not in org file): "
          f"{sum(1 for o in org_map.values() if o.get('from_people_only'))}")
    print(f"Maps saved to: {LOCAL_DIR}" + ("" if local_dir else f"  and s3://{store.bucket}/{MAPPING_PREFIX}"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", default=None, help="read chunks from a local 'CSV DATA' folder instead of S3")
    args = ap.parse_args()
    run(local_dir=args.local)
