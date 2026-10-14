"""Worker handlers for POST /tasks/scrape and POST /tasks/parse."""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

from all_causelist.all_schemas.schema import (
    compute_checksum,
    group_flat_cases_to_documents,
    normalize_list_type_enum,
    resolve_court_key,
    script_key_from_court_key,
)
from all_causelist.cloud import config
from all_causelist.cloud.gcs import upload_pdf
from all_causelist.cloud.jobs import (
    bump_parsed,
    job_id,
    mark_downloading,
    mark_failed,
    mark_not_published,
    mark_parsing,
)
from all_causelist.cloud.tasks import enqueue_parse_job
from all_causelist.main_output import build_main_output, collect_cases, write_main_output

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from db.mongo import upsert_main_output  # noqa: E402

log = logging.getLogger("causelist.worker")

PDF_COURTS = frozenset({"bmhc", "orhc", "sci"})
HTML_COURTS = frozenset({"dlhc", "mphc", "tshc"})


def handle_scrape(*, court: str, date: str, delay: float = 1.0) -> dict[str, Any]:
    """Worker 1: PDF courts download+GCS+enqueue parse; HTML courts scrape+upsert."""
    court_key = resolve_court_key(court)
    path = (
        "pdf_two_stage"
        if court_key in PDF_COURTS
        else "html_single_stage"
        if court_key in HTML_COURTS
        else "unknown"
    )
    log.info(
        "scrape START court=%s date=%s path=%s dry_run=%s inline_parse=%s",
        court_key,
        date,
        path,
        config.cloud_dry_run(),
        config.local_inline_parse(),
    )
    mark_downloading(court=court_key, date=date)
    output_base = _ROOT / "output"

    try:
        if court_key in PDF_COURTS:
            result = _scrape_pdf_court(
                court_key=court_key, date=date, delay=delay, output_base=output_base
            )
            log.info(
                "scrape DONE pdf court=%s date=%s pdf_count=%s status=%s",
                court_key,
                date,
                result.get("pdf_count"),
                result.get("status"),
            )
            return result
        if court_key in HTML_COURTS:
            result = _scrape_html_court(
                court_key=court_key, date=date, delay=delay, output_base=output_base
            )
            log.info(
                "scrape DONE html court=%s date=%s items=%s status=%s (no PDF path)",
                court_key,
                date,
                result.get("item_count"),
                result.get("status"),
            )
            return result
        raise ValueError(f"unsupported court: {court}")
    except Exception as exc:  # noqa: BLE001
        log.exception("scrape FAILED court=%s date=%s: %s", court_key, date, exc)
        mark_failed(court=court_key, date=date, error=str(exc))
        raise


def handle_parse(body: dict[str, Any]) -> dict[str, Any]:
    """Worker 2: download PDF (or use local_path), parse, upsert CauseList + items."""
    court_key = resolve_court_key(str(body.get("court") or ""))
    hearing_date = str(body.get("date") or "")
    list_type = str(body.get("list_type") or "main")
    source_url = str(body.get("source_url") or "")
    gcs_uri = str(body.get("gcs_uri") or "")
    label = str(body.get("label") or "")
    local_path = body.get("local_path")
    jid = str(body.get("job_id") or job_id(court_key, hearing_date))

    log.info(
        "parse START court=%s date=%s list_type=%s gcs_uri=%s local_path=%s dry_run=%s",
        court_key,
        hearing_date,
        list_type,
        gcs_uri or "-",
        local_path or "-",
        config.cloud_dry_run(),
    )

    if not hearing_date:
        raise ValueError("date is required")

    pdf_bytes: bytes
    pdf_file: Path
    tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if local_path and Path(str(local_path)).is_file():
            pdf_file = Path(str(local_path))
            pdf_bytes = pdf_file.read_bytes()
            log.info("parse source=local file=%s bytes=%s", pdf_file, len(pdf_bytes))
        elif gcs_uri and not config.cloud_dry_run():
            from all_causelist.cloud.gcs import download_pdf_bytes

            log.info("parse downloading from GCS %s", gcs_uri)
            pdf_bytes = download_pdf_bytes(gcs_uri)
            tmp = tempfile.TemporaryDirectory(prefix="causelist-parse-")
            pdf_file = Path(tmp.name) / "list.pdf"
            pdf_file.write_bytes(pdf_bytes)
            log.info("parse source=gcs bytes=%s", len(pdf_bytes))
        elif gcs_uri and config.cloud_dry_run() and local_path:
            pdf_file = Path(str(local_path))
            pdf_bytes = pdf_file.read_bytes()
            log.info("parse source=dry_run_local file=%s bytes=%s", pdf_file, len(pdf_bytes))
        else:
            raise ValueError("local_path or gcs_uri required to parse PDF")

        log.info("parse extracting cases from PDF court=%s", court_key)
        block = _cases_from_pdf_for_court(
            court_key,
            pdf_file,
            cause_date=hearing_date,
            list_type=list_type,
            source_url=source_url,
            label=label,
            extra=body,
        )
        cases = list(block.get("cases") or [])
        for row in cases:
            row.setdefault(
                "_group",
                {
                    "benchLabel": label or block.get("bench") or block.get("coram") or "",
                    "list_type_display": block.get("list_type") or list_type,
                    "externalId": source_url or gcs_uri,
                    "source_url": source_url,
                    "rawRef": gcs_uri or None,
                    "contentType": "application/pdf" if gcs_uri else None,
                    "checksum_raw": pdf_bytes,
                },
            )

        documents = group_flat_cases_to_documents(
            court_key=court_key,
            hearing_date=hearing_date,
            cases=cases,
        )
        # Ensure rawRef + byte checksum on every parent from this PDF.
        sha = compute_checksum(pdf_bytes)
        for doc in documents:
            if gcs_uri:
                doc["rawRef"] = gcs_uri
                doc["contentType"] = "application/pdf"
            doc["checksum"] = sha
            if source_url and not doc.get("externalId"):
                doc["externalId"] = source_url

        main = build_main_output(
            court=court_key,
            cause_date=hearing_date,
            cases=[],
            documents=documents,
        )
        mongo = upsert_main_output(main)
        bump = bump_parsed(court=court_key, date=hearing_date, failed=False)
        log.info(
            "parse DONE court=%s items=%s docs=%s gcs=%s",
            court_key,
            main.get("item_count"),
            main.get("document_count"),
            gcs_uri or "-",
        )
        return {
            "status": "ok",
            "court": court_key,
            "date": hearing_date,
            "job_id": jid,
            "gcs_uri": gcs_uri,
            "item_count": main.get("item_count"),
            "document_count": main.get("document_count"),
            "mongodb": mongo,
            "job": bump,
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("parse FAILED court=%s date=%s: %s", court_key, hearing_date, exc)
        bump_parsed(court=court_key, date=hearing_date, failed=True)
        raise RuntimeError(f"parse failed: {exc}") from exc
    finally:
        if tmp is not None:
            tmp.cleanup()


def _scrape_pdf_court(
    *,
    court_key: str,
    date: str,
    delay: float,
    output_base: Path,
) -> dict[str, Any]:
    script = script_key_from_court_key(court_key)
    out_dir = output_base / script
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("pdf scrape download start script=%s out_dir=%s", script, out_dir)

    if script == "bombay":
        from all_causelist.bombay import download_bombay_pdfs, iter_downloaded_pdfs

        payload = download_bombay_pdfs(date, out_dir, delay_seconds=delay)
    elif script == "odisha":
        from all_causelist.odisha import download_odisha_pdfs, iter_downloaded_pdfs

        payload = download_odisha_pdfs(date, out_dir, delay_seconds=delay)
    elif script == "supreme":
        from all_causelist.supreme import download_sci_pdfs, iter_downloaded_pdfs

        payload = download_sci_pdfs(date, out_dir, delay_seconds=delay)
    else:
        raise ValueError(f"not a PDF court: {court_key}")
    pdfs = iter_downloaded_pdfs(payload)

    log.info("pdf scrape download done court=%s pdf_count=%s", court_key, len(pdfs))

    if not pdfs:
        log.warning("pdf scrape no PDFs published court=%s date=%s", court_key, date)
        mark_not_published(court=court_key, date=date)
        return {
            "status": "not_published",
            "court": court_key,
            "date": date,
            "pdf_count": 0,
            "payload": payload,
        }

    jid = job_id(court_key, date)
    mark_parsing(court=court_key, date=date, pdf_total=len(pdfs))
    enqueued: list[dict[str, Any]] = []
    uploads: list[dict[str, Any]] = []

    for i, item in enumerate(pdfs, start=1):
        list_enum = normalize_list_type_enum(item.get("list_type"))
        log.info(
            "pdf upload %s/%s file=%s list_type=%s",
            i,
            len(pdfs),
            Path(item["pdf_path"]).name,
            list_enum,
        )
        up = upload_pdf(
            local_path=item["pdf_path"],
            court_key=court_key,
            hearing_date=date,
            list_type=list_enum,
            source_url=item.get("source_url"),
            filename=Path(item["pdf_path"]).name,
        )
        log.info("pdf uploaded gcs_uri=%s dry_run=%s", up.get("gcs_uri"), up.get("dry_run"))
        uploads.append(up)
        # Cloud Tasks → Cloud Run: only send GCS URI (no local_path).
        # Dry-run / LOCAL_INLINE_PARSE may still attach local_path for laptop tests.
        extra: dict[str, Any] = {
            "msb": item.get("msb"),
            "filename": item.get("filename"),
        }
        if config.cloud_dry_run() or config.local_inline_parse():
            extra["local_path"] = item["pdf_path"]

        if config.local_inline_parse():
            log.info("pdf inline parse %s/%s", i, len(pdfs))
            parsed = handle_parse(
                {
                    "job_id": jid,
                    "court": court_key,
                    "date": date,
                    "list_type": list_enum,
                    "source_url": item.get("source_url") or "",
                    "gcs_uri": up["gcs_uri"],
                    "label": item.get("label") or "",
                    **extra,
                }
            )
            enqueued.append({"status": "inline_parsed", "result": parsed})
        else:
            log.info("pdf enqueue parse task %s/%s gcs=%s", i, len(pdfs), up.get("gcs_uri"))
            task = enqueue_parse_job(
                job_id=jid,
                court=court_key,
                date=date,
                list_type=list_enum,
                source_url=item.get("source_url") or "",
                gcs_uri=up["gcs_uri"],
                label=item.get("label") or "",
                extra=extra,
            )
            log.info("pdf parse task result=%s", task.get("status") or task)
            enqueued.append(task)

    return {
        "status": "ok",
        "court": court_key,
        "date": date,
        "path": "pdf_two_stage",
        "pdf_count": len(pdfs),
        "job_id": jid,
        "uploads": uploads,
        "parse_tasks": enqueued,
        "output_dir": payload.get("output_dir"),
    }


def _scrape_html_court(
    *,
    court_key: str,
    date: str,
    delay: float,
    output_base: Path,
) -> dict[str, Any]:
    from run_all import (
        run_delhi,
        run_mphc,
        run_telangana,
    )

    runners = {
        "dlhc": run_delhi,
        "mphc": run_mphc,
        "tshc": run_telangana,
    }
    runner = runners[court_key]
    log.info("html scrape start court=%s (HTML — no PDF/GCS/parse queue)", court_key)
    result = runner(date, output_base, delay)
    log.info(
        "html scrape runner finished court=%s case_count=%s",
        court_key,
        result.get("case_count") or result.get("item_count"),
    )
    # Prefer court folder mainOutput
    script = script_key_from_court_key(court_key)
    main_path = Path(result.get("main_output") or "")
    if not main_path.is_file():
        candidates = [
            output_base / script / "mainOutput.json",
            output_base / script / f"causelist-{date}" / "mainOutput.json",
        ]
        main_path = next((p for p in candidates if p.is_file()), candidates[0])

    mongo: dict[str, Any]
    if main_path.is_file():
        import json

        doc = json.loads(main_path.read_text(encoding="utf-8"))
        if "documents" not in doc:
            # Rebuild Legixo shape if runner wrote legacy
            cases = collect_cases(script, doc) if doc.get("cases") else []
            if not cases and doc.get("benches"):
                cases = collect_cases(script, doc)
            write_main_output(
                main_path.parent,
                court=script,
                cause_date=date,
                cases=cases or collect_cases(script, doc),
            )
            doc = json.loads(main_path.read_text(encoding="utf-8"))
        mongo = upsert_main_output(doc)
        item_count = int(doc.get("item_count") or doc.get("case_count") or 0)
    else:
        mongo = {"status": "skipped_no_file"}
        item_count = int(result.get("case_count") or 0)

    # HTML: no PDFs — mark complete via parsing with total 0 / or complete status
    mark_parsing(court=court_key, date=date, pdf_total=0)
    from all_causelist.cloud.jobs import set_job_status, STATUS_COMPLETE

    set_job_status(court=court_key, date=date, status=STATUS_COMPLETE)

    return {
        "status": "ok",
        "court": court_key,
        "date": date,
        "path": "html_single_stage",
        "item_count": item_count,
        "main_output": str(main_path) if main_path.is_file() else None,
        "mongodb": mongo,
        "runner": {k: v for k, v in result.items() if k != "payload"},
    }


def _cases_from_pdf_for_court(
    court_key: str,
    pdf_path: Path,
    *,
    cause_date: str,
    list_type: str,
    source_url: str,
    label: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    if court_key == "bmhc":
        from all_causelist.bombay import cases_from_pdf

        return cases_from_pdf(
            pdf_path,
            cause_date=cause_date,
            list_type=list_type,
            source_url=source_url,
            label=label,
        )
    if court_key == "orhc":
        from all_causelist.odisha import cases_from_pdf

        return cases_from_pdf(
            pdf_path,
            cause_date=cause_date,
            list_type=list_type,
            source_url=source_url,
            label=label,
        )
    if court_key == "sci":
        from all_causelist.supreme import cases_from_pdf

        return cases_from_pdf(
            pdf_path,
            cause_date=cause_date,
            list_type=list_type,
            msb=str(extra.get("msb") or label or "main"),
            source_url=source_url,
            filename=extra.get("filename"),
        )
    raise ValueError(f"no cases_from_pdf for {court_key}")
