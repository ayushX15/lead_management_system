"""
Modal deployment of the Lead Clean Pipeline.

- Hourly cron (24 runs/day). Each run resumes exactly where the last stopped
  via the lead registry on the persistent volume (backed up to S3).
- max_containers=1 guarantees two runs can NEVER overlap and corrupt state.
- MAX_RUN_SECONDS=3000 makes every run stop cleanly after 50 minutes, well
  before the 60-minute hard timeout, so a run is never killed mid-row.
- PIPELINE_FILE_DISCOVERY=daily lists the S3 bucket for new CSVs only on the
  first run of each day; the other 23 runs reuse the cached list.
- All credentials (AWS + GEMINI_API_KEY) are injected at runtime from the
  local .env via modal.Secret.from_dotenv - nothing is baked into the image.
  The Vertex service key ships inside lead_clean/configs/.

Deploy:   modal deploy lead_clean/modal_app.py
Test run: modal run lead_clean/modal_app.py --max-new-leads 8
"""

import os
import modal

app = modal.App("lead-cleaner")

image = modal.Image.debian_slim().pip_install(
    "pandas",
    "google-genai",
    "google-auth",
    "beautifulsoup4",
    "requests",
    "python-dotenv",
    "pydantic",
    "boto3",
    "tzdata",
).add_local_dir(
    os.path.dirname(__file__), remote_path="/root/lead_clean"
)

volume = modal.Volume.from_name("lead-data-volume", create_if_missing=True)


@app.function(
    image=image,
    schedule=modal.Cron("0 * * * *"),          # top of every hour, 24x/day
    secrets=[modal.Secret.from_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))],
    volumes={"/data": volume},
    timeout=3600,                               # hard backstop only
    max_containers=1,                           # runs can never overlap
    memory=2048,                                # reserve 2 GB RAM floor (no OOM on busy hosts)
    cpu=2,                                       # 2 vCPUs for pandas standardization
    retries=0,
)
def hourly_lead_cleaner(max_new_leads: int = 0):
    import sys
    sys.path.append("/root/lead_clean")

    # cloud run behaviour (env can be overridden via the Modal dashboard)
    os.environ.setdefault("MAX_RUN_SECONDS", "3000")            # stop cleanly at 50 min
    os.environ.setdefault("PIPELINE_FILE_DISCOVERY", "daily")   # list S3 once per day
    if max_new_leads:
        os.environ["MAX_NEW_LEADS"] = str(max_new_leads)

    from clean_orchestrator import run_orchestrator_in_cloud

    print("[MODAL] Waking up hourly lead cleaner...")
    try:
        run_orchestrator_in_cloud(data_dir="/data", script_dir="/root/lead_clean")
    finally:
        volume.commit()   # persist registry/state/caches no matter what happened
    print("[MODAL] Run finished. Sleeping until the next hour.")


@app.local_entrypoint()
def main(max_new_leads: int = 8):
    print(f"[MODAL] Triggering a manual cloud test run (cap: {max_new_leads} new leads)...")
    hourly_lead_cleaner.remote(max_new_leads=max_new_leads)
    print("[MODAL] Manual run complete.")
