"""Run all High Court cause-list scrapers in one pass.

Usage:
  uv run python run_all.py
  uv run python run_all.py --date 2026-10-12
  uv run python run_all.py --courts delhi,odisha,mphc
  python -m all_causelist all --date 2026-10-12
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ProgressFn = Callable[[str], None]


def _format_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(seconds, 60)
    return f"{int(mins)}m {secs:.1f}s"


def _progress(court: str) -> ProgressFn:
    return lambda msg: print(f"  [{court}] {msg}", flush=True)


def run_delhi(cause_date: str, output_base: Path) -> dict[str, Any]:
    import csv

    from all_causelist.delhi import LIST_LABELS, fetch_cause_list

    out_dir = output_base / "delhi"
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    payload = fetch_cause_list(
        cause_date,
        on_progress=_progress("Delhi"),
    )
    stem = out_dir / f"causelist-{cause_date}"
    json_path = stem.with_suffix(".json")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    from all_causelist.main_output import write_main_output_from_payload
  

    #create main file ->will delete later
    main_path = write_main_output_from_payload(
        out_dir,
        "delhi",
        payload,
        cause_date=cause_date,
        court_name=payload.get("courtName"),
    )

    return {
        "status": "SUCCESS",
        "case_count": payload.get("case_count", 0),
        "bench_count": payload.get("bench_count", 0),
        "duration": time.time() - t0,
        "json_path": str(json_path),
        "main_output": str(main_path),
    }


def run_bombay(cause_date: str, output_base: Path, delay: float = 1.0) -> dict[str, Any]:
    from all_causelist.bombay import fetch_bombay_cause_list

    out_dir = output_base / "bombay"
    t0 = time.time()
    payload = fetch_bombay_cause_list(
        cause_date,
        out_dir,
        on_progress=_progress("Bombay"),
    )
    main_path = Path(payload.get("output_dir") or out_dir) / "mainOutput.json"
    return {
        "status": "SUCCESS",
        "case_count": payload.get("case_count", 0),
        "pdf_count": payload.get("pdf_count", 0),
        "coram_count": payload.get("coram_count", 0),
        "duration": time.time() - t0,
        "output_dir": str(payload.get("output_dir") or out_dir),
        "main_output": str(main_path) if main_path.exists() else None,
    }


def run_telangana(cause_date: str, output_base: Path, delay: float = 1.0) -> dict[str, Any]:
    from all_causelist.telangana import fetch_telangana_cause_list, write_json

    out_dir = output_base / "telangana"
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    payload = fetch_telangana_cause_list(
        cause_date,
        delay_seconds=delay,
        on_progress=_progress("Telangana"),
    )
    json_path = write_json(payload, out_dir)
    return {
        "status": "SUCCESS",
        "case_count": payload.get("case_count", 0),
        "judge_count": payload.get("judge_count", 0),
        "duration": time.time() - t0,
        "json_path": str(json_path),
        "main_output": str(out_dir / "mainOutput.json"),
    }


def run_odisha(cause_date: str, output_base: Path, delay: float = 1.0) -> dict[str, Any]:
    from all_causelist.odisha import fetch_odisha_cause_list

    out_dir = output_base / "odisha"
    t0 = time.time()
    payload = fetch_odisha_cause_list(
        cause_date,
        out_dir,
        delay_seconds=delay,
        on_progress=_progress("Odisha"),
    )
    main_path = Path(payload.get("output_dir") or out_dir) / "mainOutput.json"
    return {
        "status": "SUCCESS",
        "case_count": payload.get("case_count", 0),
        "pdf_count": payload.get("pdf_count", 0),
        "bench_count": payload.get("bench_count", 0),
        "duration": time.time() - t0,
        "output_dir": str(payload.get("output_dir") or out_dir),
        "main_output": str(main_path) if main_path.exists() else None,
    }


def run_mphc(cause_date: str, output_base: Path, delay: float = 1.0) -> dict[str, Any]:
    from all_causelist.mphc import fetch_mphc_cause_list, write_json

    out_dir = output_base / "mphc"
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    payload = fetch_mphc_cause_list(
        cause_date,
        delay_seconds=delay,
        on_progress=_progress("MPHC"),
    )
    json_path = write_json(payload, out_dir)
    return {
        "status": "SUCCESS",
        "case_count": payload.get("case_count", 0),
        "combo_count": payload.get("combo_count", 0),
        "duration": time.time() - t0,
        "json_path": str(json_path),
        "main_output": str(out_dir / "mainOutput.json"),
    }


def run_supreme(cause_date: str, output_base: Path, delay: float = 1.0) -> dict[str, Any]:
    from all_causelist.supreme import fetch_sci_cause_list

    out_dir = output_base / "supreme"
    t0 = time.time()
    payload = fetch_sci_cause_list(
        cause_date,
        out_dir,
        delay_seconds=delay,
        on_progress=_progress("Supreme"),
    )
    main_path = payload.get("main_output") or (
        Path(payload.get("output_dir") or out_dir) / "mainOutput.json"
    )
    return {
        "status": "SUCCESS",
        "pdf_count": payload.get("pdf_count", 0),
        "case_count": payload.get("case_count", 0),
        "duration": time.time() - t0,
        "output_dir": str(payload.get("output_dir") or out_dir),
        "main_output": str(main_path),
    }


# Default High Court set (supreme only with --include-supreme).
DEFAULT_COURTS = ("delhi", "mphc", "telangana", "bombay", "odisha")

AVAILABLE_RUNNERS: dict[str, tuple[str, Callable[..., dict[str, Any]]]] = {
    "delhi": ("Delhi High Court", run_delhi),
    "mphc": ("Madhya Pradesh High Court", run_mphc),
    "telangana": ("Telangana High Court", run_telangana),
    "bombay": ("Bombay High Court", run_bombay),
    "odisha": ("Odisha High Court", run_odisha),
    "supreme": ("Supreme Court of India", run_supreme),
}


def _count_label(res: dict[str, Any]) -> str:
    if "case_count" in res:
        label = f"{res['case_count']} cases"
        if res.get("pdf_count"):
            label += f" / {res['pdf_count']} PDFs"
        return label
    if "pdf_count" in res:
        return f"{res['pdf_count']} PDFs"
    return "-"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run all High Court cause-list scrapers at once.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python run_all.py
  uv run python run_all.py --date 2026-10-12
  uv run python run_all.py --courts delhi,odisha,mphc
  uv run python run_all.py --include-supreme
  python -m all_causelist all --date 2026-10-12
        """,
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Cause-list date YYYY-MM-DD (default: today).",
    )
    parser.add_argument(
        "--courts",
        default="all",
        help=(
            "Comma-separated courts, or 'all' "
            f"(default: {', '.join(DEFAULT_COURTS)})."
        ),
    )
    parser.add_argument(
        "--include-supreme",
        action="store_true",
        help="Also run Supreme Court scraper.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between requests inside each scraper (default: 1.0).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "output",
        help="Base output directory (default: ./output).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on first scraper failure.",
    )
    return parser


def resolve_courts(
    courts_arg: str,
    *,
    include_supreme: bool = False,
) -> list[str]:
    if courts_arg.strip().lower() == "all":
        selected = list(DEFAULT_COURTS)
    else:
        selected = [c.strip().lower() for c in courts_arg.split(",") if c.strip()]

    if include_supreme and "supreme" not in selected:
        selected.append("supreme")

    unknown = [k for k in selected if k not in AVAILABLE_RUNNERS]
    if unknown:
        raise ValueError(
            f"Unknown court(s): {', '.join(unknown)}. "
            f"Available: {', '.join(AVAILABLE_RUNNERS)}"
        )
    return selected


def run_selected(
    selected_keys: list[str],
    *,
    cause_date: str,
    output_dir: Path,
    delay: float,
    fail_fast: bool = False,
) -> tuple[dict[str, dict[str, Any]], bool]:
    results: dict[str, dict[str, Any]] = {}
    has_failure = False

    for idx, key in enumerate(selected_keys, 1):
        name, runner_fn = AVAILABLE_RUNNERS[key]
        print(f"\n[{idx}/{len(selected_keys)}] >>> Running {name} ({key})...")
        t0 = time.time()
        try:
            res = runner_fn(cause_date, output_dir, delay=delay)
            results[key] = res
            dur = _format_time(res.get("duration", time.time() - t0))
            if res.get("status") == "SUCCESS":
                print(f"  [OK] {name} completed in {dur} ({_count_label(res)})")
            else:
                has_failure = True
                print(
                    f"  [FAILED] {name}: {res.get('error', 'unknown')}",
                    file=sys.stderr,
                )
                if fail_fast:
                    break
        except Exception as exc:
            has_failure = True
            results[key] = {
                "status": "FAILED",
                "error": str(exc),
                "duration": time.time() - t0,
            }
            print(
                f"  [ERROR] {name} raised in {_format_time(time.time() - t0)}: {exc}",
                file=sys.stderr,
            )
            if fail_fast:
                print("Aborting due to --fail-fast.", file=sys.stderr)
                break

    return results, has_failure


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        selected_keys = resolve_courts(
            args.courts,
            include_supreme=args.include_supreme,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    total_start = time.time()

    print("=" * 70)
    print(f"  MULTI-COURT CAUSE LIST RUN — {args.date}")
    print(f"  Courts ({len(selected_keys)}): {', '.join(selected_keys)}")
    print(f"  Output: {args.output_dir}")
    print("=" * 70)

    results, has_failure = run_selected(
        selected_keys,
        cause_date=args.date,
        output_dir=args.output_dir,
        delay=args.delay,
        fail_fast=args.fail_fast,
    )

    total_duration = time.time() - total_start
    summary = {
        "date": args.date,
        "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "total_duration_seconds": round(total_duration, 2),
        "total_duration": _format_time(total_duration),
        "total_courts": len(selected_keys),
        "succeeded": sum(1 for r in results.values() if r.get("status") == "SUCCESS"),
        "failed": sum(1 for r in results.values() if r.get("status") != "SUCCESS"),
        "courts": results,
    }
    summary_file = args.output_dir / f"run_summary_{args.date}.json"
    summary_file.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\n" + "=" * 70)
    print(f"  FINAL SUMMARY — {args.date}")
    print("=" * 70)
    print(f" {'Court':<28} | {'Status':<9} | {'Count':<22} | {'Time':<8}")
    print("-" * 70)
    for key in selected_keys:
        name = AVAILABLE_RUNNERS[key][0]
        res = results.get(key, {})
        status = res.get("status", "SKIPPED")
        print(
            f" {name:<28} | {status:<9} | {_count_label(res):<22} | "
            f"{_format_time(res.get('duration', 0.0)):<8}"
        )
    print("=" * 70)
    print(
        f" Total: {_format_time(total_duration)}  |  "
        f"OK {summary['succeeded']}/{len(selected_keys)}  |  "
        f"Failed {summary['failed']}"
    )
    print(f" Summary: {summary_file}")
    print("=" * 70 + "\n")

    return 1 if has_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
