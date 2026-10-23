"""Worker handlers for POST /tasks/scrape and POST /tasks/parse."""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from all_causelist.all_schemas.schema import (
    COURT_SOURCE,
    compute_checksum,
    compute_parsed_unit_checksum,
    group_flat_cases_to_documents,
    make_external_id,
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
from all_causelist.main_output import build_main_output, collect_cases, write_main_output

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from db.mongo import (  # noqa: E402
    find_cause_list_unit,
    list_items_for_document,
    match_and_notify_created_updated,
    match_and_notify_items,
    upsert_main_output,
)

log = logging.getLogger("causelist.worker")

PDF_COURTS = frozenset({"bmhc", "orhc", "sci"})
HTML_COURTS = frozenset({"dlhc", "mphc", "tshc"})

# Court portals raise these when the date has no list yet — not a worker failure.
_NOT_PUBLISHED_MARKERS = (
    "no sci glance pdfs published",
    "no cause list published",
    "no bombay cause list",
    "empty coram table",
    "coram html table was missing",
    "could not parse coram rows",
    "could not parse published lists",
    "bench dropdown was empty",
    "judge dropdown was empty",
)


def _is_not_published_error(exc: BaseException) -> bool:
    """True when the court site has nothing for that date (avoid Cloud Tasks 500 retries)."""
    text = str(exc).casefold()
    return any(marker in text for marker in _NOT_PUBLISHED_MARKERS)


def _not_published_response(
    *,
    court_key: str,
    date: str,
    reason: str,
) -> dict[str, Any]:
    mark_not_published(court=court_key, date=date)
    log.warning(
        "scrape not_published court=%s date=%s reason=%s",
        court_key,
        date,
        reason,
    )
    return {
        "status": "not_published",
        "court": court_key,
        "date": date,
        "pdf_count": 0,
        "reason": reason,
    }


def _should_cleanup_local_output() -> bool:
    """Cloud Run / real cloud: drop local scrape artifacts after GCS (or HTML upsert)."""
    return not config.cloud_dry_run()


def _safe_unlink(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
            log.info("local output DELETED file=%s", path)
    except OSError as exc:
        log.warning("local output delete failed file=%s error=%s", path, exc)


def _safe_rmtree(path: Path | str | None) -> None:
    if not path:
        return
    target = Path(path)
    try:
        if target.is_dir():
            shutil.rmtree(target)
            log.info("local output DELETED dir=%s", target)
        elif target.is_file():
            _safe_unlink(target)
    except OSError as exc:
        log.warning("local output rmtree failed path=%s error=%s", target, exc)


def handle_scrape(*, court: str, date: str) -> dict[str, Any]:
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
    mark_downloading(court=court_key, date=date) #scrape-job db status update
    output_base = _ROOT / "output"

    try: # it check key  which have prasing via pdf
        if court_key in PDF_COURTS:
            result = _scrape_pdf_court(
                court_key=court_key, date=date,  output_base=output_base
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
                court_key=court_key, date=date,  output_base=output_base
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
        if _is_not_published_error(exc):
            return _not_published_response(
                court_key=court_key,
                date=date,
                reason=str(exc),
            )
        log.exception("scrape FAILED court=%s date=%s: %s", court_key, date, exc)
        mark_failed(court=court_key, date=date, error=str(exc))
        raise


#make func parse pdf. for routing also
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

        # Must parse first: CauseList.checksum = hash of all parsed fields (not PDF bytes).
        list_type_enum = normalize_list_type_enum(list_type)
        bench_label = (label or "").strip()
        source = COURT_SOURCE[court_key]

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
            row_bench = (
                label or block.get("bench") or block.get("coram") or ""
            ).strip()
            row.setdefault(
                "_group",
                {
                    "benchLabel": row_bench,
                    "list_type_display": block.get("list_type") or list_type,
                    "edition": "",
                    "benchType": "",
                    "externalId": make_external_id(
                        court_key,
                        hearing_date,
                        list_type_enum,
                        row_bench,
                        "",
                        "",
                    ),
                    "source_url": source_url,
                    "rawRef": gcs_uri or None,
                    "contentType": "application/pdf" if gcs_uri else None,
                },
            )

        documents = group_flat_cases_to_documents(
            court_key=court_key,
            hearing_date=hearing_date,
            cases=cases,
        )

        for doc in documents:
            if gcs_uri:
                doc["rawRef"] = gcs_uri
                doc["contentType"] = "application/pdf"
            doc.setdefault("edition", "")
            doc.setdefault("benchType", "")
            doc["externalId"] = make_external_id(
                court_key,
                hearing_date,
                doc.get("listType") or list_type_enum,
                doc.get("benchLabel") or bench_label,
                doc.get("edition") or "",
                doc.get("benchType") or "",
            )
            # After parse: checksum from all item fields of this unit.
            doc["checksum"] = compute_parsed_unit_checksum(
                court_key=court_key,
                hearing_date=hearing_date,
                list_type=doc.get("listType") or list_type_enum,
                bench_label=doc.get("benchLabel") or bench_label,
                items=list(doc.get("items") or []),
                external_id=str(doc.get("externalId") or ""),
                edition=str(doc.get("edition") or ""),
                bench_type=str(doc.get("benchType") or ""),
            )

        # Same parsed fields → skip rewrite; still rematch + notify.
        if len(documents) == 1:
            doc0 = documents[0]
            parsed_sha = str(doc0.get("checksum") or "")
            external_id = str(doc0.get("externalId") or "")
            existing = find_cause_list_unit(
                source=source,
                court_key=court_key,
                hearing_date=hearing_date,
                list_type=doc0.get("listType") or list_type_enum,
                bench_label=doc0.get("benchLabel") or bench_label,
                external_id=external_id,
                edition=str(doc0.get("edition") or ""),
                bench_type=str(doc0.get("benchType") or ""),
            )
            stored_sha = (existing or {}).get("checksum")
            log.info(
                "checksum compare (parsed fields) court=%s externalId=%s "
                "incoming=%s... stored=%s... match=%s",
                court_key,
                external_id,
                parsed_sha[:16] if parsed_sha else "-",
                (str(stored_sha)[:16] + "...") if stored_sha else "-",
                bool(existing is not None and stored_sha == parsed_sha),
            )
            if existing is not None and stored_sha == parsed_sha:
                items = list_items_for_document(existing["_id"])
                notify = match_and_notify_items(items, court_key=court_key)
                bump = bump_parsed(court=court_key, date=hearing_date, failed=False)
                return {
                    "status": "ok",
                    "court": court_key,
                    "date": hearing_date,
                    "job_id": jid,
                    "gcs_uri": gcs_uri,
                    "checksum_unchanged": True,
                    "item_count": len(items),
                    "document_count": 1,
                    "mongodb": {
                        "status": "checksum_unchanged",
                        "document_id": str(existing["_id"]),
                    },
                    "notify": notify,
                    "job": bump,
                }

        main = build_main_output(
            court=court_key,
            cause_date=hearing_date,
            cases=[],
            documents=documents,
        )

        fp_sample: list[str] = []
        for doc in documents:
            for it in doc.get("items") or []:
                if isinstance(it, dict) and it.get("fingerprint"):
                    fp_sample.append(str(it["fingerprint"])[:16])
                if len(fp_sample) >= 3:
                    break
            if len(fp_sample) >= 3:
                break
        log.info(
            "fingerprint ready court=%s docs=%s items=%s sample_fp=%s "
            "parsed_checksum=%s...",
            court_key,
            len(documents),
            main.get("item_count"),
            fp_sample or "-",
            (documents[0].get("checksum") or "-")[:16] if documents else "-",
        )

        mongo = upsert_main_output(main)
        log.info("mongo upsert after checksum change result=%s", mongo)

        created_items = list(mongo.get("created_items") or [])
        updated_items = list(mongo.get("updated_items") or [])
        log.info(
            "notify start (checksum changed) court=%s created_items=%s "
            "updated_items=%s",
            court_key,
            len(created_items),
            len(updated_items),
        )
        notify = match_and_notify_created_updated(
            created_items,
            updated_items,
            court_key=court_key,
        )

        bump = bump_parsed(court=court_key, date=hearing_date, failed=False)
        log.info(
            "parse DONE court=%s items=%s docs=%s gcs=%s checksum=%s... notify=%s",
            court_key,
            main.get("item_count"),
            main.get("document_count"),
            gcs_uri or "-",
            (documents[0].get("checksum") or "-")[:16] if documents else "-",
            notify,
        )
        return {
            "status": "ok",
            "court": court_key,
            "date": hearing_date,
            "job_id": jid,
            "gcs_uri": gcs_uri,
            "checksum_unchanged": False,
            "item_count": main.get("item_count"),
            "document_count": main.get("document_count"),
            "mongodb": mongo,
            "notify": notify,
            "job": bump,
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("parse FAILED court=%s date=%s: %s", court_key, hearing_date, exc)
        bump_parsed(court=court_key, date=hearing_date, failed=True)
        raise RuntimeError(f"parse failed: {exc}") from exc
    finally:
        if tmp is not None:
            tmp.cleanup()


#it is scraping pdf and saving it
def _scrape_pdf_court(
    *,
    court_key: str,
    date: str,
    output_base: Path,
) -> dict[str, Any]:
    script = script_key_from_court_key(court_key) #return key value based which script to run like dlhc for delhi
    out_dir = output_base / script
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("pdf scrape download start script=%s out_dir=%s", script, out_dir)

    try:
        if script == "bombay":
            from all_causelist.bombay import download_bombay_pdfs, iter_downloaded_pdfs

            payload = download_bombay_pdfs(date, out_dir)
        elif script == "odisha":
            from all_causelist.odisha import download_odisha_pdfs, iter_downloaded_pdfs

            payload = download_odisha_pdfs(date, out_dir)
        elif script == "supreme":
            from all_causelist.supreme import download_sci_pdfs, iter_downloaded_pdfs

            payload = download_sci_pdfs(date, out_dir)
        else:
            raise ValueError(f"not a PDF court: {court_key}")
    except Exception as exc:  # noqa: BLE001
        if _is_not_published_error(exc):
            return _not_published_response(
                court_key=court_key,
                date=date,
                reason=str(exc),
            )
        raise

    pdfs = iter_downloaded_pdfs(payload)

    log.info("pdf scrape download done court=%s pdf_count=%s", court_key, len(pdfs))

    if not pdfs:
        return _not_published_response(
            court_key=court_key,
            date=date,
            reason=f"No PDFs downloaded for {court_key} on {date}",
        )

    jid = job_id(court_key, date)
    mark_parsing(court=court_key, date=date, pdf_total=len(pdfs)) #updating status in scrpe-jbs 
    enqueued: list[dict[str, Any]] = []
    uploads: list[dict[str, Any]] = []

    for i, item in enumerate(pdfs, start=1):
        list_enum = normalize_list_type_enum(item.get("list_type"))
        label = str(item.get("label") or "").strip()
        # BMHC: object name = coram label (+ weekly tag when needed)
        source_list = str(item.get("list_type") or "").strip()
        gcs_filename = str(item.get("filename") or Path(item["pdf_path"]).name)
        bench_for_unit = label
        if court_key == "bmhc":
            from all_causelist.cloud.gcs import bmhc_coram_pdf_filename

            gcs_filename = bmhc_coram_pdf_filename(
                label or str(item.get("coram_slug") or "") or gcs_filename,
                source_list=source_list or None,
            )
            # Weekly Main - 1 vs - 5 — tag bench when source is a weekly link.
            if (
                list_enum == "weekly"
                or source_list.casefold().startswith("weekly")
            ) and source_list:
                bench_for_unit = f"{label} [{source_list}]".strip() if label else source_list
        local_pdf = Path(item["pdf_path"])
        log.info(
            "pdf upload %s/%s file=%s gcs_name=%s list_type=%s label=%s",
            i,
            len(pdfs),
            local_pdf.name,
            gcs_filename,
            list_enum,
            (bench_for_unit or label)[:80],
        )
        if not local_pdf.is_file():
            log.warning(
                "pdf upload SKIP missing local file court=%s path=%s "
                "(duplicate coram row or earlier cleanup)",
                court_key,
                local_pdf,
            )
            uploads.append(
                {
                    "skipped_upload": True,
                    "reason": "missing_local_file",
                    "local_path": str(local_pdf),
                    "gcs_uri": None,
                }
            )
            continue
        up = _upload_pdf_unless_checksum_unchanged(
            local_path=local_pdf,
            court_key=court_key,
            hearing_date=date,
            list_type=list_enum,
            bench_label=bench_for_unit,
            source_url=item.get("source_url"),
            filename=gcs_filename,
        )
        log.info(
            "pdf upload result gcs_uri=%s skipped=%s dry_run=%s",
            up.get("gcs_uri"),
            up.get("skipped_upload"),
            up.get("dry_run"),
        )
        uploads.append(up)
        # Cloud Tasks → Cloud Run: only send GCS URI (no local_path).
        # Dry-run / LOCAL_INLINE_PARSE may still attach local_path for laptop tests.
        extra: dict[str, Any] = {
            "msb": item.get("msb"),
            "filename": gcs_filename,
            "coram_index": item.get("coram_index"),
        }
        if config.cloud_dry_run() or config.local_inline_parse():
            extra["local_path"] = item["pdf_path"]

        # Prefer site label (e.g. Weekly Main - 5) so enum+bench stay unique for weekly.
        parse_list_type = source_list or list_enum
        if config.local_inline_parse():
            log.info("pdf inline parse %s/%s", i, len(pdfs))
            parsed = handle_parse(
                {
                    "job_id": jid,
                    "court": court_key,
                    "date": date,
                    "list_type": parse_list_type,
                    "source_url": item.get("source_url") or "",
                    "gcs_uri": up["gcs_uri"],
                    "label": bench_for_unit,
                    **extra,
                }
            )
            enqueued.append({"status": "inline_parsed", "result": parsed})
            # Inline parse done — local PDF no longer needed on cloud.
            if _should_cleanup_local_output():
                _safe_unlink(Path(item["pdf_path"]))
        else:
            from all_causelist.cloud.tasks import enqueue_parse_job

            log.info("pdf enqueue parse task %s/%s gcs=%s", i, len(pdfs), up.get("gcs_uri"))
            task = enqueue_parse_job(
                job_id=jid,
                court=court_key,
                date=date,
                list_type=parse_list_type,
                source_url=item.get("source_url") or "",
                gcs_uri=up["gcs_uri"],
                label=bench_for_unit,
                extra=extra,
            )
            log.info("pdf parse task result=%s", task.get("status") or task)
            enqueued.append(task)
            # Cloud Tasks parse uses gcs_uri only — drop local scrape copy.
            if _should_cleanup_local_output() and up.get("gcs_uri"):
                _safe_unlink(Path(item["pdf_path"]))

    if _should_cleanup_local_output():
        _safe_rmtree(payload.get("output_dir"))

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
        "local_output_cleaned": _should_cleanup_local_output(),
    }


def _scrape_html_court(
    *,
    court_key: str,
    date: str,
    output_base: Path,
) -> dict[str, Any]:
    # MPHC: stream one combo → checksum/upsert/notify → drop (no 36-in-memory).
    if court_key == "mphc":
        return _scrape_mphc_streaming(date=date)

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
    result = runner(date, output_base)
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
    notify: dict[str, Any] = {
        "item_count": 0,
        "matched_items": 0,
        "notify_ok": 0,
        "notify_fail": 0,
    }

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
        item_count = int(doc.get("item_count") or doc.get("case_count") or 0)
        documents = doc.get("documents") if isinstance(doc.get("documents"), list) else []
        if item_count == 0 and not documents:
            cleaned = False
            if _should_cleanup_local_output() and main_path.is_file():
                day_dir = main_path.parent
                if day_dir.name.startswith("causelist-"):
                    _safe_rmtree(day_dir)
                else:
                    _safe_unlink(main_path)
                cleaned = True
            out = _not_published_response(
                court_key=court_key,
                date=date,
                reason=f"No cause list items for {court_key} on {date}",
            )
            out["path"] = "html_single_stage"
            out["item_count"] = 0
            out["local_output_cleaned"] = cleaned
            return out
        mongo, notify = _upsert_html_documents_with_checksum_gate(
            court_key=court_key,
            main_output=doc,
        )
    else:
        return _not_published_response(
            court_key=court_key,
            date=date,
            reason=f"No mainOutput.json for {court_key} on {date}",
        )

    # HTML: no PDFs — mark complete via parsing with total 0 / or complete status
    mark_parsing(court=court_key, date=date, pdf_total=0)
    from all_causelist.cloud.jobs import set_job_status, STATUS_COMPLETE

    set_job_status(court=court_key, date=date, status=STATUS_COMPLETE)

    cleaned = False
    if _should_cleanup_local_output() and main_path.is_file():
        day_dir = main_path.parent
        # Prefer deleting causelist-<date>/; else just the JSON file.
        if day_dir.name.startswith("causelist-"):
            _safe_rmtree(day_dir)
        else:
            _safe_unlink(main_path)
        cleaned = True

    return {
        "status": "ok",
        "court": court_key,
        "date": date,
        "path": "html_single_stage",
        "item_count": item_count,
        "main_output": str(main_path) if main_path.is_file() else None,
        "mongodb": mongo,
        "notify": notify,
        "local_output_cleaned": cleaned,
        "runner": {k: v for k, v in result.items() if k != "payload"},
    }


def _upload_pdf_unless_checksum_unchanged(
    *,
    local_path: Path,
    court_key: str,
    hearing_date: str,
    list_type: str,
    bench_label: str,
    source_url: str | None,
    filename: str,
) -> dict[str, Any]:
    """Upload PDF to GCS for view/rawRef.

    CauseList.checksum is **parsed fields** (set in parse job), so upload does not
    gate on that checksum — always write/overwrite the object path for this unit.
    """
    list_enum = normalize_list_type_enum(list_type)
    bench = (bench_label or "").strip()
    edition = ""
    bench_type = ""
    external_id = make_external_id(
        court_key, hearing_date, list_enum, bench, edition, bench_type
    )
    pdf_bytes = local_path.read_bytes()
    file_sha = compute_checksum(pdf_bytes)
    log.info(
        "pdf upload WRITE court=%s externalId=%s file_sha=%s... (CauseList.checksum is parsed-fields, gated in parse)",
        court_key,
        external_id,
        file_sha[:16],
    )
    up = upload_pdf(
        local_path=local_path,
        court_key=court_key,
        hearing_date=hearing_date,
        list_type=list_enum,
        source_url=source_url,
        filename=filename,
        edition=edition,
        bench_type=bench_type,
    )
    up["skipped_upload"] = False
    up["checksum"] = file_sha  # file hash for logs/GCS meta only; not CauseList.checksum
    up["external_id"] = external_id
    return up


def _scrape_mphc_streaming(*, date: str) -> dict[str, Any]:
    """MPHC: per combo fetch → parse → checksum gate → upsert/notify; then next."""
    from all_causelist.mphc import iter_mphc_combo_units

    combo_total = 0
    combos_ok = 0
    combos_error = 0
    combos_empty = 0
    case_count = 0
    docs_written = 0
    docs_unchanged = 0
    notify_ok = 0
    notify_fail = 0
    notify_skip = 0

    def _progress(msg: str) -> None:
        log.info("mphc %s", msg)

    for unit in iter_mphc_combo_units(date, on_progress=_progress):
        combo_total = int(unit.get("combo_total") or combo_total)
        idx = unit.get("combo_index")
        meta = unit.get("combo_meta") or {}
        docs = list(unit.get("documents") or [])
        case_count += int(unit.get("case_count") or 0)

        if meta.get("error"):
            combos_error += 1
            log.warning(
                "mphc combo %s/%s error=%s",
                idx,
                combo_total,
                meta.get("error"),
            )
            continue
        if not docs:
            combos_empty += 1
            continue

        combos_ok += 1
        mini_main = {
            "courtKey": "mphc",
            "hearingDate": date,
            "documents": docs,
            "item_count": sum(len(d.get("items") or []) for d in docs),
            "document_count": len(docs),
        }
        mongo, notify = _upsert_html_documents_with_checksum_gate(
            court_key="mphc",
            main_output=mini_main,
        )
        docs_written += int(mongo.get("documents_written") or 0)
        docs_unchanged += int(mongo.get("documents_unchanged") or 0)
        notify_ok += int(notify.get("notify_ok") or 0)
        notify_fail += int(notify.get("notify_fail") or 0)
        notify_skip += int(notify.get("notify_skip") or 0)
        log.info(
            "mphc combo %s/%s upsert done label=%s docs=%s written=%s unchanged=%s notify=%s",
            idx,
            combo_total,
            meta.get("label"),
            len(docs),
            mongo.get("documents_written"),
            mongo.get("documents_unchanged"),
            notify.get("status"),
        )
        # Drop references before next combo.
        del docs
        del mini_main

    from all_causelist.cloud.jobs import STATUS_COMPLETE, set_job_status

    set_job_status(court="mphc", date=date, status=STATUS_COMPLETE)
    log.info(
        "mphc stream scrape done date=%s combos=%s ok=%s empty=%s err=%s cases=%s "
        "docs_written=%s docs_unchanged=%s",
        date,
        combo_total,
        combos_ok,
        combos_empty,
        combos_error,
        case_count,
        docs_written,
        docs_unchanged,
    )

    if combo_total > 0 and combos_ok == 0 and case_count == 0 and combos_error == combo_total:
        out = _not_published_response(
            court_key="mphc",
            date=date,
            reason=f"All MPHC combos failed for {date}",
        )
        out["path"] = "html_combo_stream"
        return out

    if case_count == 0 and docs_written == 0 and combos_ok == 0:
        out = _not_published_response(
            court_key="mphc",
            date=date,
            reason=f"No cause list items for mphc on {date}",
        )
        out["path"] = "html_combo_stream"
        return out

    return {
        "status": "ok",
        "court": "mphc",
        "date": date,
        "path": "html_combo_stream",
        "item_count": case_count,
        "combo_total": combo_total,
        "combos_ok": combos_ok,
        "combos_empty": combos_empty,
        "combos_error": combos_error,
        "mongodb": {
            "documents_written": docs_written,
            "documents_unchanged": docs_unchanged,
        },
        "notify": {
            "notify_ok": notify_ok,
            "notify_fail": notify_fail,
            "notify_skip": notify_skip,
        },
        "local_output_cleaned": True,
    }


def _upsert_html_documents_with_checksum_gate(
    *,
    court_key: str,
    main_output: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Per CauseList unit: same checksum → skip rewrite; else upsert. Then rematch+notify."""
    documents = main_output.get("documents")
    if not isinstance(documents, list) or not documents:
        return {"status": "error: no documents"}, {
            "item_count": 0,
            "matched_items": 0,
            "notify_ok": 0,
            "notify_fail": 0,
        }

    hearing_fallback = str(main_output.get("hearingDate") or "")
    to_upsert: list[dict[str, Any]] = []
    rematch_as_updated: list[dict[str, Any]] = []
    unchanged = 0

    for doc in documents:
        if not isinstance(doc, dict):
            continue
        ck = str(doc.get("courtKey") or court_key)
        hearing = doc.get("hearingDate") or hearing_fallback
        list_type = str(doc.get("listType") or "main")
        bench_label = str(doc.get("benchLabel") or "")
        edition = str(doc.get("edition") or "")
        bench_type = str(doc.get("benchType") or "")
        doc["edition"] = edition
        doc["benchType"] = bench_type
        # Normalize to contract externalId before lookup / write.
        doc["externalId"] = make_external_id(
            ck, hearing, list_type, bench_label, edition, bench_type
        )
        existing = find_cause_list_unit(
            source=str(doc.get("source") or COURT_SOURCE[court_key]),
            court_key=ck,
            hearing_date=hearing,
            list_type=list_type,
            bench_label=bench_label,
            external_id=doc["externalId"],
            edition=edition,
            bench_type=bench_type,
        )
        sha = doc.get("checksum")
        stored_sha = (existing or {}).get("checksum")
        if existing is not None and sha and stored_sha == sha:
            unchanged += 1
            log.info(
                "html checksum UNCHANGED externalId=%s checksum=%s... → rematch only",
                doc["externalId"],
                str(sha)[:16],
            )
            rematch_as_updated.extend(list_items_for_document(existing["_id"]))
            continue
        log.info(
            "html checksum CHANGED/NEW externalId=%s incoming=%s... stored=%s... → upsert",
            doc["externalId"],
            (str(sha)[:16] + "...") if sha else "-",
            (str(stored_sha)[:16] + "...") if stored_sha else "-",
        )
        to_upsert.append(doc)

    created_items: list[dict[str, Any]] = []
    updated_items: list[dict[str, Any]] = list(rematch_as_updated)
    if to_upsert:
        mongo = upsert_main_output({**main_output, "documents": to_upsert})
        log.info("html mongo upsert result=%s", mongo)
        created_items = list(mongo.get("created_items") or [])
        updated_items.extend(list(mongo.get("updated_items") or []))
        mongo = {
            **mongo,
            "documents_unchanged": unchanged,
            "documents_written": len(to_upsert),
        }
    else:
        mongo = {
            "status": "checksum_unchanged",
            "documents_unchanged": unchanged,
            "documents_written": 0,
            "created_items": [],
            "updated_items": [],
        }

    log.info(
        "notify start (html) court=%s created_items=%s updated_items=%s "
        "unchanged_docs=%s written_docs=%s",
        court_key,
        len(created_items),
        len(updated_items),
        unchanged,
        len(to_upsert),
    )
    notify = match_and_notify_created_updated(
        created_items,
        updated_items,
        court_key=court_key,
    )
    log.info(
        "html checksum gate court=%s unchanged=%s written=%s notify=%s",
        court_key,
        unchanged,
        len(to_upsert),
        notify,
    )
    return mongo, notify


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
