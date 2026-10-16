"""Map tracking CNR → scraper courtKey (HC/SC contract)."""

from __future__ import annotations

# Prefix / alias → courtKey used by CauseList.courtKey
_CNR_PREFIX_TO_COURT: dict[str, str] = {
    "dlhc": "dlhc",
    "bmhc": "bmhc",
    "hcbm": "bmhc",
    "bhc": "bmhc",
    "tshc": "tshc",
    "tghc": "tshc",
    "orhc": "orhc",
    "ohc": "orhc",
    "mphc": "mphc",
    "sci": "sci",
}


def scrape_court_id_from_cnr(cnr: str | None) -> str | None:
    """Return scraper courtKey for a CNR, or None if unknown."""
    normalized = (cnr or "").strip().upper()
    if len(normalized) < 2:
        return None
    for length in (4, 6, 3):
        if len(normalized) < length:
            continue
        mapped = _CNR_PREFIX_TO_COURT.get(normalized[:length].lower())
        if mapped:
            return mapped
    if normalized.startswith("SC"):
        return "sci"
    return None
