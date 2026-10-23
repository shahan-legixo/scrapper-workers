"""Legixo-aligned cause-list schema: CauseList (parent) + CauseListItem (child).

Canonical Mongo collections: ``causelists``, ``causelistitems``.
PDF URI lives on the parent as ``rawRef`` only.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# Court identity
# ---------------------------------------------------------------------------

COURT_NAMES: dict[str, str] = {
    "delhi": "Delhi High Court",
    "bombay": "Bombay High Court",
    "telangana": "Telangana High Court",
    "odisha": "Orissa High Court",
    "mphc": "High Court of Madhya Pradesh",
    "supreme": "Supreme Court of India",
}

# scraper script key → Legixo courtKey
COURT_KEYS: dict[str, str] = {
    "delhi": "dlhc",
    "bombay": "bmhc",
    "telangana": "tshc",
    "odisha": "orhc",
    "mphc": "mphc",
    "supreme": "sci",
}

COURT_LEVEL: dict[str, str] = {
    "dlhc": "HIGH_COURT",
    "bmhc": "HIGH_COURT",
    "tshc": "HIGH_COURT",
    "orhc": "HIGH_COURT",
    "mphc": "HIGH_COURT",
    "sci": "SUPREME_COURT",
}

COURT_SOURCE: dict[str, str] = {
    "dlhc": "hc_scrape",
    "bmhc": "hc_scrape",
    "tshc": "hc_scrape",
    "orhc": "hc_scrape",
    "mphc": "hc_scrape",
    "sci": "sc_scrape",
}

# Informational only — CauseList.listType is a free string (not validated here).
CAUSE_LIST_TYPES: frozenset[str] = frozenset(
    {
        "main",
        "daily",
        "supplementary",
        "advance",
        "deletion",
        "miscellaneous",
        "regular",
        "weekly",
    }
)

CAUSE_LIST_STATUSES: frozenset[str] = frozenset({"ok", "empty", "failed"})

# Parent / item field contracts (for validation helpers).
CAUSE_LIST_KEYS: tuple[str, ...] = (
    "source",
    "courtLevel",
    "courtKey",
    "hearingDate",
    "listType",
    "benchLabel",
    "edition",
    "benchType",
    "checksum",
    "status",
    "externalId",
    "rawRef",
    "contentType",
)

CAUSE_LIST_ITEM_KEYS: tuple[str, ...] = (
    "hearingDate",
    "fingerprint",
    "identities",
    "cnr",
    "listingNo",
    "listingSection",
    "purpose",
    "courtNo",
    "bench",
    "judges",
    "party",
    "payload",
)

# Display labels kept for scrapers / payload.list_type_display.
_LIST_TYPE_DISPLAY: dict[str, str] = {
    "daily": "DAILY LIST",
    "daily list": "DAILY LIST",
    "daily main": "DAILY LIST",
    "daily causelist": "DAILY LIST",
    "daily cause list": "DAILY LIST",
    "advanced": "ADVANCED LIST",
    "advanced list": "ADVANCED LIST",
    "regular": "REGULAR LIST",
    "regular list": "REGULAR LIST",
    "supplementary": "SUPPLEMENTARY CAUSE LIST",
    "supplementary list": "SUPPLEMENTARY CAUSE LIST",
    "daily supplementary": "SUPPLEMENTARY CAUSE LIST",
    "supplimentary": "SUPPLEMENTARY CAUSE LIST",
    "supplimentary list": "SUPPLEMENTARY CAUSE LIST",
    "supplimentary cause list": "SUPPLEMENTARY CAUSE LIST",
    "supplementary cause list": "SUPPLEMENTARY CAUSE LIST",
    "supplementary causelist": "SUPPLEMENTARY CAUSE LIST",
    "weekly": "WEEKLY LIST",
    "weekly daily": "WEEKLY LIST",
    "weekly list": "WEEKLY LIST",
    "motion": "MOTION LIST",
    "final": "FINAL LIST",
    "lok_adalat": "LOK ADALAT LIST",
    "lok adalat": "LOK ADALAT LIST",
    "mediation": "MEDIATION LIST",
    "chamber": "CHAMBER LIST",
    "chamber list": "CHAMBER LIST",
    "production": "PRODUCTION LIST",
    "officer": "OFFICER LIST",
    "taken on board": "TAKEN ON BOARD",
    "miscellaneous": "MISCELLANEOUS LIST",
    "miscellaneous list": "MISCELLANEOUS LIST",
    "single_judge": "SINGLE JUDGE LIST",
    "single judge": "SINGLE JUDGE LIST",
    "single judge list": "SINGLE JUDGE LIST",
    "registrar": "REGISTRAR LIST",
    "registrar list": "REGISTRAR LIST",
    "advance": "ADVANCED LIST",
    "advance list": "ADVANCED LIST",
    "deletion": "DELETION LIST",
}

# Exact aliases → stored CauseList.listType (no substring matching).
_LIST_TYPE_STORED: dict[str, str] = {
    "daily": "daily",
    "daily list": "daily",
    "daily main": "daily",
    "daily causelist": "daily",
    "daily cause list": "daily",
    "supplementary": "supplementary",
    "supplementary list": "supplementary",
    "daily supplementary": "supplementary",
    "supplimentary": "supplementary",
    "supplimentary list": "supplementary",
    "supplimentary cause list": "supplementary",
    "supplementary cause list": "supplementary",
    "supplementary causelist": "supplementary",
    "advance": "advance",
    "advance list": "advance",
    "advanced": "advance",
    "advanced list": "advance",
    "deletion": "deletion",
    "deletion list": "deletion",
    "miscellaneous": "miscellaneous",
    "miscellaneous list": "miscellaneous",
    "regular": "regular",
    "regular list": "regular",
    "weekly": "weekly",
    "weekly list": "weekly",
    "weekly daily": "weekly",
    # Legacy / default token when label missing.
    "main": "main",
}

_SPLIT_RE = re.compile(r"\s*\|\s*")
_WITH_RE = re.compile(r"^(?:WITH|with)\s+")
# WP/2166/2009
_SLASH_CASE_RE = re.compile(r"^(.+?)/(\d+)/(\d{4})$")
# WP-2166/2009 (no spaces around first dash)
_DASH_CASE_RE = re.compile(r"^(.+?)-(\d+)/(\d{4})$")
# CO.PET. - 153/1998 | CO.APPL. 1804/2017 | COMP - 7/2008 | WP - 19459/2016 (S)
_SPACED_CASE_RE = re.compile(
    r"^(.+?)\s*[-–]?\s+(\d+)\s*/\s*(\d{4})\b",
)
# C.A. No. 329-330/2004 | W.P.(C) No. 180/2004 | SLP(C) No. 6431-6432/2017
_SCI_NO_CASE_RE = re.compile(
    r"^(.+?)\s+No\.?\s+(\d+)(?:\s*[-–]\s*\d+)?\s*/\s*(\d{4})\b",
    re.I,
)
_DIARY_RE = re.compile(
    r"(?:Diary|Dy\.?)\s*(?:No\.?|Number)?\s*[:\-]?\s*(\d+\s*[/\-]\s*\d{4})",
    re.I,
)
# Trailing junk after the year token (Connected notes, filing tags, district notes).
_CASE_TRAILING_RE = re.compile(
    r"(?:/\(?\s*Filing\s+No\.?\s*\)?|\s+\d+\.\s*Connected.*)$",
    re.I,
)
# MPHC / SCI notes after the year: keep through YYYY, drop the rest.
_AFTER_YEAR_JUNK_RE = re.compile(r"(?<=\d{4})\s+.*$")

# Legacy alias — flat intermediate keys scrapers still produce via make_case.
COMMON_CASE_KEYS: tuple[str, ...] = (
    "date",
    "courtName",
    "courtNo",
    "listingNo",
    "caseNumber",
    "party",
    "judge",
    "list_type",
    "section",
    "remark",
    "advocates",
)


def court_name(script_key: str) -> str:
    """Return the display courtName for a scraper key."""
    return COURT_NAMES[script_key]


def resolve_court_key(court: str) -> str:
    """Map scraper / alias court string to Legixo courtKey."""
    key = court.strip().lower().replace("_", "-")
    aliases = {
        "dlhc": "dlhc",
        "delhi": "dlhc",
        "bmhc": "bmhc",
        "bmx": "bmhc",
        "bhc": "bmhc",
        "bombay": "bmhc",
        "mphc": "mphc",
        "mp": "mphc",
        "tshc": "tshc",
        "tghc": "tshc",
        "telangana": "tshc",
        "orhc": "orhc",
        "ohc": "orhc",
        "odisha": "orhc",
        "orissa": "orhc",
        "sci": "sci",
        "scic": "sci",
        "supreme": "sci",
    }
    if key in aliases:
        return aliases[key]
    if key in COURT_KEYS:
        return COURT_KEYS[key]
    return key


def script_key_from_court_key(court_key: str) -> str:
    """Inverse of COURT_KEYS for collectors that still use delhi/bombay/..."""
    for script, ck in COURT_KEYS.items():
        if ck == court_key:
            return script
    return court_key


def normalize_list_type(raw: str | None, *, default: str = "DAILY LIST") -> str:
    """Normalize a court-native list label to the shared display vocabulary."""
    if not raw:
        return default
    text = re.sub(r"\s+", " ", str(raw).replace("\xa0", " ")).strip()
    if not text:
        return default
    key = text.casefold()
    if key in _LIST_TYPE_DISPLAY:
        return _LIST_TYPE_DISPLAY[key]
    for needle, label in _LIST_TYPE_DISPLAY.items():
        if key.startswith(needle) or needle in key:
            return label
    return text.upper()


def normalize_list_type_enum(raw: str | None, *, default: str = "main") -> str:
    """Produce the **stored** CauseList.listType string (free string).

    Exact alias match only — no substring loop (so ``daily supplementary``
    stays ``supplementary``, not ``daily``). Unknown labels → uppercased text.
    Missing → ``main``.
    """
    if not raw:
        return default
    text = re.sub(r"\s+", " ", str(raw).replace("\xa0", " ")).strip()
    if not text:
        return default
    folded = text.casefold()
    if folded in _LIST_TYPE_STORED:
        return _LIST_TYPE_STORED[folded]
    return text.upper()


def _identity_seg(value: str | None) -> str:
    """externalId / path segment: strip; never allow ``:``."""
    return (value or "").strip().replace(":", "_")


def split_case_numbers(raw: str | list[str] | None) -> list[str]:
    """Normalize a case-number cell into a list of non-empty strings."""
    if not raw:
        return []
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            out.extend(split_case_numbers(str(item) if item is not None else None))
        return out
    text = re.sub(r"\s+", " ", str(raw).replace("\xa0", " ")).strip()
    if not text:
        return []
    parts: list[str] = []
    for chunk in _SPLIT_RE.split(text):
        piece = _WITH_RE.sub("", chunk.strip()).strip()
        if piece and piece not in parts:
            parts.append(piece)
    return parts


def as_string_list(value: Any) -> list[str]:
    """Coerce a string / list value into ``list[str]`` (deduped, trimmed)."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "," in text:
            parts = [p.strip(" ,;") for p in text.split(",")]
            return [p for p in parts if p]
        return [text]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            for piece in as_string_list(item):
                if piece not in out:
                    out.append(piece)
        return out
    text = str(value).strip()
    return [text] if text else []


def as_judge_list(value: Any) -> list[str]:
    """Coerce judge / judges into ``list[str]``."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            text = str(item).strip() if item is not None else ""
            if text and text not in out:
                out.append(text)
        return out
    text = str(value).strip()
    return [text] if text else []


def parse_listing_no(value: Any) -> int | None:
    """Coerce listing number to int; omit if not numeric."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    match = re.match(r"^(\d+)", text)
    if match:
        return int(match.group(1))
    return None


def _normalize_case_type(raw: str) -> str:
    """Store the printed type the way tracking does after parse.

    Uppercase, drop spaces, drop a trailing hyphen. Keep dots
    (``CO.PET.`` stays ``CO.PET.``, ``W.P.(C)`` stays ``W.P.(C)``).
    """
    text = re.sub(r"\s+", "", raw).upper()
    return text.rstrip("-–").strip()


def parse_printed_case_number(
    printed: str,
) -> dict[str, str]:
    """Split printed case strings into type / root / year.

    Handles High Court and SCI forms, e.g.::

        WP/2166/2009
        WP-2166/2009
        CO.PET. - 153/1998
        CO.APPL. 1804/2017
        COMP - 7/2008
        WP - 19459/2016 (S)
        C.A. No. 329-330/2004 101. Connected
        W.P.(C) No. 180/2004
    """
    normalized = printed.strip().replace("\xa0", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    # Cut filing / connected suffixes, then any notes after the year.
    clipped = _CASE_TRAILING_RE.sub("", normalized).strip()
    clipped = _AFTER_YEAR_JUNK_RE.sub("", clipped).strip()

    for pattern in (
        _SCI_NO_CASE_RE,
        _SLASH_CASE_RE,
        _DASH_CASE_RE,
        _SPACED_CASE_RE,
    ):
        match = pattern.match(clipped) or pattern.match(normalized)
        if match:
            case_type = _normalize_case_type(match.group(1))
            # Drop a glued "No." label. Dots that belong to the type stay.
            if case_type.endswith("NO."):
                case_type = case_type[:-3]
            elif case_type.endswith("NO"):
                case_type = case_type[:-2]
            if not case_type:
                continue
            return {
                "caseType": case_type,
                "rootCaseNumber": match.group(2),
                "caseYear": match.group(3),
            }
    return {}


def parse_case_identities(
    case_numbers: list[str] | str | None,
    *,
    cnr: str | None = None,
    diary_no: str | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    """Build Legixo ``identities`` from printed case numbers (+ optional CNR/diary)."""
    numbers = (
        split_case_numbers(case_numbers)
        if not isinstance(case_numbers, list)
        else [str(x).strip() for x in case_numbers if x is not None and str(x).strip()]
    )
    identities: dict[str, Any] = {}
    if cnr and str(cnr).strip():
        identities["cnr"] = str(cnr).strip().upper()
    if numbers:
        identities["caseNumber"] = numbers[0]
        identities["caseNumbers"] = numbers
        identities.update(parse_printed_case_number(numbers[0]))
    diary = (diary_no or "").strip() or None
    if not diary and remark:
        match = _DIARY_RE.search(str(remark))
        if match:
            diary = re.sub(r"\s+", "", match.group(1))
    if not diary:
        for printed in numbers:
            match = _DIARY_RE.search(printed)
            if match:
                diary = re.sub(r"\s+", "", match.group(1))
                break
    if diary:
        identities["diaryNo"] = diary
    return identities


def stable_serialize(value: Any) -> str:
    """Deterministic JSON for hashing (sorted object keys; array order kept)."""
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(stable_serialize(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value.keys())
        return (
            "{"
            + ",".join(
                f"{json.dumps(k, ensure_ascii=False)}:{stable_serialize(value[k])}"
                for k in keys
            )
            + "}"
        )
    return json.dumps(str(value), ensure_ascii=False)


def sha256_hex(material: Any) -> str:
    return hashlib.sha256(stable_serialize(material).encode("utf-8")).hexdigest()


def compute_checksum(raw: bytes | str | Any) -> str:
    """SHA-256 of bytes/string, or of stable JSON for structured values.

    Prefer :func:`compute_parsed_unit_checksum` for CauseList.checksum (parsed fields).
    """
    if isinstance(raw, (bytes, bytearray)):
        return hashlib.sha256(raw).hexdigest()
    if isinstance(raw, str):
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return sha256_hex(raw)


# Volatile / download-only keys — not part of parsed listing content.
_PARSED_PAYLOAD_SKIP = frozenset(
    {"source_url", "gcs_uri", "rawRef", "pdf_path", "local_path"}
)


def parsed_fields_checksum_material(item: dict[str, Any]) -> dict[str, Any]:
    """One CauseListItem's parse-derived fields for unit checksum (stable keys)."""
    payload_in = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    payload = {
        k: v
        for k, v in payload_in.items()
        if k not in _PARSED_PAYLOAD_SKIP and v is not None
    }
    return {
        "hearingDate": item.get("hearingDate"),
        "fingerprint": item.get("fingerprint"),
        "identities": item.get("identities") or {},
        "cnr": item.get("cnr"),
        "listingNo": item.get("listingNo"),
        "listingSection": item.get("listingSection"),
        "purpose": item.get("purpose"),
        "courtNo": item.get("courtNo"),
        "bench": item.get("bench"),
        "judges": item.get("judges") or [],
        "party": item.get("party"),
        "payload": payload,
    }


def compute_parsed_unit_checksum(
    *,
    court_key: str,
    hearing_date: str,
    list_type: str,
    bench_label: str,
    items: list[dict[str, Any]],
    external_id: str = "",
    edition: str = "",
    bench_type: str = "",
) -> str:
    """CauseList.checksum = SHA of **all parsed fields** for one board unit.

    After PDF/HTML parse: hash every item's listing fields (identities, party,
    judges, purpose, payload content, …) plus unit identity. Not raw file bytes.
    """
    ck = resolve_court_key(court_key)
    lt = normalize_list_type_enum(list_type)
    materials = [
        parsed_fields_checksum_material(item)
        for item in items
        if isinstance(item, dict)
    ]
    materials.sort(
        key=lambda m: (
            m.get("listingNo") is None,
            m.get("listingNo") if isinstance(m.get("listingNo"), int) else 10**9,
            str(m.get("fingerprint") or ""),
            stable_serialize(m),
        )
    )
    return sha256_hex(
        {
            "courtKey": ck,
            "hearingDate": hearing_date,
            "listType": lt,
            "benchLabel": (bench_label or "").strip(),
            "edition": _identity_seg(edition),
            "benchType": _identity_seg(bench_type),
            "externalId": external_id or "",
            "items": materials,
        }
    )


def make_external_id(
    court_key: str,
    hearing_date: str,
    list_type: str,
    bench_label: str = "",
    edition: str = "",
    bench_type: str = "",
) -> str:
    """Stable CauseList.externalId.

    ``courtKey:YYYY-MM-DD:listType:benchLabel:edition:benchType``
    """
    ck = resolve_court_key(court_key)
    date = str(hearing_date or "").strip()
    if hasattr(hearing_date, "strftime"):
        date = hearing_date.strftime("%Y-%m-%d")  # type: ignore[union-attr]
    elif len(date) >= 10 and date[4] == "-" and date[7] == "-":
        date = date[:10]
    lt = normalize_list_type_enum(list_type)
    bench = _identity_seg(bench_label)
    ed = _identity_seg(edition)
    bt = _identity_seg(bench_type)
    return f"{ck}:{date}:{lt}:{bench}:{ed}:{bt}"


def compute_fingerprint(
    *,
    identities: dict[str, Any],
    hearing_date: str,
    listing_no: int | None,
    listing_section: str | None,
    purpose: str | None,
    court_no: str | None,
    bench: str | None,
    judges: list[str],
    list_type: str | None = None,
    edition: str | None = None,
    bench_type: str | None = None,
) -> str:
    """Item fingerprint per HC/SC contract (purpose in, party out).

    Stored ``listType``, ``edition``, and ``benchType`` are included so the
    same case on a different board (e.g. division bench / edition 2) is distinct.
    """
    return sha256_hex(
        [
            identities.get("caseType"),
            identities.get("rootCaseNumber"),
            identities.get("caseYear"),
            identities.get("diaryNo"),
            hearing_date,
            listing_no,
            listing_section,
            purpose,
            court_no,
            bench,
            judges,
            normalize_list_type_enum(list_type),
            _identity_seg(edition),
            _identity_seg(bench_type),
        ]
    )


def make_case(
    *,
    date: str | None = None,
    court_name_value: str,
    court_no: str | None = None,
    listing_no: str | int | None = None,
    case_number: list[str] | str | None = None,
    party: str | None = None,
    judge: Any = None,
    list_type: str | None = None,
    section: str | None = None,
    remark: str | None = None,
    advocates: Any = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flat intermediate row used by scrapers; converted to CauseListItem later."""
    listing = None if listing_no is None else str(listing_no).strip() or None
    case: dict[str, Any] = {
        "date": date,
        "courtName": court_name_value,
        "courtNo": str(court_no).strip() if court_no not in (None, "") else None,
        "listingNo": listing,
        "caseNumber": split_case_numbers(case_number)
        if not isinstance(case_number, list)
        else [str(x).strip() for x in case_number if x is not None and str(x).strip()],
        "party": (party or "").strip() or None,
        "judge": as_judge_list(judge),
        "list_type": normalize_list_type(list_type),
        "section": (section or "").strip() or None,
        "remark": (remark or "").strip() or None,
        "advocates": as_string_list(advocates),
    }
    if extra:
        for key, value in extra.items():
            if key in case:
                continue
            case[key] = value
    return case


def make_cause_list_item(
    *,
    hearing_date: str,
    case_numbers: list[str] | str | None = None,
    party: str | None = None,
    judges: Any = None,
    listing_no: Any = None,
    listing_section: str | None = None,
    purpose: str | None = None,
    court_no: str | None = None,
    bench: str | None = None,
    cnr: str | None = None,
    diary_no: str | None = None,
    remark: str | None = None,
    advocates: Any = None,
    court_name_value: str | None = None,
    list_type: str | None = None,
    list_type_display: str | None = None,
    source_url: str | None = None,
    payload_extra: dict[str, Any] | None = None,
    edition: str | None = None,
    bench_type: str | None = None,
) -> dict[str, Any]:
    """Build one CauseListItem-shaped dict (no documentId)."""
    numbers = (
        split_case_numbers(case_numbers)
        if not isinstance(case_numbers, list)
        else [str(x).strip() for x in case_numbers if x is not None and str(x).strip()]
    )
    judge_list = as_judge_list(judges)
    section = (listing_section or "").strip() or None
    purpose_text = (purpose or "").strip() or None
    court = str(court_no).strip() if court_no not in (None, "") else None
    bench_label = (bench or "").strip() or None
    party_text = (party or "").strip() or None
    listing = parse_listing_no(listing_no)
    list_type_stored = normalize_list_type_enum(list_type)
    ed = _identity_seg(edition)
    bt = _identity_seg(bench_type)
    identities = parse_case_identities(
        numbers,
        cnr=cnr,
        diary_no=diary_no,
        remark=remark,
    )
    fingerprint = compute_fingerprint(
        identities=identities,
        hearing_date=hearing_date,
        listing_no=listing,
        listing_section=section,
        purpose=purpose_text,
        court_no=court,
        bench=bench_label,
        judges=judge_list,
        list_type=list_type_stored,
        edition=ed,
        bench_type=bt,
    )
    payload: dict[str, Any] = {}
    if court_name_value:
        payload["courtName"] = court_name_value
    if list_type_display:
        payload["list_type_display"] = list_type_display
    if remark and str(remark).strip():
        payload["remark"] = str(remark).strip()
    adv = as_string_list(advocates)
    if adv:
        payload["advocates"] = adv
    if numbers:
        payload["caseNumber"] = numbers
    if source_url:
        payload["source_url"] = source_url
    if payload_extra:
        for key, value in payload_extra.items():
            if key in payload or value is None:
                continue
            payload[key] = value

    item: dict[str, Any] = {
        "hearingDate": hearing_date,
        "fingerprint": fingerprint,
        "identities": identities,
    }
    if identities.get("cnr"):
        item["cnr"] = identities["cnr"]
    if listing is not None:
        item["listingNo"] = listing
    if section:
        item["listingSection"] = section
    if purpose_text:
        item["purpose"] = purpose_text
    if court:
        item["courtNo"] = court
    if bench_label:
        item["bench"] = bench_label
    if judge_list:
        item["judges"] = judge_list
    if party_text:
        item["party"] = party_text
    item["payload"] = payload
    return item


def make_cause_list(
    *,
    court_key: str,
    hearing_date: str,
    list_type: str,
    bench_label: str,
    items: list[dict[str, Any]],
    external_id: str = "",
    checksum: str | None = None,
    status: str | None = None,
    raw_ref: str | None = None,
    content_type: str | None = None,
    edition: str = "",
    bench_type: str = "",
) -> dict[str, Any]:
    """Build one CauseList-shaped parent (items nested for file output)."""
    ck = resolve_court_key(court_key)
    stored_type = normalize_list_type_enum(list_type)
    ed = _identity_seg(edition)
    bt = _identity_seg(bench_type)
    bench = (bench_label or "").strip()
    item_list = list(items)
    if status is None:
        status = "ok" if item_list else "empty"
    if status not in CAUSE_LIST_STATUSES:
        raise ValueError(f"invalid status: {status}")
    eid = external_id or make_external_id(
        ck, hearing_date, stored_type, bench, ed, bt
    )
    doc: dict[str, Any] = {
        "source": COURT_SOURCE[ck],
        "courtLevel": COURT_LEVEL[ck],
        "courtKey": ck,
        "hearingDate": hearing_date,
        "listType": stored_type,
        "benchLabel": bench,
        "edition": ed,
        "benchType": bt,
        "checksum": checksum
        or compute_parsed_unit_checksum(
            court_key=ck,
            hearing_date=hearing_date,
            list_type=stored_type,
            bench_label=bench,
            items=item_list,
            external_id=eid,
            edition=ed,
            bench_type=bt,
        ),
        "status": status,
        "externalId": eid,
        "items": item_list,
    }
    if raw_ref:
        doc["rawRef"] = raw_ref
        doc["contentType"] = content_type or "application/pdf"
    elif content_type:
        doc["contentType"] = content_type
    return doc


def flat_case_to_item(
    row: dict[str, Any],
    *,
    hearing_date: str,
    bench_label: str,
    list_type_display: str,
    list_type: str | None = None,
    source_url: str | None = None,
    edition: str = "",
    bench_type: str = "",
) -> dict[str, Any]:
    """Convert a scraper ``make_case`` row into a CauseListItem."""
    reserved = set(COMMON_CASE_KEYS) | {
        "coram",
        "bench",
        "cnr",
        "diaryNo",
        "diary_no",
        "purpose",
        "case_stage",
        "listingSection",
        "source_url",
        "pdf_path",
        "pdf_url",
        "gcs_uri",
        "_benchLabel",
        "_source_url",
        "_externalId",
        "_list_type_display",
        "_group",
    }
    extra = {k: v for k, v in row.items() if k not in reserved and not str(k).startswith("_")}
    bench = (
        bench_label
        or (row.get("coram") or "")
        or (row.get("bench") or "")
        or " | ".join(as_judge_list(row.get("judge") or row.get("judges")))
    )
    return make_cause_list_item(
        hearing_date=hearing_date or str(row.get("date") or ""),
        case_numbers=row.get("caseNumber"),
        party=row.get("party"),
        judges=row.get("judges") or row.get("judge"),
        listing_no=row.get("listingNo"),
        listing_section=row.get("section") or row.get("listingSection"),
        purpose=row.get("purpose") or row.get("case_stage"),
        court_no=row.get("courtNo"),
        bench=bench or None,
        cnr=row.get("cnr"),
        diary_no=row.get("diaryNo") or row.get("diary_no"),
        remark=row.get("remark"),
        advocates=row.get("advocates"),
        court_name_value=row.get("courtName"),
        list_type=list_type,
        list_type_display=list_type_display or row.get("list_type"),
        source_url=source_url
        or row.get("source_url")
        or row.get("pdf_url")
        or row.get("_source_url"),
        payload_extra=extra or None,
        edition=edition,
        bench_type=bench_type,
    )


def group_flat_cases_to_documents(
    *,
    court_key: str,
    hearing_date: str,
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group flat scraper rows into CauseList documents with items."""
    ck = resolve_court_key(court_key)
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    meta: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    for row in cases:
        if not isinstance(row, dict):
            continue
        group = row.get("_group") if isinstance(row.get("_group"), dict) else {}
        display = (
            group.get("list_type_display")
            or row.get("_list_type_display")
            or row.get("list_type")
            or "DAILY LIST"
        )
        # Display map OK for human label; stored listType = exact-alias enum only.
        display = normalize_list_type(str(display))
        list_type = normalize_list_type_enum(
            group.get("listType") or display
        )
        bench = (
            group.get("benchLabel")
            or row.get("_benchLabel")
            or row.get("coram")
            or row.get("bench")
            or " | ".join(as_judge_list(row.get("judge") or row.get("judges")))
            or ""
        )
        bench = str(bench).strip()
        edition = _identity_seg(group.get("edition") or row.get("edition") or "")
        bench_type = _identity_seg(
            group.get("benchType") or row.get("benchType") or ""
        )
        source_url = (
            group.get("source_url")
            or row.get("_source_url")
            or row.get("source_url")
            or row.get("pdf_url")
            or ""
        )
        external_id = make_external_id(
            ck, hearing_date, list_type, bench, edition, bench_type
        )
        key = (list_type, bench, edition, bench_type, external_id)
        groups.setdefault(key, []).append(row)
        meta[key] = {
            "list_type_display": display,
            "listType": list_type,
            "benchLabel": bench,
            "edition": edition,
            "benchType": bench_type,
            "externalId": external_id,
            "source_url": source_url or None,
            "rawRef": group.get("rawRef") or row.get("gcs_uri") or row.get("rawRef"),
            "contentType": group.get("contentType") or row.get("contentType"),
        }

    documents: list[dict[str, Any]] = []
    for key, rows in groups.items():
        info = meta[key]
        items = [
            flat_case_to_item(
                row,
                hearing_date=hearing_date,
                bench_label=info["benchLabel"],
                list_type_display=info["list_type_display"],
                list_type=info["listType"],
                source_url=info["source_url"],
                edition=info["edition"],
                bench_type=info["benchType"],
            )
            for row in rows
        ]
        documents.append(
            make_cause_list(
                court_key=ck,
                hearing_date=hearing_date,
                list_type=info["listType"],
                bench_label=info["benchLabel"],
                items=items,
                external_id=info["externalId"],
                checksum=None,
                raw_ref=info.get("rawRef"),
                content_type=info.get("contentType"),
                edition=info["edition"],
                bench_type=info["benchType"],
            )
        )
    return documents
