"""PyMuPDF layout parser for Supreme Court cause-list PDFs (single source of truth).

Columns (page-width fractions):
  left  — serial, case number, category
  mid   — petitioner / Versus / respondent, IA notes
  right — advocates

Public case rows use real fields only (no raw_left / raw_middle / raw_right).
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pymupdf

# Column bands as fractions of page width (A4 cause-list layout).
LEFT_FRACTION = 0.30  # SNo + Case No + category
MID_FRACTION = 0.70  # Petitioner / Respondent
# Right = advocates (>= MID_FRACTION)

HEADER_NOISE = re.compile(
    r"SUPREME COURT OF INDIA|DAILY CAUSE LIST FOR DATED|"
    r"IT WILL BE APPRECIATED|LISTED BEFORE ALL THE COURTS|"
    r"Petitioner/Respondent|SNo\.|Case No\.|"
    r"^NOTE\s*:-|^LISTING IS AUTOMATIC",
    re.I,
)
# PDF text often has a colon: "COURT NO. : 5" / "COURT NO.:5"
COURT_RE = re.compile(
    r"^(CHIEF JUSTICE'?S COURT|"
    r"COURT\s*NO\.?\s*:?\s*\d+|"
    r"Registrar Court No\.?\s*:?\s*\d+)$",
    re.I,
)
JUDGE_RE = re.compile(r"^HON[''`′]?BLE\b", re.I)
REGISTRAR_RE = re.compile(
    r"^(?:SH\.|SMT\.|MS\.|MR\.)\s+.+,?\s*REGISTRAR",
    re.I,
)
TIME_RE = re.compile(r"^\(TIME\s*:", re.I)
SECTION_HINTS = (
    "HEARING",
    "APPLICATION",
    "MATTERS",
    "FOR ORDERS",
    "FOR ADMISSION",
    "AFTER NOTICE",
    "ADMITTED",
)
CASE_TYPE_RE = re.compile(
    r"^(?:C\.?A\.?|SLP(?:\([A-Za-z]+\))?|W\.?P\.?(?:\(\w+\))?|"
    r"CRL\.?A?\.?|Crl\.A\.?|CIVIL|CRIMINAL|T\.?P\.?(?:\(\w+\))?|"
    r"R\.?P\.?|MA|Diary|DIARY|CONT\.?|OR\.?|SMC|SMW(?:\(\w+\))?|"
    r"ARBIT\.?|TRANSFER)\b",
    re.I,
)
CATEGORY_RE = re.compile(
    r"^(?:[IVX]+(?:-[A-Z])?|PIL-W|PIL-C|\(SCLSC\)|[XIV]+-?[A-Z]?)$",
    re.I,
)
SERIAL_RE = re.compile(r"^\d{1,4}$")
ADVOCATE_ENTRY_RE = re.compile(
    r"(?:PETITIONER-IN-PERSON|"
    r"(?:[A-Z][A-Z0-9.&'/\-]*\s+|&\s+)*[A-Z][A-Z0-9.&'/\-]*\s*-\s*\d{3,5}"
    r"(?:\s*,?\s*\[[PR]-\d+\])*)",
    re.I,
)
IA_RE = re.compile(
    r"IA\s+No\.\s*([\d/\-]+)\s*(?:-\s*([^\[\n]+?))?(?=IA\s+No\.|\[|$)",
    re.I,
)
MENTION_RE = re.compile(r"\{?\s*Mention\s+Memo\s*\}?", re.I)


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


def _is_noise_line(text: str) -> bool:
    if not text.strip():
        return True
    if HEADER_NOISE.search(text):
        return True
    if TIME_RE.match(text.strip()):
        return True
    if text.strip().upper().startswith("MISCELLANEOUS HEARING"):
        return True
    if text.strip().upper().startswith("REGULAR HEARING"):
        return True
    return False


def _is_section_line(text: str, left: str, mid: str, right: str) -> bool:
    joined = text.strip()
    if not joined or SERIAL_RE.match(left):
        return False
    # IA / mention / ONLY-note lines are case body, not section headers
    if re.search(r"\bIA\s+No\.", joined, re.I):
        return False
    if MENTION_RE.search(joined):
        return False
    if re.search(r"\[\s*ONLY\b", joined, re.I):
        return False
    if mid.strip().upper() in {"APPLICATION", "APPLICATION."}:
        return False
    if left and CASE_TYPE_RE.match(left.split()[0] if left.split() else ""):
        return False
    # Full-width section titles usually sit in middle with empty left serial
    if not left and mid and not right:
        upper = mid.upper()
        return any(h in upper for h in SECTION_HINTS) and len(mid) > 8
    if not left and not right and mid:
        upper = mid.upper()
        return any(h in upper for h in SECTION_HINTS)
    if not SERIAL_RE.match(left or "") and not CASE_TYPE_RE.search(left or ""):
        upper = joined.upper()
        if any(h in upper for h in SECTION_HINTS) and "Versus" not in joined:
            if not re.search(r"-\s*\d{3,5}\b", joined):
                return True
    return False


def _split_advocates(right_text: str) -> tuple[list[str], list[str]]:
    cleaned = MENTION_RE.sub(" ", right_text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    if not cleaned:
        return [], []

    chunks = [m.group(0) for m in ADVOCATE_ENTRY_RE.finditer(cleaned)]
    if not chunks and cleaned:
        chunks = [cleaned]

    petitioners: list[str] = []
    respondents: list[str] = []
    seen_respondent = False

    for chunk in chunks:
        chunk = re.sub(r"\s+", " ", chunk).strip(" ,")
        if not chunk:
            continue
        chunk = re.sub(r"-\s+(\d{3,5})", r"-\1", chunk)
        # Keep "NULI & NULI-2021" style pairs readable
        chunk = re.sub(r"\s+&\s+", " & ", chunk)
        if re.search(r"PETITIONER-IN-PERSON", chunk, re.I):
            petitioners.append("PETITIONER-IN-PERSON")
        elif re.search(r"\[R-", chunk, re.I):
            seen_respondent = True
            respondents.append(chunk)
        elif re.search(r"\[P-", chunk, re.I):
            petitioners.append(chunk)
        elif seen_respondent:
            respondents.append(chunk)
        else:
            petitioners.append(chunk)

    return _dedupe(petitioners), _dedupe(respondents)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _parse_left_column(left_text: str) -> tuple[str, str, str]:
    """Return (item_no, case_number, case_category)."""
    tokens = left_text.split()
    if not tokens:
        return "", "", ""
    item_no = tokens[0] if SERIAL_RE.match(tokens[0]) else ""
    rest = tokens[1:] if item_no else tokens
    category = ""
    case_parts: list[str] = []
    for tok in rest:
        if CATEGORY_RE.match(tok):
            category = tok
            continue
        case_parts.append(tok)
    case_number = " ".join(case_parts).strip()
    if not category:
        m = re.search(
            r"\b([IVX]+(?:-[A-Z])?|PIL-[A-Z]|\(SCLSC\))\s*$",
            case_number,
            re.I,
        )
        if m:
            category = m.group(1)
            case_number = case_number[: m.start()].strip()
    return item_no, case_number, category


def _parse_middle(mid_text: str) -> tuple[str, str, list[dict[str, str]], bool]:
    text = mid_text.strip()
    mention = bool(MENTION_RE.search(text))
    text = MENTION_RE.sub(" ", text)
    # Keep IA listing note out of party names; still extract numbers below
    text = re.sub(
        r"\[\s*ONLY\s+IA\s+No\.[^\]]+\]",
        " ",
        text,
        flags=re.I,
    )
    applications: list[dict[str, str]] = []
    seen_ia: set[str] = set()
    for match in IA_RE.finditer(text):
        number = f"IA No. {match.group(1).strip()}"
        app_type = (match.group(2) or "").strip(" -")
        if number in seen_ia:
            for app in applications:
                if app["application_number"] == number and app_type:
                    app["type"] = app_type
            continue
        seen_ia.add(number)
        applications.append(
            {
                "application_number": number,
                "type": app_type,
            }
        )
    for match in re.finditer(r"IA\s+No\.\s*([\d/\-]+)", mid_text, flags=re.I):
        number = f"IA No. {match.group(1).strip()}"
        if number not in seen_ia:
            seen_ia.add(number)
            applications.append({"application_number": number, "type": ""})
    text = IA_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,")

    petitioner = ""
    respondent = ""
    if re.search(r"\bVersus\b", text, re.I):
        pre, post = re.split(r"\bVersus\b", text, maxsplit=1, flags=re.I)
        petitioner = pre.strip(" ,")
        respondent = post.strip(" ,")
        respondent = re.sub(
            r"DAILY CAUSE LIST FOR DATED\s*:?\s*\d{2}-\d{2}-\d{4}",
            "",
            respondent,
            flags=re.I,
        ).strip(" ,")
    else:
        petitioner = text

    for noise in ("FOR ADMISSION", "FOR ORDERS"):
        if respondent.upper().endswith(noise):
            respondent = respondent[: -len(noise)].strip(" ,")
        if petitioner.upper().endswith(noise) and not respondent:
            petitioner = petitioner[: -len(noise)].strip(" ,")

    return petitioner, respondent, applications, mention


def _line_has_case_start(left: str) -> bool:
    if not left:
        return False
    tokens = left.split()
    if not tokens:
        return False
    if not SERIAL_RE.match(tokens[0]):
        return False
    if len(tokens) == 1:
        return True
    return bool(CASE_TYPE_RE.match(tokens[1])) or bool(CASE_TYPE_RE.search(left))


def _case_from_blobs(
    *,
    left_blob: str,
    mid_blob: str,
    right_blob: str,
    page_no: int,
    court: str | None,
    judges: list[str],
    section: str | None,
) -> tuple[dict[str, Any], str | None]:
    item_no, case_number, case_category = _parse_left_column(left_blob)
    petitioner, respondent, applications, mention = _parse_middle(mid_blob)
    pet_advs, resp_advs = _split_advocates(right_blob)

    if section and applications:
        for app in applications:
            if not app.get("type") and "EARLY HEARING" in section.upper():
                app["type"] = "EARLY HEARING APPLICATION"
            elif not app.get("type"):
                app["type"] = section
            elif app["type"].upper() == "APPLICATION" and "EARLY HEARING" in section.upper():
                app["type"] = "EARLY HEARING APPLICATION"

    warning = None
    if not case_number or not re.search(r"\d", case_number):
        warning = "missing_or_weak_case_number"
    elif "DAILY CAUSE LIST" in (respondent or "").upper():
        warning = "header_leak_in_respondent"
    elif not petitioner and not respondent:
        warning = "missing_parties"

    case = {
        "item_no": item_no,
        "case_number": case_number,
        "case_category": case_category,
        "petitioner": petitioner,
        "respondent": respondent,
        "petitioner_advocates": pet_advs,
        "respondent_advocates": resp_advs,
        "applications": applications,
        "mention_memo": mention,
        "section": section,
        "court": court,
        "judges": list(judges),
        "page": page_no,
        "parse_warning": warning,
        # Internal only — stripped before public output.
        "_left_blob": left_blob,
        "_mid_blob": mid_blob,
        "_right_blob": right_blob,
    }
    return case, warning


def _blobs_from_content(content: list[dict[str, Any]]) -> tuple[str, str, str]:
    left_blob = " ".join(r["left"] for r in content if r.get("left")).strip()
    mid_blob = " ".join(r["mid"] for r in content if r.get("mid")).strip()
    right_blob = " ".join(r["right"] for r in content if r.get("right")).strip()
    return left_blob, mid_blob, right_blob


def _public_case(case: dict[str, Any]) -> dict[str, Any]:
    """Drop internal column blobs; keep real parsed fields only."""
    return {k: v for k, v in case.items() if not k.startswith("_")}


def _extend_case_with_content(
    case: dict[str, Any], content: list[dict[str, Any]]
) -> str | None:
    left_extra, mid_extra, right_extra = _blobs_from_content(content)
    left_blob = f"{case.get('_left_blob', '')} {left_extra}".strip()
    mid_blob = f"{case.get('_mid_blob', '')} {mid_extra}".strip()
    right_blob = f"{case.get('_right_blob', '')} {right_extra}".strip()
    rebuilt, warning = _case_from_blobs(
        left_blob=left_blob,
        mid_blob=mid_blob,
        right_blob=right_blob,
        page_no=case.get("page") or 0,
        court=case.get("court"),
        judges=list(case.get("judges") or []),
        section=case.get("section"),
    )
    case.update(rebuilt)
    return warning


def parse_page(
    page: pymupdf.Page,
    page_no: int,
    *,
    court: str | None,
    judges: list[str],
    section: str | None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    str | None,
    list[str],
    str | None,
    list[str],
]:
    """Parse one page.

    Returns:
      cases, leading_continuation_rows, court, judges, section, warnings
    """
    width = float(page.rect.width)
    left_x = width * LEFT_FRACTION
    mid_x = width * MID_FRACTION
    words = _words_from_page(page)
    lines = _cluster_lines(words)
    warnings: list[str] = []
    cases: list[dict[str, Any]] = []

    rows: list[dict[str, Any]] = []
    for line in lines:
        full = line.text
        if _is_noise_line(full):
            continue
        left = _column_text(line, 0.0, left_x)
        mid = _column_text(line, left_x, mid_x)
        right = _column_text(line, mid_x, width + 1.0)
        centered = mid or full
        if COURT_RE.match(centered.strip()):
            court = centered.strip()
            judges = []
            continue
        if JUDGE_RE.match(centered.strip()) or REGISTRAR_RE.match(centered.strip()):
            name = centered.strip()
            if name not in judges:
                judges.append(name)
            continue
        if _is_section_line(full, left, mid, right):
            section = (mid or full).strip()
            continue
        rows.append(
            {
                "kind": "content",
                "left": left,
                "mid": mid,
                "right": right,
                "y": line.y_mid,
            }
        )

    starts: list[int] = []
    for i, row in enumerate(rows):
        if _line_has_case_start(row.get("left") or ""):
            starts.append(i)

    leading: list[dict[str, Any]] = rows[: starts[0]] if starts else rows

    for s_i, start in enumerate(starts):
        end = starts[s_i + 1] if s_i + 1 < len(starts) else len(rows)
        content = rows[start:end]
        left_blob, mid_blob, right_blob = _blobs_from_content(content)
        case, warning = _case_from_blobs(
            left_blob=left_blob,
            mid_blob=mid_blob,
            right_blob=right_blob,
            page_no=page_no,
            court=court,
            judges=judges,
            section=section,
        )
        if warning:
            warnings.append(f"page {page_no} item {case['item_no']}: {warning}")
        cases.append(case)

    return cases, leading, court, judges, section, warnings


def parse_cause_list_pdf(pdf_path: Path) -> dict[str, Any]:
    doc = pymupdf.open(pdf_path)
    all_cases: list[dict[str, Any]] = []
    all_warnings: list[str] = []
    court: str | None = None
    judges: list[str] = []
    section: str | None = None
    page_records: list[dict[str, Any]] = []

    for page_index in range(len(doc)):
        page = doc[page_index]
        page_no = page_index + 1
        cases, leading, court, judges, section, warnings = parse_page(
            page,
            page_no,
            court=court,
            judges=judges,
            section=section,
        )
        if leading and all_cases:
            warn = _extend_case_with_content(all_cases[-1], leading)
            if warn:
                all_warnings.append(
                    f"page {page_no} continuation item "
                    f"{all_cases[-1].get('item_no')}: {warn}"
                )
        all_cases.extend(cases)
        all_warnings.extend(warnings)
        page_records.append(
            {
                "page": page_no,
                "case_count": len(cases),
                "continuation_rows": len(leading),
                "court": court,
                "section": section,
                "warnings": warnings,
            }
        )

    doc.close()
    return {
        "source_pdf": str(pdf_path),
        "page_count": len(page_records),
        "case_count": len(all_cases),
        "warning_count": len(all_warnings),
        "warnings": all_warnings,
        "pages": page_records,
        "cases": [_public_case(case) for case in all_cases],
    }


def _default_out_dir(pdf_path: Path) -> Path:
    # …/causelist-DATE/pdfs/miscellaneous/main.pdf → …/causelist-DATE/structured
    resolved = pdf_path.resolve()
    for parent in resolved.parents:
        if parent.name == "pdfs":
            return parent.parent / "structured"
    return resolved.parent / "structured"


def _output_stem(pdf_path: Path) -> str:
    # miscellaneous/main.pdf → miscellaneous_main
    parent = pdf_path.parent.name
    stem = pdf_path.stem
    if parent and parent not in {".", "pdfs"}:
        return f"{parent}_{stem}"
    return stem


def write_outputs(
    result: dict[str, Any],
    pdf_path: Path,
    out_dir: Path,
    *,
    write_jsonl: bool,
) -> tuple[Path, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _output_stem(pdf_path)
    json_path = out_dir / f"{stem}_cases.json"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    jsonl_path: Path | None = None
    if write_jsonl:
        jsonl_path = out_dir / f"{stem}_cases.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as fh:
            for case in result["cases"]:
                fh.write(json.dumps(case, ensure_ascii=False) + "\n")
    return json_path, jsonl_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Layout-aware parse of SCI cause-list PDFs → structured JSON."
    )
    parser.add_argument("pdf", type=Path, help="Path to a downloaded cause-list PDF")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: <causelist-day>/structured)",
    )
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="Also write one JSON object per case as .jsonl",
    )
    args = parser.parse_args(argv)

    pdf_path = args.pdf
    if not pdf_path.is_file():
        print(f"PDF not found: {pdf_path}")
        return 1

    print(f"[+] Parsing {pdf_path}")
    result = parse_cause_list_pdf(pdf_path)
    out_dir = args.out or _default_out_dir(pdf_path)
    json_path, jsonl_path = write_outputs(
        result, pdf_path, out_dir, write_jsonl=args.jsonl
    )
    print(
        f"[+] cases={result['case_count']} pages={result['page_count']} "
        f"warnings={result['warning_count']}"
    )
    print(f"[+] Wrote {json_path}")
    if jsonl_path:
        print(f"[+] Wrote {jsonl_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
