"""Fetch one day of Delhi High Court cause lists."""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Callable

import httpx
from bs4 import BeautifulSoup, Tag

from .all_schemas.schema import court_name, make_case, normalize_list_type

BASE = "https://delhihighcourt.nic.in"
COURT_NAME = court_name("delhi")
CAUSELIST_URL = f"{BASE}/app/online-causelist"
VALIDATE_URL = f"{BASE}/app/validateCaptcha"
GENERATE_URL = f"{BASE}/app/generate-captcha"
BENCH_URL = f"{BASE}/app/get-bench"
DETAILS_URL = f"{BASE}/app/get-bench-details"

# (internal key, HTML section heading on the Delhi cause-list page)
LIST_LABELS = (
    ("advanced", "ADVANCED LIST"),
    ("supplementary", "SUPPLEMENTARY LIST"),
    ("regular", "REGULAR LIST"),
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

AJAX_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": CAUSELIST_URL,
}


class CauseListError(RuntimeError):
    """The court site did not return a usable cause list."""


DelhiCauseListError = CauseListError


def fetch_cause_list(
    cause_date: str,
    *,
    bench_filter: str | None = None,
    fetch_cnr: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Return every selected bench for ``cause_date`` (YYYY-MM-DD)."""
    _require_iso_date(cause_date)
    progress = on_progress or (lambda _message: None)

    with httpx.Client(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(60.0, connect=20.0),
        follow_redirects=True,
    ) as client:
        token, code = _open_form(client)
        if not _validate_captcha(client, token, code):
            progress("Validation code was rejected. Refreshing it once.")
            code = _refresh_captcha(client)
            if not _validate_captcha(client, token, code):
                raise CauseListError("Validation code was rejected after one refresh.")

        bench_html = _post_form(
            client,
            BENCH_URL,
            {
                "_token": token,
                "cause_list_date": cause_date,
                "randomid": code,
                "captchaInput": code,
            },
            referer=CAUSELIST_URL,
        )
        benches = _parse_benches(bench_html)
        selected = _filter_benches(benches, bench_filter)
        if not selected:
            raise CauseListError(
                f"No bench matched {bench_filter!r}. The list has {len(benches)} courts."
            )

        token = _csrf_token(BeautifulSoup(bench_html, "html.parser"))
        posted_date = _hidden_value(bench_html, "cause_list_date", cause_date)
        progress(f"Fetching {len(selected)} bench(es) for {cause_date}.")

        results: list[dict] = []
        for index, bench in enumerate(selected, start=1):
            progress(f"[{index}/{len(selected)}] {bench['judge']}")
            parsed, token = _fetch_bench_details(
                client,
                bench,
                token=token,
                posted_date=posted_date,
                cause_date=cause_date,
                fetch_cnr=fetch_cnr,
            )
            if index == 1 and parsed.get("error"):
                raise CauseListError(
                    "The first bench page was not a cause list: " + parsed["error"]
                )
            results.append(parsed)

    case_count = sum(
        len(bench["lists"][name])
        for bench in results
        for name, _label in LIST_LABELS
    )
    return {
        "court": "delhi",
        "courtName": COURT_NAME,
        "date": cause_date,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "bench_count": len(results),
        "case_count": case_count,
        "benches": results,
    }


def _open_form(client: httpx.Client) -> tuple[str, str]:
    response = client.get(CAUSELIST_URL)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    return _csrf_token(soup), _captcha_code(soup)


def _validate_captcha(client: httpx.Client, token: str, code: str) -> bool:
    response = client.post(
        VALIDATE_URL,
        data={"_token": token, "captchaInput": code},
        headers=AJAX_HEADERS,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("success") is True


def _refresh_captcha(client: httpx.Client) -> str:
    response = client.get(GENERATE_URL, headers=AJAX_HEADERS)
    response.raise_for_status()
    code = str(response.json().get("captcha_code", "")).strip()
    if not code:
        raise CauseListError("Captcha refresh did not return a code.")
    return code


def fetch_cnr(client: httpx.Client, case_history_url: str) -> str:
    """Extract 16-character CNR from Delhi High Court Case History URL."""
    if not case_history_url:
        return ""
    try:
        response = client.get(case_history_url, timeout=20.0)
        response.raise_for_status()
        match = re.search(r"CNR\s*No\.?\s*:\s*([A-Z0-9]{16})", response.text)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return ""


def _fetch_bench_details(
    client: httpx.Client,
    bench: dict,
    *,
    token: str,
    posted_date: str,
    cause_date: str,
    fetch_cnr: bool = False,
) -> tuple[dict, str]:
    last_error = "unknown error"
    for attempt in range(2):
        try:
            html = _post_form(
                client,
                DETAILS_URL,
                {
                    "_token": token,
                    "cause_list_date": posted_date,
                    "courtno": bench["court_value"],
                },
                referer=BENCH_URL,
            )
        except httpx.HTTPError as exc:
            last_error = str(exc)
            continue
        if not _looks_like_cause_list(html):
            last_error = "response was not a cause-list page"
            if attempt == 0:
                continue
            break
        parsed = _parse_details(
            html,
            bench,
            cause_date=cause_date,
            client=client if fetch_cnr else None,
        )
        next_token = _optional_csrf(html) or token
        return parsed, next_token
    failed = {
        "courtName": COURT_NAME,
        "court_value": bench["court_value"],
        "court_no": bench["court_no"],
        "courtNo": bench["court_no"],
        "side": bench["side"],
        "judge": bench["judge"],
        "error": last_error,
        "lists": {name: [] for name, _label in LIST_LABELS},
    }
    return failed, token


def _post_form(
    client: httpx.Client,
    url: str,
    data: dict,
    *,
    referer: str,
) -> str:
    response = client.post(
        url,
        data=data,
        headers={**BROWSER_HEADERS, "Referer": referer},
    )
    response.raise_for_status()
    return response.text


def _parse_benches(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", id="courtno")
    if not isinstance(select, Tag):
        raise CauseListError("Bench list did not include a court dropdown.")
    benches = []
    for option in select.find_all("option"):
        value = (option.get("value") or "").strip()
        judge = " ".join(option.get_text(" ", strip=True).split())
        if not value or not judge:
            continue
        court_no, side = _split_court(value)
        benches.append(
            {
                "court_value": value,
                "court_no": court_no,
                "side": side,
                "judge": judge,
            }
        )
    if not benches:
        raise CauseListError("Bench dropdown was empty for this date.")
    return benches


def _parse_details(
    html: str,
    bench: dict,
    *,
    cause_date: str,
    client: httpx.Client | None = None,
) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    lists = {}
    missing = []
    for name, label in LIST_LABELS:
        table = _section_table(soup, label)
        if table is None:
            missing.append(label)
            lists[name] = []
        else:
            lists[name] = _parse_table(
                table,
                cause_date=cause_date,
                bench=bench,
                list_type=normalize_list_type(label),
                client=client,
            )
    parsed = {
        "courtName": COURT_NAME,
        "court_value": bench["court_value"],
        "court_no": bench["court_no"],
        "courtNo": bench["court_no"],
        "side": bench["side"],
        "judge": bench["judge"],
        "lists": lists,
        # Full page HTML — CauseList.checksum = SHA-256 of these bytes after parse.
        "rawHtml": html,
    }
    if missing:
        parsed["error"] = "Missing sections: " + ", ".join(missing)
    return parsed


def _section_table(soup: BeautifulSoup, label: str) -> Tag | None:
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.IGNORECASE)
    for text_node in soup.find_all(string=pattern):
        table = text_node.find_next("table")
        if isinstance(table, Tag):
            return table
    return None


def _extract_case_history_url(cell: Tag) -> str:
    for anchor in cell.find_all("a"):
        raw_href = anchor.attrs.get("href") or ""
        href = raw_href if isinstance(raw_href, str) else " ".join(raw_href)
        if "online-cause-history" in href:
            return href.strip()
    return ""


def _parse_table(
    table: Tag,
    *,
    cause_date: str,
    bench: dict,
    list_type: str,
    client: httpx.Client | None = None,
) -> list[dict]:
    rows = []
    seen: set[tuple[str, str, str]] = set()
    body = table.find("tbody")
    container = body if isinstance(body, Tag) else table
    for tr in container.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        combined = " ".join(cell.get_text(" ", strip=True) for cell in cells).lower()
        if "no data found" in combined:
            continue
        if len(cells) < 6:
            continue

        case_history_url = _extract_case_history_url(cells[1])
        item_no = _cell_text(cells[0])
        case_no = _cell_text(cells[1])
        case_title = _cell_text(cells[2])
        advocate = _cell_text(cells[3])
        case_stage = _cell_text(cells[4])
        remarks = _cell_text(cells[5])

        row_key = (item_no, case_no, case_title)
        if row_key in seen:
            continue
        seen.add(row_key)

        extra = {
            "item_no": item_no,
            "case_no": case_no,
            "case_title": case_title,
            "advocate": advocate,
            "case_stage": case_stage,
            "remarks": remarks,
            "case_history_url": case_history_url,
            "side": bench.get("side"),
        }
        if client is not None:
            extra["cnr"] = fetch_cnr(client, case_history_url) if case_history_url else ""

        rows.append(
            make_case(
                date=cause_date,
                court_name_value=COURT_NAME,
                court_no=bench.get("court_no"),
                listing_no=item_no,
                case_number=case_no,
                party=case_title,
                judge=bench.get("judge"),
                list_type=list_type,
                section=case_stage or None,
                remark=remarks or None,
                advocates=advocate or None,
                extra=extra,
            )
        )
    return rows


def _cell_text(cell: Tag) -> str:
    cell_copy = BeautifulSoup(str(cell), "html.parser")
    for anchor in list(cell_copy.find_all("a")):
        if not isinstance(anchor, Tag):
            continue
        href = ""
        if anchor.attrs:
            raw_href = anchor.attrs.get("href") or ""
            href = raw_href if isinstance(raw_href, str) else " ".join(raw_href)
        label = anchor.get_text(" ", strip=True).lower()
        if "online-cause-history" in href or label in {"case history.", "case history"}:
            anchor.decompose()
    for br in cell_copy.find_all("br"):
        br.replace_with("\n")
    lines = []
    for raw in cell_copy.get_text("\n").splitlines():
        line = " ".join(raw.split())
        if not line or line.lower() in {"case history.", "case history"}:
            continue
        lines.append(line)
    return " | ".join(lines)


def _filter_benches(benches: list[dict], bench_filter: str | None) -> list[dict]:
    if not bench_filter:
        return benches
    needle = bench_filter.casefold()
    return [
        bench
        for bench in benches
        if needle in bench["judge"].casefold() or needle in bench["court_value"].casefold()
    ]


def _looks_like_cause_list(html: str) -> bool:
    upper = html.upper()
    return "ITEM NO." in upper and "ADVANCED LIST" in upper and "REGULAR LIST" in upper


def _csrf_token(soup: BeautifulSoup) -> str:
    field = soup.find("input", attrs={"name": "_token"})
    if not isinstance(field, Tag):
        raise CauseListError("CSRF token was missing.")
    token = (field.get("value") or "").strip()
    if not token:
        raise CauseListError("CSRF token was empty.")
    return token


def _optional_csrf(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    field = soup.find("input", attrs={"name": "_token"})
    if not isinstance(field, Tag):
        return None
    token = (field.get("value") or "").strip()
    return token or None


def _captcha_code(soup: BeautifulSoup) -> str:
    field = soup.find("input", id="randomid")
    if isinstance(field, Tag):
        code = (field.get("value") or "").strip()
        if code:
            return code
    span = soup.find(id="captcha-code")
    if isinstance(span, Tag):
        code = span.get_text(strip=True)
        if code:
            return code
    raise CauseListError("Validation code was missing from the form.")


def _hidden_value(html: str, name: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    field = soup.find("input", attrs={"name": name})
    if isinstance(field, Tag):
        value = (field.get("value") or "").strip()
        if value:
            return value
    return fallback


def _split_court(value: str) -> tuple[str, str]:
    if "~" in value:
        court_no, side = value.split("~", 1)
        return court_no.strip(), side.strip()
    return value.strip(), ""


def _require_iso_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise CauseListError("Date must be YYYY-MM-DD.") from exc


fetch_delhi_cause_list = fetch_cause_list

