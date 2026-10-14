"""PyMuPDF word-coordinate parser for Orissa High Court cause-list PDFs.

Column bands (page-width fractions) from 2026-10-12 Weekly Daily samples:
  left  < CASE_FRAC  — serial, case id, CNR, order notes
  mid   CASE..PARTY  — parties (Vs / VS)
  right >= PARTY     — advocates
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

# Vs often sits near ~0.39; keep it in the party band.
CASE_FRAC = 0.375
PARTY_FRAC = 0.66

COURT_RE = re.compile(
    r"^(?:COURT\s*NO\.?\s*[-–]?\s*([IVXLC\d]+)|CHIEF\s+JUSTICE'?S\s+COURT)",
    re.I,
)
JUDGE_RE = re.compile(r"^(?:THE\s+)?HON[''`′]?BLE\b", re.I)
TIME_RE = re.compile(r"^AT\s+\d{1,2}[:.]\d{2}\s*(?:A\.?M\.?|P\.?M\.?)\s*$", re.I)
SECTION_RE = re.compile(
    r"^(TOP\s+OF\s+THE\s+LIST|FOR\s+ORDERS|FOR\s+ADMISSION|FOR\s+FINAL\s+HEARING|"
    r"FRESH\s+ADMISSION|SPECIALLY\s+ASSIGNED|OLD\s+MATTERS|STAYED\s+MATTERS|"
    r"FOR\s+JUDGEMENT|FOR\s+DELIVERY|PRIORITISED|WITHDRAWAL|"
    r"MENTION|PART[- ]HEARD).*$",
    re.I,
)
SERIAL_CASE_RE = re.compile(
    r"^(\d{1,3})\)\s*([A-Z][A-Z0-9()./\-]*\s*/\s*\d{1,5}\s*/\s*\d{4})\b",
    re.I,
)
SERIAL_ONLY_RE = re.compile(r"^(\d{1,3})\)\s*$")
CASE_ID_RE = re.compile(
    r"^[A-Z][A-Z0-9()./\-]*\s*/\s*\d{1,5}\s*/\s*\d{4}\b",
    re.I,
)
CNR_RE = re.compile(r"\[\s*(ODHC\d+)\s*\]", re.I)
WITH_RE = re.compile(
    r"^WITH\s+([A-Z][A-Z0-9()./\-]*\s*/\s*\d{1,5}\s*/\s*\d{4})",
    re.I,
)
VS_RE = re.compile(r"^\s*V/?S\.?\s*$|^\s*Versus\s*$", re.I)
VS_INLINE_RE = re.compile(r"\bV/?S\.?\b|\bVersus\b", re.I)
PAGE_FOOTER_RE = re.compile(r"^\d+\s*/\s*\d+\s*$")
NOISE_RE = re.compile(
    r"HIGH\s+COURT\s+OF\s+ORISSA|LIST\s+OF\s+BUSINESS|"
    r"WEEKLY\s+DAILY|WEEKLY\s*$|^DAILY$|"
    r"HYBRID\s+ARRANGEMENT|VIRTUAL/PHYSICAL|"
    r"Learned\s+counsels|THIS\s+BENCH\s+WILL|"
    r"^=+$|^-+$|^\d{2}-\d{2}-\d{4}\b",
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


def parse_odisha_cause_list_pdf(pdf_path: Path | str) -> dict[str, Any]:
    """Parse one Odisha cause-list PDF into structured cases."""
    path = Path(pdf_path)
    doc = pymupdf.open(path)
    page_count = len(doc)

    court_no: str | None = None
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
        case_x = width * CASE_FRAC
        party_x = width * PARTY_FRAC
        words = _words_from_page(page)
        lines = _cluster_lines(words)

        for line in lines:
            full = _clean(line.text)
            if not full or PAGE_FOOTER_RE.match(full) or NOISE_RE.search(full):
                # Still allow judge / court / time even if noise also matches? Skip pure noise.
                if not (
                    COURT_RE.match(full)
                    or JUDGE_RE.match(full)
                    or TIME_RE.match(full)
                    or SECTION_RE.match(full)
                ):
                    continue

            case_col = _clean(_column_text(line, 0.0, case_x))
            party_col = _clean(_column_text(line, case_x, party_x))
            adv_col = _clean(_column_text(line, party_x, width + 1.0))

            court_m = COURT_RE.match(full) or COURT_RE.match(case_col) or COURT_RE.match(party_col)
            if court_m:
                flush()
                if court_m.lastindex and court_m.group(1):
                    court_no = court_m.group(1).strip()
                elif "CHIEF" in full.upper():
                    court_no = "CJ"
                judges = []
                continue

            if TIME_RE.match(full) or TIME_RE.match(party_col) or TIME_RE.match(case_col):
                session_time = full if TIME_RE.match(full) else (party_col or case_col)
                continue

            if JUDGE_RE.match(full) or JUDGE_RE.match(party_col) or JUDGE_RE.match(case_col):
                name = full if JUDGE_RE.match(full) else (party_col or case_col)
                name = _clean(name)
                if name and name not in judges:
                    judges.append(name)
                continue

            if SECTION_RE.match(full) or (
                case_col and SECTION_RE.match(case_col) and not SERIAL_CASE_RE.match(case_col)
            ):
                label = full if SECTION_RE.match(full) else case_col
                # Avoid treating long case remarks as sections
                if len(label) < 80:
                    section = label
                    continue

            # New case: "1) CRLA/423/2023" (often spans left; party/adv on same row)
            serial_src = case_col or full
            serial_m = SERIAL_CASE_RE.match(serial_src)
            if not serial_m and case_col:
                # "1)" alone in left with case id elsewhere in left
                only = SERIAL_ONLY_RE.match(case_col.split()[0] + ")") if case_col else None
                if re.match(r"^\d{1,3}\)", case_col):
                    serial_m = SERIAL_CASE_RE.match(case_col) or re.match(
                        r"^(\d{1,3})\)\s*(.*)$",
                        case_col,
                    )
            if serial_m and serial_m.group(1):
                listing = serial_m.group(1).lstrip("0") or serial_m.group(1)
                case_token = ""
                if serial_m.lastindex and serial_m.lastindex >= 2:
                    case_token = _clean(serial_m.group(2) or "")
                if case_token and not CASE_ID_RE.match(case_token):
                    # group(2) may be trailing junk after "1)"
                    case_token = ""
                    m2 = CASE_ID_RE.search(case_col or full)
                    if m2:
                        case_token = m2.group(0)

                flush()
                current = {
                    "listingNo": listing,
                    "caseNumber": [],
                    "cnr": None,
                    "_case_raw": [],
                    "_party_parts": [],
                    "_adv_parts": [],
                    "_remark_parts": [],
                    "_in_with": False,
                    "party": None,
                    "advocates": None,
                    "remark": None,
                    "section": section,
                    "session_time": session_time,
                    "courtNo": court_no,
                    "judges": list(judges),
                    "page": page_index + 1,
                }
                if case_token:
                    _add_case_token(current, case_token)
                # Remainder of left after serial/case may be remark start
                rest_left = _strip_serial_prefix(case_col)
                if rest_left and not CASE_ID_RE.match(rest_left):
                    if not CNR_RE.search(rest_left):
                        # Vs sometimes lands in left remnant — route to party
                        if VS_RE.match(rest_left) or VS_INLINE_RE.fullmatch(rest_left or ""):
                            current["_party_parts"].append("VS")
                        else:
                            current["_remark_parts"].append(rest_left)
                if party_col:
                    current["_party_parts"].append(party_col)
                if adv_col:
                    current["_adv_parts"].append(adv_col)
                continue

            if current is None:
                continue

            # WITH-linked matter (must look like WITH CASE/NO/YEAR — not "With WP(C) No.…")
            with_src = case_col or ""
            with_m = WITH_RE.match(with_src)
            if with_m:
                current["_in_with"] = True
                _add_case_token(current, with_m.group(0))
                continue

            # Linked matter case id after WITH started
            if current.get("_in_with") and case_col and CASE_ID_RE.match(case_col):
                _add_case_token(current, case_col)
                continue

            if case_col:
                cnr_m = CNR_RE.search(case_col)
                if cnr_m:
                    if not current.get("cnr"):
                        current["cnr"] = cnr_m.group(1).upper()
                    leftover = _clean(CNR_RE.sub("", case_col))
                    if leftover:
                        if VS_RE.match(leftover):
                            if not current.get("_in_with"):
                                current["_party_parts"].append("VS")
                        elif CASE_ID_RE.match(leftover):
                            _add_case_token(current, leftover)
                        elif not current.get("_in_with"):
                            current["_remark_parts"].append(leftover)
                elif CASE_ID_RE.match(case_col):
                    if current.get("_in_with"):
                        _add_case_token(current, case_col)
                    elif not current["caseNumber"]:
                        _add_case_token(current, case_col)
                    else:
                        # Bare CASE/NO/YEAR under primary starts a connected block
                        current["_in_with"] = True
                        _add_case_token(current, case_col)
                elif VS_RE.match(case_col):
                    if not current.get("_in_with"):
                        current["_party_parts"].append("VS")
                elif not current.get("_in_with"):
                    current["_remark_parts"].append(case_col)

            if current.get("_in_with"):
                continue

            if party_col:
                # Drop mid-column crumbs from wrapped order notes (short stopwords)
                if not _is_party_noise(party_col):
                    current["_party_parts"].append(party_col)
            if adv_col:
                current["_adv_parts"].append(adv_col)

    flush()
    doc.close()
    return {
        "pdf": str(path),
        "page_count": page_count,
        "courtNo": court_no,
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


def _strip_serial_prefix(text: str) -> str:
    text = _clean(text)
    text = re.sub(r"^\d{1,3}\)\s*", "", text)
    text = re.sub(
        r"^[A-Z][A-Z0-9()./\-]*\s*/\s*\d{1,5}\s*/\s*\d{4}\b\s*",
        "",
        text,
        flags=re.I,
    )
    return _clean(text)


def _normalize_case_id(token: str) -> str:
    token = _clean(token)
    token = re.sub(r"^(?:WITH|with)\s+", "", token)
    token = re.split(r"\s+\[", token, maxsplit=1)[0].strip()
    m = re.search(
        r"([A-Z][A-Z0-9()./\-]*\s*/\s*\d{1,5}\s*/\s*\d{4})",
        token,
        re.I,
    )
    if not m:
        return ""
    return re.sub(r"\s*/\s*", "/", m.group(1).strip())


def _add_case_token(case: dict[str, Any], token: str) -> None:
    token = _clean(token)
    if not token:
        return
    case.setdefault("_case_raw", []).append(token)
    base = _normalize_case_id(token)
    if base and base not in case["caseNumber"]:
        case["caseNumber"].append(base)


def _finalize_case(case: dict[str, Any]) -> None:
    case.pop("_case_raw", None)
    case.pop("_in_with", None)
    party_parts = case.pop("_party_parts", []) or []
    adv_parts = case.pop("_adv_parts", []) or []
    remark_parts = case.pop("_remark_parts", []) or []

    case["party"] = _build_party(party_parts) or None
    case["advocates"] = _build_advocates(adv_parts)
    remark = _clean(" ".join(remark_parts))
    # Drop tiny noise crumbs from wrapped order notes
    if remark and len(remark) < 3:
        remark = None
    case["remark"] = remark or None


_PARTY_NOISE_RE = re.compile(
    r"^(?:may|kindly|be|seen|at|fl\.?[A-Z0-9]+|copy|not|served|order|dtd?\.?|"
    r"ia|no\.?|for|further|orders?|spec\.?|adj\.?|mention|p/?l|\[p/?l\]|"
    r"affidavit|connected|appeals?)$",
    re.I,
)


def _is_party_noise(text: str) -> bool:
    """True only for short mid-column order-note crumbs, not real party names."""
    text = _clean(text)
    if not text:
        return True
    if VS_RE.match(text):
        return False
    tokens = text.split()
    # All-lowercase crumb lines (wrapped order notes) — drop.
    if text == text.lower() and len(tokens) <= 6:
        return True
    # Keep anything that looks like a name / org (has an uppercase word > 2 chars
    # that is not a noise token), or is longer than 3 tokens.
    if len(tokens) > 3:
        return False
    if any(
        t[:1].isupper() and len(t) > 2 and not _PARTY_NOISE_RE.match(t.strip(",;"))
        for t in tokens
    ):
        return False
    return all(_PARTY_NOISE_RE.match(t.strip(",;")) for t in tokens)


def _build_party(parts: list[str]) -> str:
    if not parts:
        return ""
    chunks: list[str] = []
    for part in parts:
        p = _clean(part)
        if not p or _is_party_noise(p):
            continue
        if VS_RE.match(p):
            chunks.append("VS")
            continue
        if VS_INLINE_RE.search(p):
            pre, post = VS_INLINE_RE.split(p, maxsplit=1)
            if pre.strip() and not _is_party_noise(pre):
                chunks.append(pre.strip())
            chunks.append("VS")
            if post.strip() and not _is_party_noise(post):
                chunks.append(post.strip())
            continue
        chunks.append(p)
    out: list[str] = []
    for chunk in chunks:
        if chunk == "VS" and out and out[-1] == "VS":
            continue
        out.append(chunk)
    text = " ".join(out)
    text = re.sub(r"\s+VS\s+", " VS ", text)
    text = re.sub(r"\s+VS\s*$", "", text)
    return _clean(text)


def _build_advocates(parts: list[str]) -> list[str]:
    if not parts:
        return []
    joined = _clean(", ".join(_clean(p) for p in parts if _clean(p)))
    if not joined:
        return []
    # Prefer line-ish chunks first; also split long comma lists.
    tokens: list[str] = []
    for part in parts:
        p = _clean(part)
        if not p:
            continue
        if "," in p:
            for piece in p.split(","):
                piece = piece.strip(" ,;")
                if piece and piece not in tokens:
                    tokens.append(piece)
        elif p not in tokens:
            tokens.append(p)
    return tokens
