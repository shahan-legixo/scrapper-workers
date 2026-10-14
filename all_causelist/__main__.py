"""CLI for Delhi, Odisha, Telangana, Bombay, and MPHC High Court cause lists."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

from .bombay import BombayCauseListError, fetch_bombay_cause_list
from .odisha import OdishaCauseListError, fetch_odisha_cause_list
from .delhi import LIST_LABELS, CauseListError, fetch_cause_list
from .mphc import MphcCauseListError, fetch_mphc_cause_list, write_json as write_mphc_json
from .supreme import SciCauseListError, fetch_sci_cause_list
from .telangana import (
    TelanganaCauseListError,
    fetch_telangana_cause_list,
    write_json as write_telangana_json,
)

CSV_FIELDS = (
    "date",
    "courtName",
    "courtNo",
    "court_no",
    "side",
    "judge",
    "list_type",
    "listingNo",
    "item_no",
    "caseNumber",
    "case_no",
    "party",
    "case_title",
    "advocate",
    "section",
    "case_stage",
    "remark",
    "remarks",
)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description=(
            "Download High Court cause lists "
            "(Delhi, Odisha, Telangana, Bombay, Madhya Pradesh)."
        )
    )
    sub = parser.add_subparsers(dest="court", required=True)

    delhi = sub.add_parser("delhi", help="Delhi High Court HTML cause lists.")
    delhi.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Cause-list date in YYYY-MM-DD. Defaults to today.",
    )
    delhi.add_argument(
        "--bench",
        default=None,
        help="Optional judge name or court value (for example 16~C).",
    )
    delhi.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output") / "delhi",
        help="Directory for JSON/CSV. Defaults to ./output/delhi.",
    )
    delhi.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between bench requests. Defaults to 1.",
    )

    odisha = sub.add_parser("odisha", help="Odisha High Court eCourts PDFs.")
    odisha.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Cause-list date in YYYY-MM-DD. Defaults to today.",
    )
    odisha.add_argument(
        "--bench",
        default=None,
        help="Optional bench name, list type, or serial number.",
    )
    odisha.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output") / "odisha",
        help="Directory for Odisha data. Defaults to ./output/odisha.",
    )
    odisha.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between PDF downloads. Defaults to 1.",
    )
    odisha.add_argument(
        "--no-parse",
        action="store_true",
        help="Download PDFs only; skip text extraction.",
    )

    telangana = sub.add_parser("telangana", help="Telangana High Court judge-wise HTML lists.")
    telangana.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Cause-list date in YYYY-MM-DD. Defaults to today.",
    )
    telangana.add_argument(
        "--bench",
        default=None,
        help="Optional judge name filter.",
    )
    telangana.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output") / "telangana",
        help="Directory for JSON. Defaults to ./output/telangana.",
    )
    telangana.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between judge requests. Defaults to 1.",
    )

    bombay = sub.add_parser("bombay", help="Bombay High Court PDF causelistFinal lists.")
    bombay.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Cause-list date in YYYY-MM-DD. Defaults to today.",
    )
    bombay.add_argument(
        "--coram",
        default=None,
        help="Optional coram / judge name substring filter.",
    )
    bombay.add_argument(
        "--all-types",
        action="store_true",
        help="Download weekly/chamber/production/officer PDFs too (default: Daily only).",
    )
    bombay.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output") / "bombay",
        help="Directory for Bombay data. Defaults to ./output/bombay.",
    )
    bombay.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between PDF downloads. Defaults to 1.",
    )
    bombay.add_argument(
        "--no-parse",
        action="store_true",
        help="Download PDFs only; skip text extraction.",
    )

    mphc = sub.add_parser(
        "mphc",
        help="Madhya Pradesh High Court Judge-tab HTML cause lists.",
    )
    mphc.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Cause-list date in YYYY-MM-DD. Defaults to today.",
    )
    mphc.add_argument(
        "--place",
        action="append",
        default=None,
        help="Bench place: JBP, IND, or GWL (repeatable). Default: all three.",
    )
    mphc.add_argument(
        "--mfb",
        default=None,
        help="List type: M (Motion), F (Final), L (Lok Adalat), S (Mediation). Default: all.",
    )
    mphc.add_argument(
        "--sbdb",
        default=None,
        help="Bench type: 1 (Single), 2 (Division), 3 (Full). Default: all.",
    )
    mphc.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output") / "mphc",
        help="Directory for JSON. Defaults to ./output/mphc.",
    )
    mphc.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between combo requests. Defaults to 1.",
    )

    all_courts = sub.add_parser(
        "all",
        help="Run Delhi, MPHC, Telangana, Bombay, and Odisha scrapers sequentially.",
    )
    all_courts.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Cause-list date in YYYY-MM-DD. Defaults to today.",
    )
    all_courts.add_argument(
        "--courts",
        default="all",
        help="Comma-separated subset, or 'all' (default).",
    )
    all_courts.add_argument(
        "--include-supreme",
        action="store_true",
        help="Also run Supreme Court scraper.",
    )
    all_courts.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between requests inside each scraper. Defaults to 1.",
    )
    all_courts.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Base output directory. Defaults to ./output.",
    )
    all_courts.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on first scraper failure.",
    )

    args = parser.parse_args(argv)
    if args.court == "all":
        return _run_all(args)
    if args.court == "delhi":
        return _run_delhi(args)
    if args.court == "odisha":
        return _run_odisha(args)
    if args.court == "telangana":
        return _run_telangana(args)
    if args.court == "mphc":
        return _run_mphc(args)
    return _run_bombay(args)


def _run_all(args: argparse.Namespace) -> int:
    # Root-level run_all.py lives next to the package.
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import run_all as run_all_mod

    argv = [
        "--date",
        args.date,
        "--delay",
        str(args.delay),
        "--output-dir",
        str(args.output_dir),
    ]
    if args.courts:
        argv.extend(["--courts", args.courts])
    if args.include_supreme:
        argv.append("--include-supreme")
    if args.fail_fast:
        argv.append("--fail-fast")
    return run_all_mod.main(argv)


def _run_delhi(args: argparse.Namespace) -> int:
    try:
        payload = fetch_cause_list(
            args.date,
            bench_filter=args.bench,
            delay_seconds=args.delay,
            on_progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except CauseListError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_dir / f"causelist-{payload['date']}"
    json_path = stem.with_suffix(".json")
    csv_path = stem.with_suffix(".csv")

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_delhi_csv(csv_path, payload)

    from .main_output import write_main_output_from_payload

    main_path = write_main_output_from_payload(
        output_dir,
        "delhi",
        payload,
        cause_date=payload.get("date"),
        court_name=payload.get("courtName"),
    )

    failed = [bench["judge"] for bench in payload["benches"] if bench.get("error")]
    print(
        f"Wrote {payload['bench_count']} benches and {payload['case_count']} cases "
        f"to {json_path}, {csv_path}, and {main_path}"
    )
    if failed:
        print(f"{len(failed)} bench(es) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def _run_odisha(args: argparse.Namespace) -> int:
    try:
        payload = fetch_odisha_cause_list(
            args.date,
            args.output_dir,
            bench_filter=args.bench,
            delay_seconds=args.delay,
            parse_pdfs=not args.no_parse,
            on_progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except OdishaCauseListError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    failed = [
        f"{item['bench']} ({item['list_type']})"
        for item in payload["benches"]
        if item.get("error")
    ]
    print(
        f"Wrote {payload['pdf_count']} PDFs / {payload.get('case_count', 0)} cases "
        f"for {payload['bench_count']} benches to {payload['output_dir']}"
    )
    if failed:
        print(f"{len(failed)} PDF(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def _run_telangana(args: argparse.Namespace) -> int:
    try:
        payload = fetch_telangana_cause_list(
            args.date,
            bench_filter=args.bench,
            delay_seconds=args.delay,
            on_progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except TelanganaCauseListError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    json_path = write_telangana_json(payload, args.output_dir)
    failed = [item["judge"] for item in payload["judges"] if item.get("error")]
    print(
        f"Wrote {payload['judge_count']} judges and {payload['case_count']} cases to {json_path}"
    )
    if failed:
        print(f"{len(failed)} judge(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def _run_mphc(args: argparse.Namespace) -> int:
    try:
        payload = fetch_mphc_cause_list(
            args.date,
            places=args.place,
            mfb_filter=args.mfb,
            sbdb_filter=args.sbdb,
            delay_seconds=args.delay,
            on_progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except MphcCauseListError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    json_path = write_mphc_json(payload, args.output_dir)
    failed = [
        f"{c['place']}/{c['mfb_label']}/{c['sbdb_label']}"
        for c in payload["combos"]
        if c.get("error")
    ]
    print(
        f"Wrote {payload['combo_count']} combo(s) and {payload['case_count']} cases "
        f"to {json_path}"
    )
    if failed:
        print(f"{len(failed)} combo(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def _run_bombay(args: argparse.Namespace) -> int:
    try:
        payload = fetch_bombay_cause_list(
            args.date,
            args.output_dir,
            coram_filter=args.coram,
            all_types=args.all_types,
            delay_seconds=args.delay,
            parse_pdfs=not args.no_parse,
            on_progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except BombayCauseListError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    failed = []
    for coram in payload["corams"]:
        for item in coram["pdfs"]:
            if item.get("error"):
                failed.append(f"{coram['coram']} / {item['list_type']}")
    case_count = payload.get("case_count", 0)
    print(
        f"Wrote {payload['pdf_count']} PDFs / {case_count} cases for "
        f"{payload['coram_count']} coram(s) to {payload['output_dir']}"
    )
    if failed:
        print(f"{len(failed)} PDF(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def _write_delhi_csv(path: Path, payload: dict) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for bench in payload["benches"]:
            for list_name, _label in LIST_LABELS:
                for row in bench["lists"][list_name]:
                    writer.writerow(
                        {
                            "date": row.get("date") or payload["date"],
                            "courtName": row.get("courtName") or payload.get("courtName"),
                            "court_no": bench["court_no"],
                            "side": bench["side"],
                            "judge": row.get("judge") or bench["judge"],
                            "list_type": row.get("list_type") or _label,
                            **row,
                        }
                    )


if __name__ == "__main__":
    raise SystemExit(main())
