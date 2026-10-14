"""CLI for Supreme Court of India cause lists."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .scrape import SciCauseListError, fetch_sci_cause_list


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description=(
            "Download Supreme Court Cause Lists at a Glance PDFs "
            "and parse judge → cases JSON."
        )
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Cause-list date in YYYY-MM-DD. Defaults to today.",
    )
    parser.add_argument(
        "--list-type",
        default=None,
        help=(
            "Optional filter: miscellaneous, chamber, single_judge, "
            "registrar, advance."
        ),
    )
    parser.add_argument(
        "--judge",
        default=None,
        help="Optional judge / court substring filter after parse.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Directory for SCI data. Defaults to ./output.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between PDF downloads. Defaults to 1.",
    )
    parser.add_argument(
        "--no-parse",
        action="store_true",
        help="Download PDFs only; skip text extraction / structured parse.",
    )
    args = parser.parse_args(argv)

    try:
        payload = fetch_sci_cause_list(
            args.date,
            args.output_dir,
            list_type_filter=args.list_type,
            judge_filter=args.judge,
            delay_seconds=args.delay,
            parse_pdfs=not args.no_parse,
            on_progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except SciCauseListError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    failed = [
        f"{item['list_type']}/{item['msb']}"
        for item in payload["files"]
        if item.get("error")
    ]
    parse_failed = [
        f"{item['list_type']}/{item['msb']}"
        for item in payload["files"]
        if item.get("parse_error")
    ]
    case_count = 0
    bench_count = 0
    for item in payload["files"]:
        for bench in item.get("benches") or []:
            bench_count += 1
            case_count += len(bench.get("cases") or [])

    print(
        f"Wrote {payload['pdf_count']} PDF(s), {bench_count} bench(es), "
        f"{case_count} case row(s) to {payload['output_dir']}"
    )
    if failed:
        print(f"{len(failed)} PDF(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    if parse_failed:
        print(
            f"{len(parse_failed)} parse warning(s): {', '.join(parse_failed)}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
