"""Fetch Telangana High Court daily cause lists (judge-wise HTML)."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx
from bs4 import BeautifulSoup, Tag

from .all_schemas.schema import court_name, make_case, normalize_list_type

BASE = "https://causelist.tshc.gov.in"
COURT_NAME = court_name("telangana")
HOME_URL = f"{BASE}/"
DAILY_URL = f"{BASE}/showDailyCauseList"
JUDGE_LIST_URL = f"{BASE}/judgeCauseList"
JUDGE_VIEW_URL = f"{BASE}/judgeWiseView"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Origin": BASE,
    "Referer": DAILY_URL,
}

CASE_HEADERS = {
    "sl.no.",
    "case",
    "party details",
    "petitioner advocate",
    "respondent advocate",
    "district/remarks",
}


class TelanganaCauseListError(RuntimeError):
    """The Telangana cause list could not be fetched."""


def fetch_telangana_cause_list(
    cause_date: str,
    *,
    bench_filter: str | None = None,
    delay_seconds: float = 1.0,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Return every selected judge's daily cause list for ``cause_date`` (YYYY-MM-DD)."""
    display_date = _to_display_date(cause_date)
    progress = on_progress or (lambda _message: None)

    with httpx.Client(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(90.0, connect=20.0),
        follow_redirects=True,
    ) as client:
        _warm_session(client)
        judges, available_dates = _open_judge_list(client, display_date)
        if display_date not in available_dates and available_dates:
            # Still try; some pages keep the posted date even if dropdown differs.
            progress(
                f"Note: {display_date} was not in the site dropdown "
                f"({', '.join(available_dates[:6])}{'...' if len(available_dates) > 6 else ''})."
            )

        selected = _filter_judges(judges, bench_filter)
        if not selected:
            raise TelanganaCauseListError(
                f"No judge matched {bench_filter!r}. Available: {len(judges)}."
            )

        progress(f"Fetching {len(selected)} Telangana judge list(s) for {cause_date}.")
        results: list[dict] = []
        for index, judge in enumerate(selected, start=1):
            if index > 1 and delay_seconds > 0:
                time.sleep(delay_seconds)
            progress(f"[{index}/{len(selected)}] {judge}")
            try:
                html = _post_form(
                    client,
                    JUDGE_VIEW_URL,
                    {"listDate": display_date, "judge": judge},
                    referer=JUDGE_LIST_URL,
                )
                if "Session Expired" in html:
                    raise TelanganaCauseListError("Session expired while fetching a judge list.")
                parsed = _parse_judge_view(
                    html,
                    judge=judge,
                    list_date=display_date,
                    cause_date=cause_date,
                )
                # Full page HTML — CauseList.checksum = SHA-256 of these bytes after parse.
                parsed["rawHtml"] = html
                results.append(parsed)
            except (httpx.HTTPError, TelanganaCauseListError) as exc:
                results.append(
                    {
                        "judge": judge,
                        "list_date": display_date,
                        "error": str(exc),
                        "blocks": [],
                        "cases": [],
                    }
                )

    case_count = sum(len(item.get("cases") or []) for item in results)
    return {
        "court": "telangana",
        "courtName": COURT_NAME,
        "date": cause_date,
        "display_date": display_date,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "judge_count": len(results),
        "case_count": case_count,
        "judges": results,
    }


def _warm_session(client: httpx.Client) -> None:
    home = client.get(HOME_URL)
    home.raise_for_status()
    daily = client.get(DAILY_URL, headers={**BROWSER_HEADERS, "Referer": HOME_URL})
    daily.raise_for_status()
    if "Session Expired" in daily.text:
        raise TelanganaCauseListError("Could not open the Daily Cause List (session expired).")


def _open_judge_list(client: httpx.Client, display_date: str) -> tuple[list[str], list[str]]:
    html = _post_form(
        client,
        JUDGE_LIST_URL,
        {"hiddenInput": display_date, "selectedDate": display_date},
        referer=DAILY_URL,
    )
    if "Session Expired" in html:
        raise TelanganaCauseListError("Session expired opening Judge Wise list.")
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", id="judge") or soup.find("select", attrs={"name": "judge"})
    if not isinstance(select, Tag):
        raise TelanganaCauseListError("Judge dropdown was missing.")
    judges = []
    for option in select.find_all("option"):
        value = (option.get("value") or "").strip()
        if value:
            judges.append(" ".join(value.split()))
    if not judges:
        raise TelanganaCauseListError("Judge dropdown was empty for this date.")

    dates: list[str] = []
    date_select = soup.find("select", id="inputState") or soup.find(
        "select", attrs={"name": "selectedDate"}
    )
    if isinstance(date_select, Tag):
        for option in date_select.find_all("option"):
            value = (option.get("value") or "").strip()
            if value:
                dates.append(value)
    # Date is also shown as "Cause List Date : DD-MM-YYYY"
    heading = soup.find(string=re.compile(r"Cause List Date\s*:", re.I))
    if heading and not dates:
        match = re.search(r"(\d{2}-\d{2}-\d{4})", str(heading))
        if match:
            dates.append(match.group(1))
    return judges, dates


def _parse_judge_view(
    html: str,
    *,
    judge: str,
    list_date: str,
    cause_date: str,
) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    cases: list[dict] = []
    blocks: list[dict] = []
    current_block = {
        "court_no": "",
        "bench_heading": judge,
        "timing": "",
        "list_name": "DAILY LIST",
        "list_type": "DAILY LIST",
    }

    for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "table", "b", "strong"]):
        if not isinstance(element, Tag):
            continue
        if element.name == "table":
            if not _is_case_table(element):
                # Header-ish tables often carry court / timing text.
                text = " ".join(element.get_text(" ", strip=True).split())
                _update_block_from_text(current_block, text)
                continue
            section = ""
            block_cases = []
            for tr in element.find_all("tr"):
                cells = tr.find_all("td")
                if not cells:
                    continue
                if len(cells) == 1 or (cells[0].has_attr("colspan") and len(cells) < 6):
                    label = " ".join(cells[0].get_text(" ", strip=True).split())
                    if label and not _looks_like_case_header(label):
                        if re.search(r"COURT\s*NO", label, re.I):
                            _update_block_from_text(current_block, label)
                        elif re.search(r"DAILY\s*LIST|SUPPLEMENT|WEEKLY", label, re.I):
                            current_block["list_name"] = label
                            current_block["list_type"] = normalize_list_type(label)
                        elif re.search(r"to be heard|HYBRID|AT\s+\d", label, re.I):
                            current_block["timing"] = label
                        else:
                            section = label
                    continue
                if len(cells) < 6:
                    continue
                item_no = _cell_text(cells[0])
                if not item_no or not re.match(r"^\d+", item_no):
                    maybe = " ".join(cells[0].get_text(" ", strip=True).split())
                    if maybe and not maybe.isdigit():
                        section = maybe
                    continue
                case_no = _cell_text(cells[1])
                party_details = _cell_text(cells[2])
                pet_adv = _cell_text(cells[3])
                resp_adv = _cell_text(cells[4])
                district_remarks = _cell_text(cells[5])
                list_type = current_block["list_type"] or normalize_list_type(
                    current_block["list_name"]
                )
                row = make_case(
                    date=cause_date,
                    court_name_value=COURT_NAME,
                    court_no=current_block["court_no"] or None,
                    listing_no=item_no,
                    case_number=case_no,
                    party=party_details,
                    judge=judge,
                    list_type=list_type,
                    section=section or None,
                    remark=district_remarks or None,
                    advocates=[a for a in (pet_adv, resp_adv) if a],
                    extra={
                        "item_no": item_no,
                        "case_no": case_no,
                        "party_details": party_details,
                        "petitioner_advocate": pet_adv,
                        "respondent_advocate": resp_adv,
                        "district_remarks": district_remarks,
                        "bench_heading": current_block["bench_heading"],
                        "list_name": current_block["list_name"],
                        "timing": current_block["timing"],
                        "court_no": current_block["court_no"],
                    },
                )
                block_cases.append(row)
                cases.append(row)
            if block_cases:
                blocks.append(
                    {
                        **current_block,
                        "case_count": len(block_cases),
                    }
                )
            continue

        text = " ".join(element.get_text(" ", strip=True).split())
        if text:
            _update_block_from_text(current_block, text)

    return {
        "judge": judge,
        "list_date": list_date,
        "courtName": COURT_NAME,
        "blocks": blocks,
        "cases": cases,
    }


def _is_case_table(table: Tag) -> bool:
    headers = [
        " ".join(th.get_text(" ", strip=True).split()).casefold()
        for th in table.find_all("th")
    ]
    if not headers:
        return False
    return CASE_HEADERS.issubset(set(headers)) or (
        "sl.no." in headers and "case" in headers and "party details" in headers
    )


def _update_block_from_text(block: dict, text: str) -> None:
    court = re.search(r"COURT\s*NO\.?\s*([A-Za-z0-9\-]+)", text, re.I)
    if court:
        block["court_no"] = court.group(1).strip()
    if re.search(r"DAILY\s*LIST|SUPPLEMENT|WEEKLY", text, re.I):
        block["list_name"] = text if len(text) < 80 else "DAILY LIST"
        block["list_type"] = normalize_list_type(block["list_name"])
    if re.search(r"to be heard|HYBRID MODE|AT\s+\d{1,2}:\d{2}", text, re.I):
        block["timing"] = text
    if re.search(r"HONOURABLE|JUSTICE", text, re.I) and len(text) < 300:
        block["bench_heading"] = text


def _looks_like_case_header(text: str) -> bool:
    lower = text.casefold()
    return "sl.no" in lower or lower == "case" or "party details" in lower


def _cell_text(cell: Tag) -> str:
    for br in cell.find_all("br"):
        br.replace_with("\n")
    lines = []
    for raw in cell.get_text("\n").splitlines():
        line = " ".join(raw.split())
        if line:
            lines.append(line)
    return " | ".join(lines)


def _filter_judges(judges: list[str], bench_filter: str | None) -> list[str]:
    if not bench_filter:
        return judges
    needle = bench_filter.casefold()
    return [judge for judge in judges if needle in judge.casefold()]


def _post_form(client: httpx.Client, url: str, data: dict, *, referer: str) -> str:
    response = client.post(
        url,
        data=data,
        headers={**BROWSER_HEADERS, "Referer": referer},
    )
    response.raise_for_status()
    return response.text


def _to_display_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise TelanganaCauseListError("Date must be YYYY-MM-DD.") from exc
    return parsed.strftime("%d-%m-%Y")


def write_json(payload: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    from .main_output import strip_raw_html_from_payload, write_main_output_from_payload

    write_main_output_from_payload(
        output_dir,
        "telangana",
        payload,
        cause_date=payload.get("date"),
        court_name=payload.get("courtName") or COURT_NAME,
    )
    path = output_dir / f"causelist-{payload['date']}.json"
    path.write_text(
        json.dumps(strip_raw_html_from_payload(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
