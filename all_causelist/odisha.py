"""Fetch Odisha High Court cause lists from eCourts (PDF per bench)."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
import httpx

from .pdfLayout import parse_odisha_cause_list_pdf
from .all_schemas.schema import court_name, make_case, normalize_list_type

BASE = "https://hcservices.ecourts.gov.in/ecourtindiaHC/cases"
COURT_NAME = court_name("odisha")
PAGE_URL = (
    f"{BASE}/highcourt_causelist.php"
    "?state_cd=11&dist_cd=1&court_code=1&stateNm=Odisha/"
)
QUERY_URL = f"{BASE}/highcourt_causelist_qry.php"
PDF_URL = f"{BASE}/display_causelist.php"

STATE_CODE = "11"
DIST_CODE = "1"
COURT_CODE = "1"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Origin": "https://hcservices.ecourts.gov.in",
    "Referer": PAGE_URL,
}


class OdishaCauseListError(RuntimeError):
    """The Odisha eCourts cause list could not be fetched."""


def fetch_odisha_cause_list(
    cause_date: str,
    output_dir: Path,
    *,
    bench_filter: str | None = None,
    delay_seconds: float = 1.0,
    parse_pdfs: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Download every published Odisha cause-list PDF for ``cause_date`` (YYYY-MM-DD)."""
    display_date = _to_display_date(cause_date)
    progress = on_progress or (lambda _message: None)

    day_dir = output_dir / f"causelist-{cause_date}"
    pdf_dir = day_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(90.0, connect=20.0),
        follow_redirects=True,
    ) as client:
        warm = client.get(PAGE_URL)
        warm.raise_for_status()

        benches = _fetch_index(client, display_date)
        selected = _filter_benches(benches, bench_filter)
        if not selected:
            raise OdishaCauseListError(
                f"No Odisha bench matched {bench_filter!r}. "
                f"Published lists: {len(benches)}."
            )

        progress(f"Fetching {len(selected)} Odisha PDF(s) for {cause_date}.")
        results: list[dict] = []
        for index, bench in enumerate(selected, start=1):
            if index > 1 and delay_seconds > 0:
                time.sleep(delay_seconds)
            progress(f"[{index}/{len(selected)}] {bench['bench']} ({bench['list_type']})")
            item = _download_bench_pdf(
                client,
                bench,
                pdf_dir=pdf_dir,
                parse_pdf=parse_pdfs,
            )
            results.append(item)

    index_benches = []
    parsed: list[dict] = []
    total_cases = 0
    for item in results:
        list_type = normalize_list_type(item.get("list_type"))
        cases: list[dict] = []
        if parse_pdfs and item.get("pdf_path"):
            block = cases_from_pdf(
                item["pdf_path"],
                cause_date=cause_date,
                list_type=item.get("list_type"),
                source_url=item.get("pdf_url"),
                label=item.get("bench") or "",
                layout=item.get("layout"),
                sr_no=item.get("sr_no"),
            )
            cases = block["cases"]
            parsed.append(block)
        total_cases += len(cases)
        index_benches.append(
            {
                "sr_no": item["sr_no"],
                "bench": item["bench"],
                "list_type": list_type,
                "source_list": item.get("list_type"),
                "pdf_url": item.get("pdf_url"),
                "pdf_path": item.get("pdf_path"),
                "case_count": len(cases),
                "courtNo": (item.get("layout") or {}).get("courtNo"),
                "error": item.get("error"),
                "parse_error": item.get("parse_error"),
            }
        )

    payload = {
        "court": "odisha",
        "courtName": COURT_NAME,
        "date": cause_date,
        "display_date": display_date,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "bench_count": len(results),
        "pdf_count": sum(1 for item in results if item.get("pdf_path")),
        "case_count": total_cases,
        "benches": index_benches,
        "output_dir": str(day_dir),
    }
    index_path = day_dir / "index.json"
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if parse_pdfs:
        (day_dir / "parsed.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        from .main_output import write_main_output_from_payload

        write_main_output_from_payload(
            day_dir,
            "odisha",
            payload,
            parsed=parsed,
            cause_date=cause_date,
            court_name=COURT_NAME,
        )
    return payload


def download_odisha_pdfs(
    cause_date: str,
    output_dir: Path,
    *,
    delay_seconds: float = 1.0,
    bench_filter: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Stage 1: download PDFs only. Parsing happens in the parse worker."""
    return fetch_odisha_cause_list(
        cause_date,
        output_dir,
        bench_filter=bench_filter,
        delay_seconds=delay_seconds,
        parse_pdfs=False,
        on_progress=on_progress,
    )


def iter_downloaded_pdfs(payload: dict) -> list[dict]:
    """Stage-1 index rows: one dict per downloaded PDF."""
    out: list[dict] = []
    for item in payload.get("benches") or []:
        if not item.get("pdf_path"):
            continue
        out.append(
            {
                "court": "odisha",
                "pdf_path": item["pdf_path"],
                "source_url": item.get("pdf_url") or "",
                "list_type": item.get("source_list") or item.get("list_type") or "Daily",
                "label": item.get("bench") or "",
            }
        )
    return out


def cases_from_pdf(
    pdf_path: str | Path,
    *,
    cause_date: str,
    list_type: str | None = None,
    source_url: str | None = None,
    label: str = "",
    layout: dict | None = None,
    sr_no: str | None = None,
) -> dict:
    """Stage 2: parse one Odisha PDF into a block with flat ``cases`` rows."""
    path = Path(pdf_path)
    if layout is None:
        layout = parse_odisha_cause_list_pdf(path)
    display_list = normalize_list_type(list_type)
    item = {
        "bench": label,
        "list_type": list_type,
        "pdf_path": str(path),
        "pdf_url": source_url,
        "layout": layout,
    }
    cases = _cases_from_layout(item, cause_date=cause_date, list_type=display_list)
    for row in cases:
        if source_url:
            row["source_url"] = source_url
    return {
        "sr_no": sr_no,
        "date": cause_date,
        "courtName": COURT_NAME,
        "bench": label,
        "list_type": display_list,
        "source_list": list_type,
        "pdf_url": source_url,
        "source_url": source_url,
        "pdf_path": str(path),
        "page_count": layout.get("page_count"),
        "courtNo": layout.get("courtNo"),
        "judges": layout.get("judges") or [],
        "case_count": len(cases),
        "cases": cases,
    }


def _fetch_index(client: httpx.Client, display_date: str) -> list[dict]:
    response = client.post(
        QUERY_URL,
        data={
            "action_code": "pulishedCauselist",
            "causelist_dt": display_date,
            "state_code": STATE_CODE,
            "dist_code": DIST_CODE,
            "court_code": COURT_CODE,
        },
        headers={
            **BROWSER_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response.raise_for_status()
    body = response.text.lstrip("\ufeff").strip()
    if not body:
        raise OdishaCauseListError(f"No cause list published for {display_date}.")
    if body.upper().startswith("ERROR"):
        raise OdishaCauseListError(f"eCourts returned {body!r} for {display_date}.")

    benches = []
    for chunk in body.split("^#"):
        chunk = chunk.lstrip("\ufeff").strip()
        if not chunk:
            continue
        parts = chunk.split("~")
        if len(parts) < 5:
            continue
        benches.append(
            {
                "sr_no": parts[0].lstrip("\ufeff").strip(),
                "bench": " ".join(parts[1].split()),
                "list_type": " ".join(parts[2].split()),
                "causelist_date": parts[3].strip(),
                "filename": parts[4].strip(),
                "eliminated": (parts[5].strip() if len(parts) > 5 else "N"),
                "for_bench_id": (parts[6].strip() if len(parts) > 6 else ""),
                "causelist_id": (parts[7].strip() if len(parts) > 7 else ""),
            }
        )
    if not benches:
        raise OdishaCauseListError(f"Could not parse published lists for {display_date}.")
    return benches


def _download_bench_pdf(
    client: httpx.Client,
    bench: dict,
    *,
    pdf_dir: Path,
    parse_pdf: bool,
) -> dict:
    filename = bench["filename"]
    # Server already returns a URL-encoded token; keep it as-is in the query string.
    pdf_url = f"{PDF_URL}?filename={filename}"
    safe_name = _safe_filename(
        f"{int(bench['sr_no']):02d}_{bench['bench']}_{bench['list_type']}.pdf"
    )
    pdf_path = pdf_dir / safe_name
    item = {
        **bench,
        "pdf_path": None,
        "pdf_url": pdf_url,
        "layout": None,
        "error": None,
        "parse_error": None,
    }
    last_error = "unknown error"
    for attempt in range(3):
        try:
            response = client.get(
                pdf_url,
                headers={**BROWSER_HEADERS, "Accept": "application/pdf,*/*"},
            )
            response.raise_for_status()
            content = response.content.lstrip(b"\xef\xbb\xbf")
            content_type = (response.headers.get("content-type") or "").lower()
            if "pdf" not in content_type and not content.startswith(b"%PDF"):
                item["error"] = f"Expected PDF, got {content_type or 'unknown'}"
                return item
            pdf_path.write_bytes(content)
            item["pdf_path"] = str(pdf_path)
            last_error = ""
            break
        except httpx.HTTPError as exc:
            last_error = str(exc)
            time.sleep(0.8 * (attempt + 1))
    if last_error:
        item["error"] = last_error
        return item

    if parse_pdf:
        try:
            item["layout"] = parse_odisha_cause_list_pdf(pdf_path)
        except Exception as exc:  # noqa: BLE001 - keep run going for other benches
            item["parse_error"] = str(exc)
    return item


def _cases_from_layout(
    item: dict,
    *,
    cause_date: str,
    list_type: str,
) -> list[dict]:
    layout = item.get("layout") or {}
    judges = layout.get("judges") or []
    if not judges and item.get("bench"):
        judges = [item["bench"]]
    cases: list[dict] = []
    for raw in layout.get("cases") or []:
        cases.append(
            make_case(
                date=cause_date,
                court_name_value=COURT_NAME,
                court_no=raw.get("courtNo") or layout.get("courtNo"),
                listing_no=raw.get("listingNo"),
                case_number=raw.get("caseNumber") or [],
                party=raw.get("party"),
                judge=raw.get("judges") or judges,
                list_type=list_type,
                section=raw.get("section"),
                remark=raw.get("remark"),
                advocates=raw.get("advocates") or [],
                extra={
                    "cnr": raw.get("cnr"),
                    "bench": item.get("bench"),
                    "session_time": raw.get("session_time"),
                    "page": raw.get("page"),
                    "source_list": item.get("list_type"),
                    "pdf_path": item.get("pdf_path"),
                },
            )
        )
    return cases


def _filter_benches(benches: list[dict], bench_filter: str | None) -> list[dict]:
    if not bench_filter:
        return benches
    needle = bench_filter.casefold()
    return [
        bench
        for bench in benches
        if needle in bench["bench"].casefold()
        or needle in bench["list_type"].casefold()
        or needle == bench["sr_no"]
    ]


def _to_display_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise OdishaCauseListError("Date must be YYYY-MM-DD.") from exc
    return parsed.strftime("%d-%m-%Y")


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned[:180] or "causelist.pdf"
