"""Fetch Madhya Pradesh High Court Judge-tab cause lists (HTML via get_cl.php)."""

from __future__ import annotations

import json
import re
import ssl
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from .all_schemas.schema import court_name, make_case, normalize_list_type

PAGE_URL = "https://portal.mphc.gov.in/causelist"
COURT_NAME = court_name("mphc")
SESSION_URL = "https://portal.mphc.gov.in/php/hc/MainMenu/session_my.php"
GET_CL_URL = "https://portal.mphc.gov.in/php/hc/causelist/get_cl.php"

PLACE_CODES = {
    "JBP": "01",
    "IND": "02",
    "GWL": "03",
}
PLACE_LABELS = {
    "JBP": "Jabalpur",
    "IND": "Indore",
    "GWL": "Gwalior",
}

MFB_CODES = {
    "M": "motion",
    "F": "final",
    "L": "lok_adalat",
    "S": "mediation",
}
SBDB_CODES = {
    "1": "single_bench",
    "2": "division_bench",
    "3": "full_bench",
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": PAGE_URL,
    "Origin": "https://portal.mphc.gov.in",
}

WS_RE = re.compile(r"\s+")
JUDGE_RE = re.compile(
    r"HON[''`′]?BLE|JUSTICE|REGISTRAR",
    re.IGNORECASE,
)


class MphcCauseListError(RuntimeError):
    """The Madhya Pradesh High Court cause list could not be fetched."""


def combo_to_documents(combo: dict[str, Any], *, cause_date: str) -> list[dict[str, Any]]:
    """One scraped MPHC combo → Legixo CauseList documents (parsed-fields checksum)."""
    from .all_schemas.schema import group_flat_cases_to_documents
    from .main_output import collect_cases

    cases = collect_cases("mphc", {"combos": [combo]})
    if not cases:
        return []
    return group_flat_cases_to_documents(
        court_key="mphc",
        hearing_date=cause_date,
        cases=cases,
    )


def iter_mphc_combo_units(
    cause_date: str,
    *,
    places: list[str] | None = None,
    mfb_filter: str | None = None,
    sbdb_filter: str | None = None,
    delay_seconds: float = 1.0,
    on_progress: Callable[[str], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one combo at a time: fetch → parse → documents; drop HTML before next.

    Each yield::

        {
          "combo_index": int,
          "combo_total": int,
          "combo_meta": {...},
          "documents": [CauseList, ...],
          "case_count": int,
        }
    """
    display_date = _to_display_date(cause_date)
    progress = on_progress or (lambda _message: None)

    selected_places = _normalize_places(places)
    mfb_list = _normalize_mfb(mfb_filter)
    sbdb_list = _normalize_sbdb(sbdb_filter)
    combos_plan = [
        (place, mfb, sbdb)
        for place in selected_places
        for mfb in mfb_list
        for sbdb in sbdb_list
    ]
    if not combos_plan:
        raise MphcCauseListError("No place/mfb/sbdb combinations to fetch.")

    total = len(combos_plan)
    progress(
        f"Streaming {total} MPHC combo(s) for {cause_date} "
        f"({', '.join(selected_places)})."
    )

    with httpx.Client(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(120.0, connect=60.0),
        follow_redirects=True,
        verify=_ssl_context(),
    ) as client:
        csrf = ""
        for attempt in range(1, 4):
            try:
                csrf = _warm_and_csrf(client)
                break
            except Exception as exc:
                if attempt == 3:
                    raise MphcCauseListError(
                        f"MPHC portal connect failed after 3 attempts: {exc}"
                    ) from exc
                time.sleep(2.0 * attempt)
        last_place: str | None = None

        for index, (place, mfb, sbdb) in enumerate(combos_plan, start=1):
            if index > 1 and delay_seconds > 0:
                time.sleep(delay_seconds)

            label = (
                f"{PLACE_LABELS.get(place, place)} / "
                f"{MFB_CODES[mfb]} / {SBDB_CODES[sbdb]}"
            )
            progress(f"[{index}/{total}] {label}")
            combo = _fetch_one_combo(
                client,
                cause_date=cause_date,
                display_date=display_date,
                place=place,
                mfb=mfb,
                sbdb=sbdb,
                csrf=csrf,
                last_place=last_place,
            )
            csrf = combo.pop("_csrf_out", csrf)
            last_place = combo.pop("_last_place_out", last_place)

            documents = combo_to_documents(combo, cause_date=cause_date)
            combo.pop("rawHtml", None)
            meta = {
                "place": combo.get("place"),
                "place_label": combo.get("place_label"),
                "mfb": combo.get("mfb"),
                "mfb_label": combo.get("mfb_label"),
                "sbdb": combo.get("sbdb"),
                "sbdb_label": combo.get("sbdb_label"),
                "list_type": combo.get("list_type"),
                "label": label,
                "error": combo.get("error"),
                "row_count": combo.get("row_count", 0),
            }
            case_count = int(combo.get("row_count") or 0)
            progress(
                f"  -> {case_count} cases / {len(documents)} CauseList doc(s)"
                + (f" error={meta['error']}" if meta.get("error") else "")
            )
            yield {
                "combo_index": index,
                "combo_total": total,
                "combo_meta": meta,
                "documents": documents,
                "case_count": case_count,
            }
            del combo
            del documents


def fetch_mphc_cause_list(
    cause_date: str,
    *,
    places: list[str] | None = None,
    mfb_filter: str | None = None,
    sbdb_filter: str | None = None,
    delay_seconds: float = 1.0,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Fetch all MPHC combos into one payload (CLI / local JSON).

    Cloud path should use :func:`iter_mphc_combo_units` instead (stream + upsert).
    """
    return _fetch_mphc_cause_list_accumulate(
        cause_date,
        places=places,
        mfb_filter=mfb_filter,
        sbdb_filter=sbdb_filter,
        delay_seconds=delay_seconds,
        on_progress=on_progress,
    )


def _fetch_mphc_cause_list_accumulate(
    cause_date: str,
    *,
    places: list[str] | None = None,
    mfb_filter: str | None = None,
    sbdb_filter: str | None = None,
    delay_seconds: float = 1.0,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """CLI helper: all combos with rows (no rawHtml kept)."""
    display_date = _to_display_date(cause_date)
    progress = on_progress or (lambda _message: None)
    selected_places = _normalize_places(places)
    mfb_list = _normalize_mfb(mfb_filter)
    sbdb_list = _normalize_sbdb(sbdb_filter)
    combos_plan = [
        (place, mfb, sbdb)
        for place in selected_places
        for mfb in mfb_list
        for sbdb in sbdb_list
    ]
    if not combos_plan:
        raise MphcCauseListError("No place/mfb/sbdb combinations to fetch.")

    results: list[dict] = []
    with httpx.Client(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(120.0, connect=60.0),
        follow_redirects=True,
        verify=_ssl_context(),
    ) as client:
        csrf = ""
        for attempt in range(1, 4):
            try:
                csrf = _warm_and_csrf(client)
                break
            except Exception as exc:
                if attempt == 3:
                    raise MphcCauseListError(
                        f"MPHC portal connect failed after 3 attempts: {exc}"
                    ) from exc
                time.sleep(2.0 * attempt)
        last_place: str | None = None
        for index, (place, mfb, sbdb) in enumerate(combos_plan, start=1):
            if index > 1 and delay_seconds > 0:
                time.sleep(delay_seconds)
            label = (
                f"{PLACE_LABELS.get(place, place)} / "
                f"{MFB_CODES[mfb]} / {SBDB_CODES[sbdb]}"
            )
            progress(f"[{index}/{len(combos_plan)}] {label}")
            combo = _fetch_one_combo(
                client,
                cause_date=cause_date,
                display_date=display_date,
                place=place,
                mfb=mfb,
                sbdb=sbdb,
                csrf=csrf,
                last_place=last_place,
            )
            csrf = combo.pop("_csrf_out", csrf)
            last_place = combo.pop("_last_place_out", last_place)
            combo.pop("rawHtml", None)
            results.append(combo)
            progress(f"  -> {combo.get('row_count', 0)} cases")

    case_count = sum(int(item.get("row_count") or 0) for item in results)
    return {
        "court": "mphc",
        "courtName": COURT_NAME,
        "date": cause_date,
        "display_date": display_date,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "places": selected_places,
        "combo_count": len(results),
        "case_count": case_count,
        "combos": results,
    }


def _fetch_one_combo(
    client: httpx.Client,
    *,
    cause_date: str,
    display_date: str,
    place: str,
    mfb: str,
    sbdb: str,
    csrf: str,
    last_place: str | None,
) -> dict[str, Any]:
    """Fetch+parse one place/mfb/sbdb combo. Returns combo dict (+ session outs)."""
    out_csrf = csrf
    out_place = last_place
    try:
        if place != last_place:
            _switch_place(client, place)
            out_csrf = _warm_and_csrf(client)
            out_place = place

        html = _post_get_cl(
            client,
            csrf=out_csrf,
            place=place,
            display_date=display_date,
            mfb=mfb,
            sbdb=sbdb,
        )
        if html.lstrip().startswith("ERROR"):
            raise MphcCauseListError(html.split("|", 1)[0][:300])

        raw_rows = parse_get_cl_html(html)
        place_court_no = PLACE_CODES[place]
        place_label = PLACE_LABELS.get(place, place)
        list_type = normalize_list_type(MFB_CODES[mfb])
        rows: list[dict] = []
        for raw in raw_rows:
            party = _party_from_sides(raw.get("petitioner"), raw.get("respondent"))
            adv_list: list[str] = []
            pet_adv = raw.get("advocates")
            if isinstance(pet_adv, str) and pet_adv.strip():
                adv_list.append(pet_adv.strip())
            elif isinstance(pet_adv, list):
                adv_list.extend(str(a).strip() for a in pet_adv if a)
            for a in raw.get("respondent_advocates") or []:
                if a:
                    adv_list.append(str(a).strip())
            rows.append(
                make_case(
                    date=cause_date,
                    court_name_value=f"{COURT_NAME} ({place_label})",
                    court_no=place_court_no,
                    listing_no=raw.get("item_no"),
                    case_number=raw.get("case_no"),
                    party=party,
                    judge=raw.get("judge"),
                    list_type=list_type,
                    section=raw.get("section"),
                    remark=raw.get("remarks"),
                    advocates=adv_list,
                    extra={
                        "place": place,
                        "place_label": place_label,
                        "mfb": mfb,
                        "mfb_label": MFB_CODES[mfb],
                        "sbdb": sbdb,
                        "sbdb_label": SBDB_CODES[sbdb],
                        "item_no": raw.get("item_no"),
                        "case_no": raw.get("case_no"),
                        "case_url": raw.get("case_url"),
                        "petitioner": raw.get("petitioner"),
                        "respondent": raw.get("respondent"),
                        "respondent_advocates": raw.get("respondent_advocates"),
                        "vc_link": raw.get("vc_link"),
                        "notes": raw.get("notes") or [],
                        "court": place_court_no,
                    },
                )
            )

        combo: dict[str, Any] = {
            "place": place,
            "place_label": PLACE_LABELS.get(place, place),
            "mfb": mfb,
            "mfb_label": MFB_CODES[mfb],
            "list_type": list_type,
            "sbdb": sbdb,
            "sbdb_label": SBDB_CODES[sbdb],
            "row_count": len(rows),
            "rows": rows,
            "rawHtml": html,
            "_csrf_out": out_csrf,
            "_last_place_out": out_place,
        }
        return combo
    except (httpx.HTTPError, MphcCauseListError) as exc:
        return {
            "place": place,
            "place_label": PLACE_LABELS.get(place, place),
            "mfb": mfb,
            "mfb_label": MFB_CODES[mfb],
            "list_type": normalize_list_type(MFB_CODES[mfb]),
            "sbdb": sbdb,
            "sbdb_label": SBDB_CODES[sbdb],
            "error": str(exc),
            "row_count": 0,
            "rows": [],
            "_csrf_out": out_csrf,
            "_last_place_out": out_place if out_place is not None else last_place,
        }


def write_json(payload: dict, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    from .main_output import strip_raw_html_from_payload, write_main_output_from_payload

    write_main_output_from_payload(
        output_dir,
        "mphc",
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


def parse_get_cl_html(html: str) -> list[dict]:
    """Parse get_cl.php HTML into flat case rows."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []

    for table in soup.select("table.mytable"):
        judge = _judge_before_table(table)
        section: str | None = None
        current: dict | None = None

        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue

            texts = [_clean(c.get_text(" ", strip=True)) for c in cells]
            joined = " ".join(t for t in texts if t)

            if not joined:
                continue

            # Column header
            if texts[0].lower().startswith("c.l.no") or (
                len(texts) >= 2 and texts[1].lower().startswith("case no")
            ):
                continue

            # Section heading (MOTION HEARING, [ORDERS], etc.)
            if len(cells) == 1 or (
                texts[0]
                and not texts[0].isdigit()
                and not _case_link(cells)
                and all(not t or t == texts[0] for t in texts[1:])
            ):
                if texts[0] and not texts[0].isdigit() and "Vs." not in texts[0]:
                    # Single-cell or full-width heading
                    if len(cells) <= 2 or (cells[0].get("colspan") and not _case_link(cells)):
                        heading = texts[0]
                        if heading and heading.upper() not in ("",):
                            if heading.startswith("[") or "HEARING" in heading.upper():
                                section = heading
                        continue

            # Respondent continuation row
            if len(texts) >= 2 and texts[1].rstrip(".").upper() == "VS":
                if current is not None:
                    current["respondent"] = texts[2] if len(texts) > 2 else None
                    if len(texts) > 3 and texts[3]:
                        adv = current.get("respondent_advocates") or []
                        adv.append(texts[3])
                        current["respondent_advocates"] = adv
                continue

            # New case row: first cell is item number
            if texts[0].isdigit():
                if current is not None:
                    rows.append(current)
                link = _case_link(cells)
                case_no = texts[1] if len(texts) > 1 else None
                petitioner = texts[2] if len(texts) > 2 else None
                advocates = texts[3] if len(texts) > 3 else None
                remarks = texts[4] if len(texts) > 4 else None
                vc = None
                if len(cells) > 5:
                    a = cells[5].find("a", href=True)
                    if a:
                        vc = urljoin(PAGE_URL, a["href"])
                    elif texts[5]:
                        vc = texts[5] if "VC" in texts[5].upper() else None
                elif len(texts) > 5 and "VC" in texts[5].upper():
                    a = cells[5].find("a", href=True) if len(cells) > 5 else None
                    vc = urljoin(PAGE_URL, a["href"]) if a else texts[5]

                current = {
                    "judge": judge,
                    "section": section,
                    "item_no": texts[0],
                    "case_no": case_no or None,
                    "case_url": urljoin(PAGE_URL, link["href"]) if link else None,
                    "petitioner": petitioner or None,
                    "respondent": None,
                    "advocates": advocates or None,
                    "respondent_advocates": [],
                    "remarks": remarks or None,
                    "vc_link": vc,
                    "notes": [],
                }
                continue

            # Extra note / IA lines under an open case
            if current is not None and joined:
                current["notes"].append(joined)

        if current is not None:
            rows.append(current)

    return rows


def _judge_before_table(table: Tag) -> str | None:
    # Prefer nearby preceding <u> with judge name
    for prev in table.find_all_previous(["u", "div", "b"], limit=40):
        text = _clean(prev.get_text(" ", strip=True))
        if text and JUDGE_RE.search(text) and "Causelist dated" not in text:
            # Strip "Causelist dated …" wrappers if present
            for part in re.split(r"<br\s*/?>|\n", text, flags=re.I):
                part = _clean(part)
                if JUDGE_RE.search(part) and "dated" not in part.lower():
                    return part
            return text
    return None


def _case_link(cells: list[Tag]) -> Tag | None:
    for cell in cells[:3]:
        a = cell.find("a", href=True)
        if a and "casestatus" in (a.get("href") or ""):
            return a
        if a and a.get_text(strip=True):
            return a
    return None


def _warm_and_csrf(client: httpx.Client) -> str:
    response = client.get(PAGE_URL)
    response.raise_for_status()
    match = re.search(
        r'id=["\']csrf_token["\'][^>]*value=["\']([^"\']*)["\']',
        response.text,
    )
    if not match:
        match = re.search(
            r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']*)["\']',
            response.text,
        )
    if not match or not match.group(1):
        raise MphcCauseListError("csrf_token missing from causelist page.")
    return match.group(1)


def _switch_place(client: httpx.Client, place: str) -> None:
    city = PLACE_CODES.get(place)
    if not city:
        raise MphcCauseListError(f"Unknown place code: {place!r}")
    response = client.get(SESSION_URL, params={"my_ct": city})
    response.raise_for_status()


def _post_get_cl(
    client: httpx.Client,
    *,
    csrf: str,
    place: str,
    display_date: str,
    mfb: str,
    sbdb: str,
) -> str:
    # Final lists use week mode (dw=2) on the site; others use daily (dw=1).
    dw = "2" if mfb == "F" else "1"
    data = {
        "jcd": "0",
        "dtd": display_date,
        "place": place,
        "sbdb": sbdb,
        "dw": dw,
        "mfb": mfb,
        "lt": "0",
        "code": "",
        "a": str(time.time()),
        "csrf_token": csrf,
    }
    response = client.post(
        GET_CL_URL,
        data=data,
        headers={**BROWSER_HEADERS, "X-Requested-With": "XMLHttpRequest"},
    )
    response.raise_for_status()
    body = response.text
    # Browser JS does data.split("|")[0]; ERROR replies are "ERROR|…".
    # Success HTML can contain "|", so only split the ERROR form.
    stripped = body.lstrip()
    if stripped.startswith("ERROR"):
        return stripped.split("|", 1)[0]
    return body


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _to_display_date(cause_date: str) -> str:
    try:
        return datetime.strptime(cause_date, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError as exc:
        raise MphcCauseListError(
            f"Date must be YYYY-MM-DD, got {cause_date!r}."
        ) from exc


def _normalize_places(places: list[str] | None) -> list[str]:
    if not places:
        return list(PLACE_CODES)
    out: list[str] = []
    for raw in places:
        key = raw.strip().upper()
        aliases = {
            "JABALPUR": "JBP",
            "INDORE": "IND",
            "GWALIOR": "GWL",
            "01": "JBP",
            "02": "IND",
            "03": "GWL",
        }
        key = aliases.get(key, key)
        if key not in PLACE_CODES:
            raise MphcCauseListError(
                f"Unknown place {raw!r}. Use JBP, IND, GWL (or Jabalpur/Indore/Gwalior)."
            )
        if key not in out:
            out.append(key)
    return out


def _normalize_mfb(mfb_filter: str | None) -> list[str]:
    if not mfb_filter:
        return list(MFB_CODES)
    key = mfb_filter.strip().upper()
    aliases = {
        "MOTION": "M",
        "FINAL": "F",
        "LOK": "L",
        "LOK_ADALAT": "L",
        "LOKADALAT": "L",
        "MEDIATION": "S",
    }
    key = aliases.get(key, key)
    if key not in MFB_CODES:
        raise MphcCauseListError(f"Unknown mfb {mfb_filter!r}. Use M, F, L, or S.")
    return [key]


def _normalize_sbdb(sbdb_filter: str | None) -> list[str]:
    if not sbdb_filter:
        return list(SBDB_CODES)
    key = sbdb_filter.strip().lower()
    aliases = {
        "1": "1",
        "2": "2",
        "3": "3",
        "single": "1",
        "single_bench": "1",
        "division": "2",
        "division_bench": "2",
        "full": "3",
        "full_bench": "3",
    }
    key = aliases.get(key, key)
    if key not in SBDB_CODES:
        raise MphcCauseListError(f"Unknown sbdb {sbdb_filter!r}. Use 1, 2, or 3.")
    return [key]


def _clean(text: str) -> str:
    return WS_RE.sub(" ", (text or "").replace("\xa0", " ")).strip()


def _party_from_sides(petitioner: str | None, respondent: str | None) -> str | None:
    left = (petitioner or "").strip()
    right = (respondent or "").strip()
    if left and right:
        return f"{left} VS {right}"
    return left or right or None
