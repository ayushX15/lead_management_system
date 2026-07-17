"""Single source of truth for a lead's dedup identity.

Used by DedupFilterAgent AND WriterAgent so the two can never disagree on
what counts as "the same business" (the old code re-implemented this hash in
three places).
"""

import hashlib


def business_id(raw_name: str, city: str, domain: str) -> str:
    composite = f"{raw_name}{city}{domain}".strip().lower()
    return hashlib.md5(composite.encode("utf-8")).hexdigest()
