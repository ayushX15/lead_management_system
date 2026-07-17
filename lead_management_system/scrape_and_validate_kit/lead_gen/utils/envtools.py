"""
Environment loading for the archived lead_gen system.

Precedence (first hit wins; values already in the process env are never
overridden):
  1. scrape_and_validate_kit/.env               - this system's own env file
  2. ../lead_enrichment_system/.env          - fallback, so when both projects
                                            live in the same workspace the
                                            archived system reuses the active
                                            pipeline's credentials

Also centralizes the output directory (GEN_OUTPUT_DIR) so the CSVs and every
state file (history.json, search_state.json) live in one place and tests can
redirect them with a single env var.
"""

import os

from dotenv import load_dotenv

ARCHIVED_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKSPACE_ROOT = os.path.dirname(ARCHIVED_ROOT)


def load_env():
    load_dotenv(os.path.join(ARCHIVED_ROOT, ".env"))
    load_dotenv(os.path.join(WORKSPACE_ROOT, "lead_enrichment_system", ".env"))


def output_dir() -> str:
    """Absolute output folder for CSVs + run state. Created on first use."""
    load_env()
    rel = os.getenv("GEN_OUTPUT_DIR", "lead_gen/output")
    path = rel if os.path.isabs(rel) else os.path.join(ARCHIVED_ROOT, rel)
    os.makedirs(path, exist_ok=True)
    return path
