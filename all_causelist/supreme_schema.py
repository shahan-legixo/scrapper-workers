"""Map SCI PyMuPDF layout cases onto the shared cause-list schema."""

from __future__ import annotations

import re
from typing import Any

from .all_schemas.schema import make_case

COURT_NAME = "Supreme Court of India"

_SCI_LIST_LABELS: dict[str, str] = {
    "miscellaneous": "MISCELLANEOUS LIST",
    "chamber": "CHAMBER LIST",
    "single_judge": "SINGLE JUDGE LIST",
    "registrar": "REGISTRAR LIST",
    "advance": "ADVANCED LIST",
    "regular": "REGULAR LIST",
    "review": "REVIEW LIST",
}


def normalize_sci_list_type(list_type: str | None, msb: str | None = None) -> str:
    """Map SCI folder tags onto shared uppercase list_type labels."""
    if (msb or "").casefold() == "supplementary":
        return "SUPPLEMENTARY CAUSE LIST"
    key = (list_type or "").casefold().replace("-", "_").replace(" ", "_")
    return _SCI_LIST_LABELS.get(key, (list_type or "DAILY LIST").upper())


def court_no_from_label(court: str | None) -> str | None:
    if not court:
        return None
    text = court.strip()
    if re.search(r"CHIEF\s+JUSTICE", text, re.I):
        return "CJ"
    match = re.search(r"(?:COURT|No\.?)\s*(\d+)", text, re.I) or re.search(
        r"\b(\d+)\b", text
    )
    if match:
        return match.group(1)
    return text or None


def _remark_from_layout(case: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for app in case.get("applications") or []:
        number = (app.get("application_number") or "").strip()
        app_type = (app.get("type") or "").strip()
        if number and app_type:
            parts.append(f"{number} - {app_type}")
        elif number:
            parts.append(number)
    if case.get("mention_memo"):
        parts.append("Mention Memo")
    text = "; ".join(parts).strip()
    return text or None


def _party_from_sides(petitioner: str | None, respondent: str | None) -> str | None:
    pet = (petitioner or "").strip()
    resp = (respondent or "").strip()
    if pet and resp:
        return f"{pet} VS {resp}"
    return pet or resp or None


def project_sci_case(
    layout_case: dict[str, Any],
    *,
    cause_date: str,
    list_type: str,
    msb: str,
) -> dict[str, Any]:
    """Build one mainOutput row: common keys first, SCI extras after."""
    pet_advs = list(layout_case.get("petitioner_advocates") or [])
    resp_advs = list(layout_case.get("respondent_advocates") or [])
    advocates = [a for a in (*pet_advs, *resp_advs) if a]

    case_number = layout_case.get("case_number")
    case_numbers = [case_number] if case_number else []

    # Extras beyond common schema (no mention_memo/msb/parse_warning/bench_label).
    extra: dict[str, Any] = {}
    petitioner = (layout_case.get("petitioner") or "").strip() or None
    respondent = (layout_case.get("respondent") or "").strip() or None
    if petitioner:
        extra["petitioner"] = petitioner
    if respondent:
        extra["respondent"] = respondent
    if pet_advs:
        extra["petitioner_advocates"] = pet_advs
    if resp_advs:
        extra["respondent_advocates"] = resp_advs
    category = (layout_case.get("case_category") or "").strip() or None
    if category:
        extra["case_category"] = category
    applications = layout_case.get("applications") or []
    if applications:
        extra["applications"] = applications
    if list_type:
        extra["source_list"] = list_type
    if layout_case.get("page") is not None:
        extra["page"] = layout_case.get("page")

    row = make_case(
        date=cause_date,
        court_name_value=COURT_NAME,
        court_no=court_no_from_label(layout_case.get("court")),
        listing_no=layout_case.get("item_no"),
        case_number=case_numbers,
        party=_party_from_sides(petitioner, respondent),
        judge=layout_case.get("judges") or [],
        list_type=normalize_sci_list_type(list_type, msb),
        section=layout_case.get("section"),
        remark=_remark_from_layout(layout_case),
        advocates=advocates,
        extra=extra or None,
    )
    return row
