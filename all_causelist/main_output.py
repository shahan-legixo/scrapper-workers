"""Build Legixo-shaped ``mainOutput.json`` after each court scraper finishes.

Canonical shape::

    {
      "courtKey": "bmhc",
      "hearingDate": "2026-10-14",
      "document_count": N,
      "item_count": M,
      "documents": [ CauseList + items[] ]
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .all_schemas.schema import (
    COURT_NAMES,
    as_judge_list,
    group_flat_cases_to_documents,
    resolve_court_key,
    script_key_from_court_key,
)

MAIN_OUTPUT_NAME = "mainOutput.json"


def _stamp_group(
    row: dict[str, Any],
    *,
    bench_label: str,
    list_type_display: str,
    external_id: str = "",
    source_url: str = "",
    raw_ref: str | None = None,
    content_type: str | None = None,
    edition: str = "",
    bench_type: str = "",
) -> dict[str, Any]:
    stamped = dict(row)
    stamped["_group"] = {
        "benchLabel": bench_label,
        "list_type_display": list_type_display,
        "externalId": external_id,
        "source_url": source_url,
        "rawRef": raw_ref,
        "contentType": content_type,
        "edition": (edition or "").strip(),
        "benchType": (bench_type or "").strip(),
    }
    return stamped


def strip_raw_html_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop ``rawHtml`` before writing court JSON (not needed after parse)."""
    if not isinstance(payload, dict):
        return payload
    cleaned = dict(payload)
    for key in ("benches", "combos", "judges"):
        items = cleaned.get(key)
        if not isinstance(items, list):
            continue
        next_items: list[Any] = []
        for item in items:
            if isinstance(item, dict) and "rawHtml" in item:
                copy = dict(item)
                copy.pop("rawHtml", None)
                next_items.append(copy)
            else:
                next_items.append(item)
        cleaned[key] = next_items
    return cleaned


def collect_cases(court: str, data: Any) -> list[dict[str, Any]]:
    """Flatten court-native payload / parsed list into flat case dicts (with group stamps)."""
    key = court.strip().lower().replace("_", "-")
    if key in ("dlhc", "bmhc", "tshc", "orhc", "sci"):
        key = script_key_from_court_key(resolve_court_key(key))

    if key == "delhi":
        rows: list[dict[str, Any]] = []
        for bench in (data or {}).get("benches") or []:
            bench_label = str(
                bench.get("judge") or bench.get("bench") or bench.get("name") or ""
            ).strip()
            lists = bench.get("lists") or {}
            if isinstance(lists, dict):
                for label, bucket in lists.items():
                    for row in bucket or []:
                        if not isinstance(row, dict):
                            continue
                        rows.append(
                            _stamp_group(
                                row,
                                bench_label=bench_label
                                or " | ".join(as_judge_list(row.get("judge"))),
                                list_type_display=str(
                                    row.get("list_type") or label or "DAILY LIST"
                                ),
                                external_id="",
                            )
                        )
            elif isinstance(lists, list):
                for row in lists:
                    if isinstance(row, dict):
                        rows.append(
                            _stamp_group(
                                row,
                                bench_label=bench_label
                                or " | ".join(as_judge_list(row.get("judge"))),
                                list_type_display=str(row.get("list_type") or "DAILY LIST"),
                            )
                        )
        return rows

    if key == "telangana":
        rows = []
        for item in (data or {}).get("judges") or []:
            bench_label = str(item.get("judge") or item.get("name") or "").strip()
            for row in item.get("cases") or []:
                if not isinstance(row, dict):
                    continue
                rows.append(
                    _stamp_group(
                        row,
                        bench_label=bench_label
                        or " | ".join(as_judge_list(row.get("judge"))),
                        list_type_display=str(row.get("list_type") or "DAILY LIST"),
                    )
                )
        return rows

    if key == "mphc":
        rows = []
        for combo in (data or {}).get("combos") or []:
            bench_label = str(
                combo.get("judge") or combo.get("bench") or combo.get("label") or ""
            ).strip()
            # MPHC portal sbdb → CauseList.benchType (single_bench / division_bench / …).
            bench_type = str(
                combo.get("sbdb_label") or combo.get("benchType") or ""
            ).strip()
            for row in combo.get("rows") or combo.get("cases") or []:
                if not isinstance(row, dict):
                    continue
                rows.append(
                    _stamp_group(
                        row,
                        bench_label=bench_label
                        or " | ".join(as_judge_list(row.get("judge"))),
                        list_type_display=str(
                            row.get("list_type") or combo.get("list_type") or "DAILY LIST"
                        ),
                        edition="",
                        bench_type=bench_type,
                    )
                )
        return rows

    if key in ("bombay", "odisha"):
        # Prefer parsed blocks: each PDF/board becomes one CauseList unit.
        if isinstance(data, list):
            rows = []
            for item in data:
                rows.extend(_cases_from_pdf_block(item))
            return rows
        if isinstance(data, dict):
            if "documents" in data and isinstance(data["documents"], list):
                return []  # already Legixo-shaped; caller should not collect
            parsed_blocks = data.get("pdfs") or data.get("benches") or data.get("corams")
            if parsed_blocks is None and isinstance(data.get("cases"), list):
                first = data["cases"][0] if data["cases"] else None
                if first and "caseNumber" in first:
                    return [
                        _stamp_group(
                            row,
                            bench_label=" | ".join(
                                as_judge_list(row.get("judge") or row.get("judges"))
                            ),
                            list_type_display=str(row.get("list_type") or "DAILY LIST"),
                            source_url=str(row.get("source_url") or row.get("pdf_url") or ""),
                            external_id=str(
                                row.get("source_url") or row.get("pdf_url") or ""
                            ),
                        )
                        for row in data["cases"]
                        if isinstance(row, dict)
                    ]
            rows = []
            for item in parsed_blocks or []:
                # corams contain nested pdfs
                if isinstance(item, dict) and item.get("pdfs"):
                    for pdf in item.get("pdfs") or []:
                        block = dict(pdf)
                        block.setdefault("coram", item.get("coram"))
                        rows.extend(_cases_from_pdf_block(block))
                else:
                    rows.extend(_cases_from_pdf_block(item))
            return rows
        return []

    if key == "supreme":
        if isinstance(data, list):
            # Prefer per-PDF blocks; fall back to flat case rows.
            if data and isinstance(data[0], dict) and "cases" in data[0]:
                rows = []
                for item in data:
                    rows.extend(_cases_from_pdf_block(item))
                return rows
            return [
                _stamp_group(
                    row,
                    bench_label=" | ".join(
                        as_judge_list(row.get("judge") or row.get("judges"))
                    ),
                    list_type_display=str(row.get("list_type") or "DAILY LIST"),
                    source_url=str(row.get("source_url") or row.get("url") or ""),
                    external_id=str(
                        row.get("source_url")
                        or row.get("url")
                        or row.get("source_list")
                        or ""
                    ),
                )
                for row in data
                if isinstance(row, dict) and "caseNumber" in row
            ]
        if isinstance(data, dict) and isinstance(data.get("cases"), list):
            return [
                _stamp_group(
                    row,
                    bench_label=" | ".join(
                        as_judge_list(row.get("judge") or row.get("judges"))
                    ),
                    list_type_display=str(row.get("list_type") or "DAILY LIST"),
                    source_url=str(row.get("source_url") or ""),
                    external_id=str(row.get("source_url") or row.get("source_list") or ""),
                )
                for row in data["cases"]
                if isinstance(row, dict)
            ]
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            rows = []
            for item in data["files"]:
                rows.extend(_cases_from_pdf_block(item))
            return rows

    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return [
            _stamp_group(
                row,
                bench_label=" | ".join(as_judge_list(row.get("judge") or row.get("judges"))),
                list_type_display=str(row.get("list_type") or "DAILY LIST"),
            )
            for row in data["cases"]
            if isinstance(row, dict)
        ]
    if isinstance(data, list):
        return [
            _stamp_group(
                row,
                bench_label=" | ".join(as_judge_list(row.get("judge") or row.get("judges"))),
                list_type_display=str(row.get("list_type") or "DAILY LIST"),
            )
            for row in data
            if isinstance(row, dict) and "caseNumber" in row
        ]
    return []


def _cases_from_pdf_block(item: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        return []
    # Already a layout wrapper with cases
    cases = item.get("cases") or []
    if not cases and isinstance(item.get("layout"), dict):
        cases = (item.get("layout") or {}).get("cases") or []
    list_type_display = str(
        item.get("list_type") or item.get("source_list") or "DAILY LIST"
    )
    source_url = str(
        item.get("source_url") or item.get("pdf_url") or item.get("url") or ""
    )
    external_id = source_url or str(item.get("pdf_path") or item.get("filename") or "")
    # One CauseList per PDF: lock benchLabel for the whole block (do not split by case judges).
    bench_label = str(
        item.get("coram")
        or item.get("bench")
        or item.get("label")
        or " | ".join(as_judge_list(item.get("judges")))
        or ""
    ).strip()
    if not bench_label:
        for row in cases:
            if isinstance(row, dict):
                bench_label = " | ".join(
                    as_judge_list(row.get("judge") or row.get("judges"))
                )
                if bench_label:
                    break
    rows: list[dict[str, Any]] = []
    for row in cases:
        if not isinstance(row, dict):
            continue
        # layout raw rows may not be make_case yet — scrapers usually pass make_case
        if "caseNumber" not in row and "case_number" in row:
            continue
        rows.append(
            _stamp_group(
                row,
                bench_label=bench_label,
                list_type_display=str(row.get("list_type") or list_type_display),
                external_id=external_id,
                source_url=source_url,
            )
        )
    return rows


def build_main_output(
    *,
    court: str,
    cause_date: str,
    cases: list[dict[str, Any]],
    court_name: str | None = None,
    include_extras: bool = False,  # kept for call-site compat; unused
    documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build Legixo-shaped mainOutput (documents + items)."""
    del include_extras  # noqa: ARG001 — API compat
    court_key = resolve_court_key(court)
    docs = documents if documents is not None else group_flat_cases_to_documents(
        court_key=court_key,
        hearing_date=cause_date,
        cases=cases,
    )
    item_count = sum(len(d.get("items") or []) for d in docs)
    script = script_key_from_court_key(court_key)
    return {
        "courtKey": court_key,
        "hearingDate": cause_date,
        "court": script,  # back-compat for older server checks
        "date": cause_date,  # back-compat for sync path
        "courtName": court_name or COURT_NAMES.get(script, court_key),
        "document_count": len(docs),
        "item_count": item_count,
        "case_count": item_count,  # back-compat alias
        "documents": docs,
    }


def write_main_output(
    output_dir: Path | str,
    *,
    court: str,
    cause_date: str,
    cases: list[dict[str, Any]],
    court_name: str | None = None,
    include_extras: bool = False,
    documents: list[dict[str, Any]] | None = None,
) -> Path:
    """Write ``mainOutput.json`` into ``output_dir`` after a scraper finishes."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    doc = build_main_output(
        court=court,
        cause_date=cause_date,
        cases=cases,
        court_name=court_name,
        include_extras=include_extras,
        documents=documents,
    )
    path = out / MAIN_OUTPUT_NAME
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_main_output_from_payload(
    output_dir: Path | str,
    court: str,
    payload: Any,
    *,
    cause_date: str | None = None,
    court_name: str | None = None,
    parsed: list[dict[str, Any]] | None = None,
) -> Path:
    """Collect cases from payload (or ``parsed`` list) and write Legixo mainOutput.json."""
    source = parsed if parsed is not None else payload
    cases = collect_cases(court, source)
    date_value = cause_date
    name_value = court_name
    if isinstance(payload, dict):
        date_value = date_value or payload.get("date") or payload.get("hearingDate")
        name_value = name_value or payload.get("courtName")
    if not date_value:
        raise ValueError("cause_date is required to write mainOutput.json")
    return write_main_output(
        output_dir,
        court=court,
        cause_date=str(date_value),
        cases=cases,
        court_name=name_value,
    )
