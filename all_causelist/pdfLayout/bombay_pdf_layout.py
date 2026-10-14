"""PyMuPDF word-coordinate parser for Bombay High Court cause-list PDFs.

Technique mirrored from SCI layout parsing (columns by page-width fractions).
Focus: listingNo, caseNumber, party (VS), advocates, courtNo, judges, bench, remark.
Ignores: listType, listingFor, cnr, districtCode, courtComplexCode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

# Column bands as fractions of page width (BHC board: Sr | Case | Parties | Advocates).
# Serial digits sit near x≈48 on A4 (width≈595); 0.08 cut them off into the case band.
SR_FRAC = 0.11
CASE_FRAC = 0.32
PARTY_FRAC = 0.62
# advocates >= PARTY_FRAC

COURT_RE = re.compile(r"^COURT\s*NO\.?\s*(\d+)\s*$", re.I)
BENCH_RE = re.compile(r"^(DIVISION|SINGLE)\s*$", re.I)
JUDGE_RE = re.compile(r"^HON[''`′]?BLE\b", re.I)
OFFICER_RE = re.compile(
    r"^(?:MRS?\.|SMT\.|MS\.|MR\.)\s+.+(?:REGISTRAR|PROTHO|MASTER)",
    re.I,
)
TIME_RE = re.compile(r"^AT\s+\d{1,2}[:.]\d{2}\s*(?:A\.?M\.?|P\.?M\.?)\s*$", re.I)
SECTION_RE = re.compile(
    r"^(FOR\s+FINAL\s+HEARING|PART[- ]HEARD|FOR\s+DIRECTION|FOR\s+ORDERS|"
    r"FOR\s+ADMISSION|FOR\s+SPEAKING\s+TO\s+THE\s+MINUTES|"
    r"FRESH\s+MATTERS|CONNECTED\s+MATTERS)\s*$",
    re.I,
)
SERIAL_RE = re.compile(r"^\d{1,3}$")
# Case types like WP/123/2024, FA(ST)/12/2020 — not dates like dt.24/08/2012.
CASE_ID_RE = re.compile(
    r"^(?!dt\.?)[A-Z]{2,}[A-Z0-9().\-]*\s*/\s*\d{1,5}\s*/\s*\d{4}\b",
    re.I,
)
CASE_TAG_RE = re.compile(r"^\[(?:Original|Criminal|Civil).*\]$", re.I)
IN_CASE_RE = re.compile(r"^In\s+[A-Z]", re.I)
VS_RE = re.compile(r"^\s*V/?S\.?\s*$|^\s*Versus\s*$", re.I)
VS_INLINE_RE = re.compile(r"\bV/?S\.?\b|\bVersus\b", re.I)
REMARK_RE = re.compile(r"^REMARK\s*:", re.I)
WITH_RE = re.compile(r"^with\b", re.I)
NOISE_RE = re.compile(
    r"BOMBAY\s+HIGH\s*COURT|CAUSELIST|HEADER\s+NOTE|READER\s+NOTE|"
    r"https?://|hcbombay\.vconsol|^=+$|^-+$|"
    r"^Sr\.?$|^CASE\s*NO\.?$|^PARTIES$|^ADVOCATES$|"
    r"^For\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b|"
    r"^For\s+the\s+period\b|Practice\s+Note|Link\s*:",
    re.I,
)


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def x_mid(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def y_mid(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Line:
    y0: float
    y1: float
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in sorted(self.words, key=lambda w: w.x0))

    @property
    def y_mid(self) -> float:
        return (self.y0 + self.y1) / 2


def parse_bombay_cause_list_pdf(pdf_path: Path | str) -> dict[str, Any]:
    """Parse one Bombay cause-list PDF into structured cases."""
    path = Path(pdf_path)
    doc = pymupdf.open(path)
    page_count = len(doc)

    court_no: str | None = None
    bench: str | None = None
    judges: list[str] = []
    section: str | None = None
    session_time: str | None = None
    all_cases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            _finalize_case(current)
            if current.get("caseNumber") or current.get("party") or current.get("listingNo"):
                all_cases.append(current)
        current = None

    for page_index in range(page_count):
        page = doc[page_index]
        width = float(page.rect.width)
        sr_x = width * SR_FRAC
        case_x = width * CASE_FRAC
        party_x = width * PARTY_FRAC
        words = _words_from_page(page)
        lines = _cluster_lines(words)

        for line in lines:
            full = _clean(line.text)
            if not full or NOISE_RE.search(full):
                continue

            sr = _clean(_column_text(line, 0.0, sr_x))
            case_col = _clean(_column_text(line, sr_x, case_x))
            party_col = _clean(_column_text(line, case_x, party_x))
            adv_col = _clean(_column_text(line, party_x, width + 1.0))

            # Prefer full-line match for headers that span columns
            court_m = COURT_RE.match(full) or COURT_RE.match(party_col) or COURT_RE.match(case_col)
            if court_m:
                flush()
                court_no = court_m.group(1)
                judges = []
                continue

            if BENCH_RE.match(full) or BENCH_RE.match(party_col):
                bench = (full if BENCH_RE.match(full) else party_col).upper()
                continue

            if TIME_RE.match(full) or TIME_RE.match(party_col):
                session_time = full if TIME_RE.match(full) else party_col
                section = session_time
                continue

            if JUDGE_RE.match(full) or JUDGE_RE.match(party_col) or OFFICER_RE.match(full):
                name = full if (JUDGE_RE.match(full) or OFFICER_RE.match(full)) else party_col
                name = _clean(name)
                if name and name not in judges:
                    judges.append(name)
                continue

            if SECTION_RE.match(full) or SECTION_RE.match(party_col):
                section = full if SECTION_RE.match(full) else party_col
                continue

            if REMARK_RE.match(full) or REMARK_RE.match(case_col) or REMARK_RE.match(party_col):
                remark_text = full
                if current is not None:
                    prev = current.get("remark") or ""
                    current["remark"] = _clean(f"{prev} {remark_text}".strip())
                continue

            # New case: serial in Sr column + case id on the same row.
            # Guard against remark crumbs like "No. 1" landing in the Sr band.
            if (
                sr
                and SERIAL_RE.match(sr)
                and case_col
                and (CASE_ID_RE.match(case_col) or CASE_ID_RE.search(case_col))
            ):
                flush()
                current = {
                    "listingNo": sr.lstrip("0") or sr,
                    "caseNumber": [],
                    "_case_raw": [],
                    "_party_parts": [],
                    "_adv_parts": [],
                    "party": None,
                    "advocates": [],
                    "remark": None,
                    "section": section,
                    "session_time": session_time,
                    "courtNo": court_no,
                    "bench": bench,
                    "judges": list(judges),
                    "page": page_index + 1,
                }
                if case_col:
                    _add_case_token(current, case_col)
                if party_col:
                    current["_party_parts"].append(party_col)
                if adv_col:
                    current["_adv_parts"].append(adv_col)
                continue

            if current is None:
                continue

            # Continuation / with-linked matters (still capture party/adv on same row)
            if WITH_RE.match(case_col) or (not case_col and WITH_RE.match(full)):
                _add_case_token(current, case_col or full)
                if party_col:
                    current["_party_parts"].append(party_col)
                if adv_col:
                    current["_adv_parts"].append(adv_col)
                continue

            if case_col:
                _add_case_token(current, case_col)
            if party_col:
                current["_party_parts"].append(party_col)
            if adv_col:
                current["_adv_parts"].append(adv_col)

            # Full-width remark continuation (no sr, mostly left/mid text)
            if (
                not sr
                and not adv_col
                and case_col
                and "REMARK" not in case_col.upper()
                and current.get("remark")
                and not CASE_ID_RE.match(case_col)
            ):
                current["remark"] = _clean(f"{current['remark']} {case_col}")

    flush()
    doc.close()
    return {
        "pdf": str(path),
        "page_count": page_count,
        "courtNo": court_no,
        "bench": bench,
        "judges": judges,
        "case_count": len(all_cases),
        "cases": all_cases,
    }


def _words_from_page(page: pymupdf.Page) -> list[Word]:
    raw = page.get_text("words")
    return [
        Word(float(t[0]), float(t[1]), float(t[2]), float(t[3]), str(t[4]))
        for t in raw
    ]


def _cluster_lines(words: list[Word], y_tol: float = 3.5) -> list[Line]:
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w.y0, w.x0))
    lines: list[Line] = []
    current = Line(y0=ordered[0].y0, y1=ordered[0].y1, words=[ordered[0]])
    for word in ordered[1:]:
        if abs(word.y_mid - current.y_mid) <= y_tol:
            current.words.append(word)
            current.y0 = min(current.y0, word.y0)
            current.y1 = max(current.y1, word.y1)
        else:
            lines.append(current)
            current = Line(y0=word.y0, y1=word.y1, words=[word])
    lines.append(current)
    return lines


def _column_text(line: Line, x_min: float, x_max: float) -> str:
    parts = [
        w.text
        for w in sorted(line.words, key=lambda w: w.x0)
        if x_min <= w.x_mid < x_max
    ]
    return " ".join(parts).strip()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _add_case_token(case: dict[str, Any], token: str) -> None:
    token = _clean(token)
    if not token:
        return
    case.setdefault("_case_raw", []).append(token)
    # Primary case ids only into caseNumber array
    if CASE_ID_RE.match(token):
        # strip trailing tags on same token
        base = re.split(r"\s+\[", token, maxsplit=1)[0].strip()
        base = re.sub(r"\s*/\s*", "/", base)
        if base and base not in case["caseNumber"]:
            case["caseNumber"].append(base)
    elif WITH_RE.match(token):
        # "with IA(L)/40485/2025"
        m = re.search(
            r"([A-Z][A-Z0-9()./\-]*\s*/\s*\d{1,5}\s*/\s*\d{4})",
            token,
            re.I,
        )
        if m:
            base = re.sub(r"\s*/\s*", "/", m.group(1).strip())
            if base not in case["caseNumber"]:
                case["caseNumber"].append(base)


def _finalize_case(case: dict[str, Any]) -> None:
    raw_cases = case.pop("_case_raw", []) or []
    party_parts = case.pop("_party_parts", []) or []
    adv_parts = case.pop("_adv_parts", []) or []

    if not case.get("caseNumber"):
        for token in raw_cases:
            if CASE_ID_RE.match(token):
                base = re.split(r"\s+\[", token, maxsplit=1)[0].strip()
                base = re.sub(r"\s*/\s*", "/", base)
                if base not in case["caseNumber"]:
                    case["caseNumber"].append(base)

    party = _build_party(party_parts)
    case["party"] = party or None
    case["advocates"] = _build_advocates(adv_parts)
    if case.get("remark"):
        case["remark"] = _clean(case["remark"])


def _build_party(parts: list[str]) -> str:
    """Join party column lines; normalize VS; keep petitioner VS respondent readable."""
    if not parts:
        return ""
    chunks: list[str] = []
    for part in parts:
        p = _clean(part)
        if not p:
            continue
        if VS_RE.match(p):
            chunks.append("VS")
            continue
        if VS_INLINE_RE.search(p):
            pre, post = VS_INLINE_RE.split(p, maxsplit=1)
            if pre.strip():
                chunks.append(pre.strip())
            chunks.append("VS")
            if post.strip():
                chunks.append(post.strip())
            continue
        chunks.append(p)

    # Collapse duplicate VS and join
    out: list[str] = []
    for chunk in chunks:
        if chunk == "VS" and out and out[-1] == "VS":
            continue
        out.append(chunk)
    text = " ".join(out)
    text = re.sub(r"\s+VS\s+", " VS ", text)
    return _clean(text)


_ADV_NOISE_RE = re.compile(
    r"^\d{1,3}/\d{1,3}$|"  # page markers like 2/33
    r"^(?:PETITIONER|RESPONDENT|RESP\.?|R\.?\s*NO\.?|FOR\s+RESP|ADV\.?|NIL|-)$|"
    r"^(?:VP\s+FILED|NOA\s+IS\s+FILED|FILED\.?)$",
    re.I,
)


def _build_advocates(parts: list[str]) -> list[str]:
    if not parts:
        return []
    tokens: list[str] = []
    for part in parts:
        p = _clean(part)
        if not p:
            continue
        pieces = [x.strip(" ,;") for x in p.split(",")] if "," in p else [p]
        for piece in pieces:
            if not piece or _ADV_NOISE_RE.match(piece):
                continue
            # Drop long office-note crumbs that leaked from other columns.
            if len(piece) > 80:
                continue
            if piece not in tokens:
                tokens.append(piece)
    return tokens
