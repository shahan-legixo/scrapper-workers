"""Fetch SCI cause-list PDFs and parse with PyMuPDF layout (single source of truth)."""

from __future__ import annotations

import json
import re
import ssl
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .pdfLayout.supreme_pdf_layout import parse_cause_list_pdf
from .supreme_schema import COURT_NAME, project_sci_case

PAGE_URL = "https://www.sci.gov.in/cause-list/"
PDF_HOSTS = ("api.sci.gov.in", "webapi.sci.gov.in")

LIST_TYPE_CODES = {
    "J": "miscellaneous",
    "C": "chamber",
    "S": "single_judge",
    "R": "registrar",
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


class SciCauseListError(RuntimeError):
    """The Supreme Court cause list could not be fetched."""


def fetch_sci_cause_list(
    cause_date: str,
    output_dir: Path,
    *,
    list_type_filter: str | None = None,
    judge_filter: str | None = None,
    delay_seconds: float = 1.0,
    parse_pdfs: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Download glance PDFs for ``cause_date`` and parse cases via PyMuPDF layout."""
    _validate_date(cause_date)
    progress = on_progress or (lambda _message: None)

    day_dir = Path(output_dir) / f"causelist-{cause_date}"
    pdf_root = day_dir / "pdfs"
    pdf_root.mkdir(parents=True, exist_ok=True)

    with httpx.Client(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(120.0, connect=20.0),
        follow_redirects=True,
        verify=_ssl_context(),
    ) as client:
        progress("Loading Cause Lists at a Glance…")
        page_html = _get_page(client)
        links = _parse_glance_pdf_links(page_html, cause_date)
        roster = _parse_index_roster(page_html, cause_date)

        if list_type_filter:
            needle = list_type_filter.casefold().replace("-", "_").replace(" ", "_")
            links = [item for item in links if item["list_type"].casefold() == needle]
        if not links:
            raise SciCauseListError(
                f"No SCI glance PDFs published for {cause_date}"
                + (f" matching list type {list_type_filter!r}" if list_type_filter else "")
                + "."
            )

        progress(f"Downloading {len(links)} PDF(s) for {cause_date}.")
        file_results: list[dict] = []
        for index, link in enumerate(links, start=1):
            if index > 1 and delay_seconds > 0:
                time.sleep(delay_seconds)
            progress(
                f"[{index}/{len(links)}] {link['list_type']} / {link['msb']} "
                f"({link['filename']})"
            )
            file_results.append(
                _download_and_parse(
                    client,
                    link,
                    pdf_root=pdf_root,
                    parse_pdf=parse_pdfs,
                    judge_filter=judge_filter,
                    cause_date=cause_date,
                )
            )

    all_cases: list[dict] = []
    for item in file_results:
        all_cases.extend(item.get("cases") or [])

    payload = {
        "court": "supreme",
        "courtName": COURT_NAME,
        "date": cause_date,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "list_type_filter": list_type_filter,
        "judge_filter": judge_filter,
        "pdf_count": sum(1 for item in file_results if item.get("pdf_path")),
        "case_count": len(all_cases),
        "files": [
            {
                "list_type": item["list_type"],
                "msb": item["msb"],
                "filename": item["filename"],
                "url": item.get("url"),
                "pdf_path": item.get("pdf_path"),
                "page_count": item.get("page_count"),
                "case_count": len(item.get("cases") or []),
                "parse_error": item.get("parse_error"),
                "error": item.get("error"),
            }
            for item in file_results
        ],
        "index_roster": roster,
        "output_dir": str(day_dir),
    }
    (day_dir / "index.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if parse_pdfs:
        parsed = []
        for item in file_results:
            if not item.get("pdf_path"):
                continue
            if item.get("cases"):
                parsed.append(
                    {
                        "list_type": item["list_type"],
                        "msb": item["msb"],
                        "filename": item["filename"],
                        "url": item.get("url"),
                        "source_url": item.get("url"),
                        "pdf_path": item.get("pdf_path"),
                        "page_count": item.get("page_count"),
                        "case_count": len(item.get("cases") or []),
                        "cases": item.get("cases") or [],
                        "parse_error": item.get("parse_error"),
                        "error": item.get("error"),
                    }
                )
            else:
                parsed.append(
                    cases_from_pdf(
                        item["pdf_path"],
                        cause_date=cause_date,
                        list_type=item.get("list_type"),
                        msb=item.get("msb") or "main",
                        source_url=item.get("url"),
                        filename=item.get("filename"),
                    )
                )
        (day_dir / "parsed.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        from .main_output import write_main_output_from_payload

        main_path = write_main_output_from_payload(
            day_dir,
            "supreme",
            payload,
            parsed=parsed,
            cause_date=cause_date,
            court_name=COURT_NAME,
        )
        payload["main_output"] = str(main_path)
        payload["case_count"] = len(all_cases)

    return payload


def download_sci_pdfs(
    cause_date: str,
    output_dir: Path,
    *,
    delay_seconds: float = 1.0,
    list_type_filter: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Stage 1: download PDFs only. Parsing happens in the parse worker."""
    return fetch_sci_cause_list(
        cause_date,
        output_dir,
        list_type_filter=list_type_filter,
        delay_seconds=delay_seconds,
        parse_pdfs=False,
        on_progress=on_progress,
    )


def iter_downloaded_pdfs(payload: dict) -> list[dict]:
    """Stage-1 index rows: one dict per downloaded PDF."""
    out: list[dict] = []
    for item in payload.get("files") or []:
        if not item.get("pdf_path"):
            continue
        out.append(
            {
                "court": "supreme",
                "pdf_path": item["pdf_path"],
                "source_url": item.get("url") or "",
                "list_type": item.get("list_type") or "miscellaneous",
                "label": item.get("msb") or item.get("filename") or "",
                "msb": item.get("msb"),
                "filename": item.get("filename"),
            }
        )
    return out


def cases_from_pdf(
    pdf_path: str | Path,
    *,
    cause_date: str,
    list_type: str | None = None,
    msb: str = "main",
    source_url: str | None = None,
    filename: str | None = None,
    judge_filter: str | None = None,
) -> dict:
    """Stage 2: parse one SCI PDF into a block with flat ``cases`` rows."""
    path = Path(pdf_path)
    layout = parse_cause_list_pdf(path)
    projected = [
        project_sci_case(
            raw,
            cause_date=cause_date,
            list_type=list_type or "miscellaneous",
            msb=msb,
        )
        for raw in layout.get("cases") or []
    ]
    if judge_filter:
        needle = judge_filter.casefold()
        projected = [
            case
            for case in projected
            if needle in " ".join(case.get("judge") or []).casefold()
            or needle in (case.get("courtNo") or "").casefold()
        ]
    if source_url:
        for row in projected:
            row["source_url"] = source_url
    return {
        "list_type": list_type,
        "msb": msb,
        "filename": filename,
        "url": source_url,
        "source_url": source_url,
        "pdf_path": str(path),
        "page_count": layout.get("page_count"),
        "case_count": len(projected),
        "cases": projected,
    }


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return ctx


def _validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SciCauseListError("Date must be YYYY-MM-DD.") from exc


def _get_page(client: httpx.Client) -> str:
    response = client.get(PAGE_URL)
    response.raise_for_status()
    return response.text


def _parse_glance_pdf_links(page_html: str, cause_date: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    section = soup.select_one("#Published-Cause-List") or soup
    by_key: dict[str, dict] = {}

    for anchor in section.find_all("a", href=True):
        href = anchor["href"].strip()
        if ".pdf" not in href.lower():
            continue
        abs_url = urljoin(PAGE_URL, href)
        parsed = urlparse(abs_url)
        if parsed.netloc not in PDF_HOSTS:
            continue
        if f"/{cause_date}/" not in parsed.path and not parsed.path.endswith(
            f"/{cause_date}"
        ):
            if f"/{cause_date}/" not in abs_url:
                continue

        filename = parsed.path.rsplit("/", 1)[-1]
        meta = _meta_from_url(abs_url, filename)
        if meta is None:
            continue
        key = f"{meta['list_type']}|{meta['msb']}|{filename}"
        existing = by_key.get(key)
        if existing is None or (
            existing["host"] != "api.sci.gov.in" and parsed.netloc == "api.sci.gov.in"
        ):
            by_key[key] = {
                **meta,
                "url": abs_url,
                "filename": filename,
                "host": parsed.netloc,
            }

    msb_order = {"main": 0, "supplementary": 1}
    type_order = {
        "miscellaneous": 0,
        "chamber": 1,
        "single_judge": 2,
        "registrar": 3,
        "regular": 4,
        "review": 5,
        "advance": 6,
    }
    items = list(by_key.values())
    items.sort(
        key=lambda item: (
            type_order.get(item["list_type"], 50),
            msb_order.get(item["msb"], 50),
            item["filename"],
        )
    )
    return items


def _meta_from_url(url: str, filename: str) -> dict | None:
    is_advance = "/advance/" in url.lower()
    stem = filename.rsplit(".", 1)[0]

    match = re.fullmatch(r"([A-Z])_([A-Z]+)(?:_(\d+))?", stem, flags=re.IGNORECASE)
    if not match:
        return None
    _prefix, code, msb_num = match.groups()
    code = code.upper()
    list_type = LIST_TYPE_CODES.get(code)
    if list_type is None:
        list_type = f"code_{code.lower()}"
    if is_advance:
        list_type = "advance"
        msb = "main"
    elif msb_num == "2":
        msb = "supplementary"
    else:
        msb = "main"
    return {"list_type": list_type, "msb": msb, "code": code}


def _parse_index_roster(page_html: str, cause_date: str) -> list[dict]:
    """Parse Index table rows for the display date DD-MM-YYYY."""
    display = datetime.strptime(cause_date, "%Y-%m-%d").strftime("%d-%m-%Y")
    soup = BeautifulSoup(page_html, "html.parser")
    roster: list[dict] = []
    capturing = False

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            texts = [" ".join(c.get_text(" ", strip=True).split()) for c in cells]
            joined = " ".join(texts)
            if f"Index for {display}" in joined:
                capturing = True
                continue
            if capturing and joined.startswith("Index for ") and display not in joined:
                capturing = False
                continue
            if not capturing:
                continue
            if len(cells) < 2:
                continue
            court = texts[0]
            judges = texts[1] if len(texts) > 1 else ""
            session = texts[2] if len(texts) > 2 else ""
            if court.casefold() in {"court number", "index", ""}:
                continue
            if not judges or (
                "hon" not in judges.casefold()
                and "justice" not in judges.casefold()
                and "chief" not in judges.casefold()
            ):
                continue
            roster.append(
                {
                    "court_no": court,
                    "judges": judges,
                    "session": session,
                }
            )
    return roster


def _download_and_parse(
    client: httpx.Client,
    link: dict,
    *,
    pdf_root: Path,
    parse_pdf: bool,
    judge_filter: str | None,
    cause_date: str,
) -> dict:
    type_dir = pdf_root / link["list_type"]
    type_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = type_dir / f"{link['msb']}.pdf"
    item: dict = {
        "list_type": link["list_type"],
        "msb": link["msb"],
        "filename": link["filename"],
        "url": link["url"],
        "pdf_path": None,
        "page_count": None,
        "cases": [],
        "error": None,
        "parse_error": None,
    }

    last_error = "unknown error"
    for attempt in range(3):
        try:
            response = client.get(
                link["url"],
                headers={
                    **BROWSER_HEADERS,
                    "Accept": "application/pdf,*/*",
                    "Referer": PAGE_URL,
                },
            )
            response.raise_for_status()
            content = response.content
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
            layout = parse_cause_list_pdf(pdf_path)
            item["page_count"] = layout.get("page_count")
            projected = [
                project_sci_case(
                    raw,
                    cause_date=cause_date,
                    list_type=link["list_type"],
                    msb=link["msb"],
                )
                for raw in layout.get("cases") or []
            ]
            if judge_filter:
                needle = judge_filter.casefold()
                projected = [
                    case
                    for case in projected
                    if needle in " ".join(case.get("judge") or []).casefold()
                    or needle in (case.get("bench_label") or "").casefold()
                    or needle in (case.get("courtNo") or "").casefold()
                ]
            item["cases"] = projected
        except Exception as exc:  # noqa: BLE001
            item["parse_error"] = str(exc)
    return item
