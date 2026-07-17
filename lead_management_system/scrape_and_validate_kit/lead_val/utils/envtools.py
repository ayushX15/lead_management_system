"""
Environment loading for the archived lead_val system.

Precedence (first hit wins; values already in the process env are never
overridden):
  1. scrape_and_validate_kit/.env               - this system's own env file
  2. ../lead_enrichment_system/.env          - fallback, so when both projects
                                            live in the same workspace the
                                            archived system reuses the active
                                            pipeline's credentials
"""

import os

from dotenv import load_dotenv

ARCHIVED_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKSPACE_ROOT = os.path.dirname(ARCHIVED_ROOT)


def load_env():
    load_dotenv(os.path.join(ARCHIVED_ROOT, ".env"))
    load_dotenv(os.path.join(WORKSPACE_ROOT, "lead_enrichment_system", ".env"))
