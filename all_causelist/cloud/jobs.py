"""scrape_jobs bookkeeping in MongoDB.

One document per court+date run::

    {
      "_id": "scrape-bmhc-2026-10-12",
      "court": "bmhc",
      "date": "2026-10-12",
      "status": "PARSING",
      "pdf_total": 14,
      "pdf_parsed": 9,
      "pdf_failed": 0,
      "updatedAt": "..."
    }

Statuses: QUEUED → DOWNLOADING → PARSING → COMPLETE
         DOWNLOADING → NOT_PUBLISHED (no list for date)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import ReturnDocument

# Project root (causelists/) for db.mongo
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from db.mongo import get_database  # noqa: E402

_COLLECTION = "scrape_jobs"

STATUS_QUEUED = "QUEUED"
STATUS_DOWNLOADING = "DOWNLOADING"
STATUS_PARSING = "PARSING"
STATUS_COMPLETE = "COMPLETE"
STATUS_NOT_PUBLISHED = "NOT_PUBLISHED"
STATUS_FAILED = "FAILED"

_INDEXES_READY = False


def job_id(court_key: str, hearing_date: str) -> str:
    from all_causelist.all_schemas.schema import resolve_court_key

    return f"scrape-{resolve_court_key(court_key)}-{hearing_date}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coll():
    global _INDEXES_READY
    db = get_database()
    if db is None:
        return None
    coll = db[_COLLECTION]
    if not _INDEXES_READY:
        coll.create_index([("court", 1), ("date", 1)])
        coll.create_index([("status", 1)])
        _INDEXES_READY = True
    return coll


def _result(status: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status}
    out.update(extra)
    return out


def ensure_job(
    *,
    court: str,
    date: str,
    status: str = STATUS_QUEUED,
) -> dict[str, Any]:
    """Create or return the scrape_jobs doc for court+date."""
    from all_causelist.all_schemas.schema import resolve_court_key

    coll = _coll()
    if coll is None:
        return _result("skipped_no_uri", job_id=job_id(court, date))

    ck = resolve_court_key(court)
    jid = job_id(ck, date)
    now = _now()
    doc = coll.find_one_and_update(
        {"_id": jid},
        {
            "$setOnInsert": {
                "_id": jid,
                "court": ck,
                "date": date,
                "status": status,
                "pdf_total": 0,
                "pdf_parsed": 0,
                "pdf_failed": 0,
                "createdAt": now,
            },
            "$set": {"updatedAt": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return _result("ok", job=doc)


def set_job_status(
    *,
    court: str,
    date: str,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    coll = _coll()
    if coll is None:
        return _result("skipped_no_uri", job_id=job_id(court, date))

    jid = job_id(court, date)
    fields: dict[str, Any] = {"status": status, "updatedAt": _now()}
    if error is not None:
        fields["error"] = error
    doc = coll.find_one_and_update(
        {"_id": jid},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        ensure_job(court=court, date=date, status=status)
        doc = coll.find_one_and_update(
            {"_id": jid},
            {"$set": fields},
            return_document=ReturnDocument.AFTER,
        )
    return _result("ok", job=doc)


def mark_downloading(*, court: str, date: str) -> dict[str, Any]:
    ensure_job(court=court, date=date, status=STATUS_DOWNLOADING)
    return set_job_status(court=court, date=date, status=STATUS_DOWNLOADING)


def mark_not_published(*, court: str, date: str) -> dict[str, Any]:
    return set_job_status(court=court, date=date, status=STATUS_NOT_PUBLISHED)


def mark_parsing(*, court: str, date: str, pdf_total: int) -> dict[str, Any]:
    coll = _coll()
    if coll is None:
        return _result("skipped_no_uri", job_id=job_id(court, date))

    jid = job_id(court, date)
    ensure_job(court=court, date=date, status=STATUS_PARSING)
    doc = coll.find_one_and_update(
        {"_id": jid},
        {
            "$set": {
                "status": STATUS_PARSING,
                "pdf_total": int(pdf_total),
                "updatedAt": _now(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    return _result("ok", job=doc)


def bump_parsed(*, court: str, date: str, failed: bool = False) -> dict[str, Any]:
    """Increment pdf_parsed or pdf_failed; mark COMPLETE when done."""
    coll = _coll()
    if coll is None:
        return _result("skipped_no_uri", job_id=job_id(court, date))

    jid = job_id(court, date)
    field = "pdf_failed" if failed else "pdf_parsed"
    doc = coll.find_one_and_update(
        {"_id": jid},
        {"$inc": {field: 1}, "$set": {"updatedAt": _now()}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        return _result("error: job not found", job_id=jid)

    total = int(doc.get("pdf_total") or 0)
    parsed = int(doc.get("pdf_parsed") or 0)
    failed_n = int(doc.get("pdf_failed") or 0)
    if total > 0 and parsed + failed_n >= total:
        doc = coll.find_one_and_update(
            {"_id": jid},
            {"$set": {"status": STATUS_COMPLETE, "updatedAt": _now()}},
            return_document=ReturnDocument.AFTER,
        )
    return _result("ok", job=doc)


def get_job(*, court: str, date: str) -> dict[str, Any] | None:
    coll = _coll()
    if coll is None:
        return None
    return coll.find_one({"_id": job_id(court, date)})


def mark_failed(*, court: str, date: str, error: str) -> dict[str, Any]:
    return set_job_status(court=court, date=date, status=STATUS_FAILED, error=error)
