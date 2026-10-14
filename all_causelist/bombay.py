"""Fetch Bombay High Court PDF cause lists from causelistFinal."""

from __future__ import annotations

import json
import re
import ssl
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from .pdfLayout import parse_bombay_cause_list_pdf
from .all_schemas.schema import court_name, make_case, normalize_list_type

BASE = "https://bombayhighcourt.gov.in"
COURT_NAME = court_name("bombay")
PAGE_URL = f"{BASE}/bhc/causelistFinal"
GET_DATA_URL = f"{BASE}/bhc/causelist/get-data"

DEFAULT_LIST_TYPES = frozenset({"daily main", "daily supplementary"})

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


class BombayCauseListError(RuntimeError):
    """The Bombay High Court cause list could not be fetched."""


def fetch_bombay_cause_list(
    cause_date: str,
    output_dir: Path,
    *,
    coram_filter: str | None = None,
    all_types: bool = False,
    delay_seconds: float = 1.0,
    parse_pdfs: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Download Bombay cause-list PDFs for ``cause_date`` (YYYY-MM-DD).

    By default only Daily Main and Daily Supplementary links are fetched.
    Pass ``all_types=True`` for weekly / chamber / production / officer lists too.
    """
    display_date = _to_display_date(cause_date)
    progress = on_progress or (lambda _message: None)

    day_dir = output_dir / f"causelist-{cause_date}"
    pdf_root = day_dir / "pdfs"
    pdf_root.mkdir(parents=True, exist_ok=True)

    with httpx.Client(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(120.0, connect=60.0),
        follow_redirects=True,
        verify=_ssl_context(),
    ) as client:
        form_fields = None
        for attempt in range(1, 4):
            try:
                form_fields = _load_form_fields(client)
                break
            except Exception as exc:
                if attempt == 3:
                    raise BombayCauseListError(f"Bombay HC form load failed after 3 attempts: {exc}") from exc
                time.sleep(2.0 * attempt)

        corams = _fetch_coram_table(client, form_fields, display_date)
        selected = _filter_corams(corams, coram_filter)
        if not selected:
            raise BombayCauseListError(
                f"No Bombay coram matched {coram_filter!r}. "
                f"Published corams: {len(corams)}."
            )

        progress(f"Fetching PDFs for {len(selected)} Bombay coram(s) on {cause_date}.")
        results: list[dict] = []
        download_index = 0
        for coram_index, coram in enumerate(selected, start=1):
            progress(f"[{coram_index}/{len(selected)}] {coram['coram']}")
            links = _select_links(coram["links"], all_types=all_types)
            coram_slug = _safe_filename(coram["coram"][:80] or f"coram_{coram_index}")
            coram_dir = pdf_root / coram_slug
            coram_dir.mkdir(parents=True, exist_ok=True)

            pdf_items: list[dict] = []
            for link in links:
                download_index += 1
                if download_index > 1 and delay_seconds > 0:
                    time.sleep(delay_seconds)
                progress(f"  -> {link['list_type']}")
                pdf_items.append(
                    _download_pdf(
                        client,
                        link,
                        pdf_dir=coram_dir,
                        parse_pdf=parse_pdfs,
                    )
                )

            results.append(
                {
                    "coram": coram["coram"],
                    "coram_slug": coram_slug,
                    "link_count": len(links),
                    "pdf_count": sum(1 for item in pdf_items if item.get("pdf_path")),
                    "pdfs": pdf_items,
                }
            )

    # index.json stays lean — full case rows go only to parsed.json
    index_corams = []
    for coram in results:
        index_pdfs = []
        for item in coram["pdfs"]:
            layout = item.get("layout") or {}
            index_pdfs.append(
                {
                    "list_type": item["list_type"],
                    "url": item["url"],
                    "pdf_path": item.get("pdf_path"),
                    "case_count": layout.get("case_count", 0),
                    "courtNo": layout.get("courtNo"),
                    "error": item.get("error"),
                    "parse_error": item.get("parse_error"),
                }
            )
        index_corams.append(
            {
                "coram": coram["coram"],
                "coram_slug": coram["coram_slug"],
                "link_count": coram["link_count"],
                "pdf_count": coram["pdf_count"],
                "pdfs": index_pdfs,
            }
        )

    payload = {
        "court": "bombay",
        "courtName": COURT_NAME,
        "jurisdiction": "B",
        "date": cause_date,
        "display_date": display_date,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "all_types": all_types,
        "coram_filter": coram_filter,
        "coram_count": len(results),
        "pdf_count": sum(item["pdf_count"] for item in results),
        "case_count": sum(
            (item.get("layout") or {}).get("case_count", 0)
            for coram in results
            for item in coram["pdfs"]
        ),
        "corams": index_corams,
        "output_dir": str(day_dir),
    }
    index_path = day_dir / "index.json"
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if parse_pdfs:
        parsed = []
        for coram in results:
            for item in coram["pdfs"]:
                if not item.get("pdf_path"):
                    continue
                block = cases_from_pdf(
                    item["pdf_path"],
                    cause_date=cause_date,
                    list_type=item.get("list_type"),
                    source_url=item.get("url"),
                    label=coram["coram"],
                    layout=item.get("layout"),
                )
                parsed.append(block)
        (day_dir / "parsed.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        from .main_output import write_main_output_from_payload

        write_main_output_from_payload(
            day_dir,
            "bombay",
            payload,
            parsed=parsed,
            cause_date=cause_date,
            court_name=COURT_NAME,
        )
    return payload


def download_bombay_pdfs(
    cause_date: str,
    output_dir: Path,
    *,
    delay_seconds: float = 1.0,
    coram_filter: str | None = None,
    all_types: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Stage 1: download PDFs only. Parsing happens in the parse worker."""
    return fetch_bombay_cause_list(
        cause_date,
        output_dir,
        coram_filter=coram_filter,
        all_types=all_types,
        delay_seconds=delay_seconds,
        parse_pdfs=False,
        on_progress=on_progress,
    )


def iter_downloaded_pdfs(payload: dict) -> list[dict]:
    """Stage-1 index rows: one dict per downloaded PDF (for GCS + parse enqueue)."""
    out: list[dict] = []
    for coram in payload.get("corams") or []:
        label = coram.get("coram") or ""
        for item in coram.get("pdfs") or []:
            if not item.get("pdf_path"):
                continue
            out.append(
                {
                    "court": "bombay",
                    "pdf_path": item["pdf_path"],
                    "source_url": item.get("url") or "",
                    "list_type": item.get("list_type") or "Daily Main",
                    "label": label,
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
) -> dict:
    """Stage 2: parse one Bombay PDF into a block with flat ``cases`` rows."""
    path = Path(pdf_path)
    if layout is None:
        layout = parse_bombay_cause_list_pdf(path)
    display_list = normalize_list_type(list_type)
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
                judge=raw.get("judges") or raw.get("judge"),
                list_type=display_list,
                section=raw.get("section"),
                remark=raw.get("remark"),
                advocates=raw.get("advocates") or [],
                extra={
                    "coram": label,
                    "bench": raw.get("bench") or layout.get("bench"),
                    "session_time": raw.get("session_time"),
                    "page": raw.get("page"),
                    "court": raw.get("courtNo") or layout.get("courtNo"),
                    "source_list": list_type,
                    "source_url": source_url,
                },
            )
        )
    return {
        "coram": label,
        "courtName": COURT_NAME,
        "list_type": display_list,
        "source_list": list_type,
        "url": source_url,
        "source_url": source_url,
        "pdf_path": str(path),
        "page_count": layout.get("page_count"),
        "courtNo": layout.get("courtNo"),
        "bench": layout.get("bench") or label,
        "judges": layout.get("judges") or [],
        "case_count": len(cases),
        "cases": cases,
    }


def _ssl_context() -> ssl.SSLContext:
    # Bombay HC TLS still needs OpenSSL legacy renegotiation.
    ctx = ssl.create_default_context()
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return ctx


def _load_form_fields(client: httpx.Client) -> dict[str, str]:
    response = client.get(PAGE_URL)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.select_one("form.pdfcauselist_form")
    if form is None:
        raise BombayCauseListError("PDF causelist form was missing on the page.")

    fields: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value") or ""

    for required in ("_token", "form_secret", "chkpassphrase", "m_juris"):
        if not fields.get(required):
            raise BombayCauseListError(f"Form field {required!r} was missing.")
    fields["m_juris"] = "B"
    return fields


def _fetch_coram_table(
    client: httpx.Client,
    form_fields: dict[str, str],
    display_date: str,
) -> list[dict]:
    data = {**form_fields, "m_causedt": display_date}
    response = client.post(
        GET_DATA_URL,
        data=data,
        headers={
            **BROWSER_HEADERS,
            "X-CSRF-TOKEN": form_fields["_token"],
            "X-Requested-With": "XMLHttpRequest",
            "Referer": PAGE_URL,
            "Origin": BASE,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise BombayCauseListError("get-data did not return JSON.") from exc

    if not payload.get("status"):
        raise BombayCauseListError(
            f"No Bombay cause list for {display_date} "
            f"(server status={payload.get('status')!r})."
        )

    page_html = payload.get("page") or ""
    if not page_html.strip():
        raise BombayCauseListError(f"Empty coram table for {display_date}.")

    soup = BeautifulSoup(page_html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise BombayCauseListError("Coram HTML table was missing.")

    corams: list[dict] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        coram_text = " ".join(cells[0].get_text(" ", strip=True).split())
        if not coram_text:
            continue
        links = _parse_row_links(cells[1:])
        corams.append({"coram": coram_text, "links": links})

    if not corams:
        raise BombayCauseListError(f"Could not parse coram rows for {display_date}.")
    return corams


def _parse_row_links(cells: list[Tag]) -> list[dict]:
    links: list[dict] = []
    seen: set[str] = set()
    # Skip the last "Entire Causelist" View form column; use signed <a> PDFs only.
    link_cells = cells[:-1] if len(cells) > 1 else cells
    for cell in link_cells:
        for anchor in cell.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or href.startswith("#"):
                continue
            list_type = " ".join(anchor.get_text(" ", strip=True).split())
            if not list_type:
                continue
            url = urljoin(BASE, href)
            key = f"{list_type}|{url}"
            if key in seen:
                continue
            seen.add(key)
            links.append({"list_type": list_type, "url": url})
    return links


def _select_links(links: list[dict], *, all_types: bool) -> list[dict]:
    if all_types:
        return links
    selected = []
    for link in links:
        name = link["list_type"].casefold()
        if name in DEFAULT_LIST_TYPES or name.startswith("daily supplementary"):
            selected.append(link)
    return selected


def _filter_corams(corams: list[dict], coram_filter: str | None) -> list[dict]:
    if not coram_filter:
        return corams
    needle = coram_filter.casefold()
    return [item for item in corams if needle in item["coram"].casefold()]


def _download_pdf(
    client: httpx.Client,
    link: dict,
    *,
    pdf_dir: Path,
    parse_pdf: bool,
) -> dict:
    safe_name = _safe_filename(f"{link['list_type']}.pdf")
    pdf_path = pdf_dir / safe_name
    item = {
        "list_type": link["list_type"],
        "url": link["url"],
        "pdf_path": None,
        "layout": None,
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
            item["layout"] = parse_bombay_cause_list_pdf(pdf_path)
        except Exception as exc:  # noqa: BLE001 - keep other downloads going
            item["parse_error"] = str(exc)
    return item


def _to_display_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise BombayCauseListError("Date must be YYYY-MM-DD.") from exc
    return parsed.strftime("%d-%m-%Y")


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned[:180] or "causelist.pdf"
