"""Upsert Legixo-shaped mainOutput into causelists + causelistsitems.

Local-first defaults:
  MONGO_URI=mongodb://localhost:27017
  MONGO_DB=causelists

Collections (fixed):
  causelists
  causelistsitems
  casealerttracking

Set MONGO_URI= (empty) to disable sync without removing code.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

log = logging.getLogger("causelist.mongo")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from load_env import load_env

    load_env()
except ImportError:
    pass

_DEFAULT_URI = "mongodb://localhost:27017"
_DEFAULT_DB = "causelists"
_CAUSE_LIST = (
    os.environ.get("MONGO_CAUSE_LISTS_COLLECTION")
    or os.environ.get("MONGO_CAUSE_LIST_COLLECTION")
    or "causelists"
).strip()
_CAUSE_LIST_ITEM = (
    os.environ.get("MONGO_CAUSE_LISTS_ITEMS_COLLECTION")
    or os.environ.get("MONGO_CAUSE_LIST_ITEM_COLLECTION")
    or "causelistsitems"
).strip()
_CASE_ALERT_TRACKING = "casealerttracking"

_client: MongoClient | None = None
_indexes_ready = False


def mongo_uri() -> str | None:
    """Return configured URI, or None when sync is explicitly disabled."""
    if "MONGO_URI" in os.environ:
        value = os.environ["MONGO_URI"].strip()
        return value or None
    return _DEFAULT_URI


def mongo_db_name() -> str:
    return (os.environ.get("MONGO_DB") or _DEFAULT_DB).strip() or _DEFAULT_DB


def mongo_collection_name() -> str:
    """Primary collection label for API responses."""
    return _CAUSE_LIST


def reset_client() -> None:
    """Drop cached client (tests / env changes)."""
    global _client, _indexes_ready
    if _client is not None:
        _client.close()
    _client = None
    _indexes_ready = False


def _db():
    global _client, _indexes_ready
    uri = mongo_uri()
    if uri is None:
        return None
    if _client is None:
        _client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    db = _client[mongo_db_name()]
    if not _indexes_ready:
        docs: Collection = db[_CAUSE_LIST]
        items: Collection = db[_CAUSE_LIST_ITEM]
        docs.create_index(
            [
                ("source", 1),
                ("courtKey", 1),
                ("hearingDate", 1),
                ("listType", 1),
                ("benchLabel", 1),
                ("externalId", 1),
            ],
            unique=True,
        )
        docs.create_index([("checksum", 1)])
        docs.create_index([("hearingDate", 1), ("courtLevel", 1), ("listType", 1)])
        items.create_index([("documentId", 1)])
        # Contract upsert key (replace legacy documentId+fingerprint unique).
        try:
            items.drop_index("documentId_1_fingerprint_1")
        except Exception:  # noqa: BLE001
            pass
        items.create_index(
            [
                ("documentId", 1),
                ("identities.caseType", 1),
                ("identities.rootCaseNumber", 1),
                ("identities.caseYear", 1),
                ("identities.diaryNo", 1),
                ("listingNo", 1),
            ],
            unique=True,
            name="cause_list_item_identity_key",
        )
        items.create_index([("cnr", 1), ("hearingDate", 1)])
        items.create_index(
            [
                ("identities.caseType", 1),
                ("identities.rootCaseNumber", 1),
                ("identities.caseYear", 1),
                ("hearingDate", 1),
            ]
        )
        items.create_index([("identities.diaryNo", 1), ("hearingDate", 1)])
        items.create_index([("fingerprint", 1)])
        _indexes_ready = True
    return db


def get_database():
    """Return the configured Mongo database, or None when URI disabled."""
    return _db()


def get_collection() -> Collection | None:
    """Back-compat: return CauseList collection, or None when URI disabled."""
    db = _db()
    if db is None:
        return None
    return db[_CAUSE_LIST]


def _result(status: str, **counts: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": status,
        "upserted": 0,
        "modified": 0,
        "matched": 0,
        "documents_upserted": 0,
        "items_upserted": 0,
        "collection": f"{_CAUSE_LIST}+{_CAUSE_LIST_ITEM}",
    }
    base.update(counts)
    return base


def _parse_hearing_date(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("hearingDate required")
    # YYYY-MM-DD
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return datetime(
            int(text[0:4]),
            int(text[5:7]),
            int(text[8:10]),
            tzinfo=timezone.utc,
        )
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parent_filter(doc: dict[str, Any], hearing: datetime) -> dict[str, Any]:
    return {
        "source": doc["source"],
        "courtKey": doc["courtKey"],
        "hearingDate": hearing,
        "listType": doc["listType"],
        "benchLabel": doc.get("benchLabel") or "",
        "externalId": doc.get("externalId") or "",
    }


def _parent_set(doc: dict[str, Any], hearing: datetime, now: datetime) -> dict[str, Any]:
    fields = {
        "source": doc["source"],
        "courtLevel": doc["courtLevel"],
        "courtKey": doc["courtKey"],
        "hearingDate": hearing,
        "listType": doc["listType"],
        "benchLabel": doc.get("benchLabel") or "",
        "checksum": doc["checksum"],
        "status": doc.get("status") or "ok",
        "externalId": doc.get("externalId") or "",
        "updatedAt": now,
    }
    if doc.get("rawRef"):
        fields["rawRef"] = doc["rawRef"]
        fields["contentType"] = doc.get("contentType") or "application/pdf"
    elif doc.get("contentType"):
        fields["contentType"] = doc["contentType"]
    return fields


def _item_set(
    item: dict[str, Any],
    document_id: ObjectId,
    hearing: datetime,
    now: datetime,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "documentId": document_id,
        "hearingDate": hearing,
        "fingerprint": item["fingerprint"],
        "identities": item.get("identities") or {},
        "updatedAt": now,
    }
    for key in (
        "cnr",
        "listingNo",
        "listingSection",
        "purpose",
        "courtNo",
        "bench",
        "judges",
        "party",
        "payload",
    ):
        if key in item and item[key] is not None:
            fields[key] = item[key]
    return fields


def _item_upsert_filter(item: dict[str, Any], document_id: ObjectId) -> dict[str, Any]:
    """Contract key: documentId + trio + diaryNo + listingNo (null when missing)."""
    identities = item.get("identities") or {}
    return {
        "documentId": document_id,
        "identities.caseType": identities.get("caseType"),
        "identities.rootCaseNumber": identities.get("rootCaseNumber"),
        "identities.caseYear": identities.get("caseYear"),
        "identities.diaryNo": identities.get("diaryNo"),
        "listingNo": item.get("listingNo"),
    }


def find_cause_list_unit(
    *,
    source: str,
    court_key: str,
    hearing_date: str | datetime,
    list_type: str,
    bench_label: str,
    external_id: str,
) -> dict[str, Any] | None:
    """findOne CauseList by unit identity."""
    db = _db()
    if db is None:
        return None
    hearing = (
        hearing_date
        if isinstance(hearing_date, datetime)
        else _parse_hearing_date(hearing_date)
    )
    return db[_CAUSE_LIST].find_one(
        {
            "source": source,
            "courtKey": court_key,
            "hearingDate": hearing,
            "listType": list_type,
            "benchLabel": bench_label or "",
            "externalId": external_id or "",
        }
    )


def list_items_for_document(document_id: ObjectId | str) -> list[dict[str, Any]]:
    """Load CauseListItems for a parent CauseList."""
    db = _db()
    if db is None:
        return []
    oid = (
        document_id
        if isinstance(document_id, ObjectId)
        else ObjectId(str(document_id))
    )
    return list(db[_CAUSE_LIST_ITEM].find({"documentId": oid}))


def match_tracking_ids_for_item(
    item: dict[str, Any],
    *,
    court_key: str,
) -> list[str]:
    """Query CaseAlertTracking for one item; return tracking ObjectId strings."""
    db = _db()
    if db is None:
        return []

    identities = item.get("identities") or {}
    case_type = (identities.get("caseType") or "").strip().upper()
    root = (identities.get("rootCaseNumber") or "").strip()
    year = (identities.get("caseYear") or "").strip()
    if not case_type or not root or not year:
        return []

    query: dict[str, Any] = {
        "enabled": True,
        "events.causeList": True,
        "identities.caseType": case_type,
        "identities.rootCaseNumber": root,
        "identities.caseYear": year,
    }
    diary = identities.get("diaryNo")
    if diary not in (None, ""):
        query["identities.diaryNo"] = str(diary).strip()

    from all_causelist.cloud.court_map import scrape_court_id_from_cnr

    tracking_ids: list[str] = []
    for row in db[_CASE_ALERT_TRACKING].find(query, {"_id": 1, "cnr": 1}):
        cnr = row.get("cnr")
        if cnr:
            mapped = scrape_court_id_from_cnr(str(cnr))
            if mapped is not None and mapped != court_key:
                continue
        tracking_ids.append(str(row["_id"]))
    if tracking_ids:
        fp = str(item.get("fingerprint") or "")[:16]
        log.info(
            "tracking MATCH court=%s itemId=%s fp=%s... case=%s/%s/%s tracking_count=%s",
            court_key,
            item.get("_id"),
            fp or "-",
            case_type,
            root,
            year,
            len(tracking_ids),
        )
    return tracking_ids


def match_and_notify_items(
    items: list[dict[str, Any]],
    *,
    court_key: str,
) -> dict[str, Any]:
    """For each item: match tracking → POST notify. Never raises."""
    from all_causelist.cloud.notify import post_cause_list_notify

    log.info("notify loop START court=%s items=%s", court_key, len(items))
    matched_items = 0
    notify_ok = 0
    notify_fail = 0
    notify_skip = 0
    for item in items:
        if not item.get("_id"):
            continue
        tracking_ids = match_tracking_ids_for_item(item, court_key=court_key)
        if not tracking_ids:
            continue
        matched_items += 1
        fp = str(item.get("fingerprint") or "")[:16]
        log.info(
            "notify POST court=%s itemId=%s fp=%s... trackingIds=%s",
            court_key,
            item["_id"],
            fp or "-",
            tracking_ids,
        )
        result = post_cause_list_notify(
            item_id=str(item["_id"]),
            tracking_ids=tracking_ids,
        )
        status = result.get("status")
        if status == "ok":
            notify_ok += 1
        elif status in ("skipped_no_url", "skipped_no_tracking"):
            notify_skip += 1
            log.info(
                "notify SKIPPED status=%s itemId=%s",
                status,
                item["_id"],
            )
        else:
            notify_fail += 1
    summary = {
        "item_count": len(items),
        "matched_items": matched_items,
        "notify_ok": notify_ok,
        "notify_fail": notify_fail,
        "notify_skip": notify_skip,
    }
    log.info("notify loop DONE court=%s %s", court_key, summary)
    return summary


def upsert_main_output(main_output: dict[str, Any]) -> dict[str, Any]:
    """Upsert CauseList parents then CauseListItem children. Soft-fails via status."""
    if mongo_uri() is None:
        return _result("skipped_no_uri")

    documents = main_output.get("documents")
    # Legacy flat cases: reject with clear error (schema migrated).
    if not isinstance(documents, list):
        if isinstance(main_output.get("cases"), list):
            return _result(
                "error: legacy cases[] mainOutput; re-scrape to emit documents[]"
            )
        return _result("error: documents must be a list")

    court_key = str(
        main_output.get("courtKey") or main_output.get("court") or ""
    ).strip()
    hearing_raw = main_output.get("hearingDate") or main_output.get("date")
    if not court_key or not hearing_raw:
        return _result("error: courtKey and hearingDate required on mainOutput")

    try:
        db = _db()
        if db is None:
            return _result("skipped_no_uri")
        db.client.admin.command("ping")
        docs_coll = db[_CAUSE_LIST]
        items_coll = db[_CAUSE_LIST_ITEM]
        now = datetime.now(timezone.utc)

        docs_upserted = 0
        docs_modified = 0
        items_upserted = 0
        items_modified = 0
        items_matched = 0
        items_skipped_no_fp = 0
        item_ops: list[UpdateOne] = []

        for doc in documents:
            if not isinstance(doc, dict):
                continue
            hearing = _parse_hearing_date(doc.get("hearingDate") or hearing_raw)
            filt = _parent_filter(doc, hearing)
            fields = _parent_set(doc, hearing, now)
            checksum = doc.get("checksum")
            log.info(
                "upsert CauseList court=%s externalId=%s checksum=%s... items=%s",
                doc.get("courtKey") or court_key,
                doc.get("externalId") or "-",
                (str(checksum)[:16] + "...") if checksum else "-",
                len(doc.get("items") or []),
            )
            res = docs_coll.update_one(
                filt,
                {"$set": fields, "$setOnInsert": {"createdAt": now}},
                upsert=True,
            )
            if res.upserted_id is not None:
                docs_upserted += 1
                document_id = res.upserted_id
            else:
                if res.modified_count:
                    docs_modified += 1
                existing = docs_coll.find_one(filt, {"_id": 1})
                if not existing:
                    continue
                document_id = existing["_id"]

            for item in doc.get("items") or []:
                if not isinstance(item, dict) or not item.get("fingerprint"):
                    items_skipped_no_fp += 1
                    continue
                item_fields = _item_set(item, document_id, hearing, now)
                item_ops.append(
                    UpdateOne(
                        _item_upsert_filter(item, document_id),
                        {
                            "$set": item_fields,
                            "$setOnInsert": {"createdAt": now},
                        },
                        upsert=True,
                    )
                )

        if items_skipped_no_fp:
            log.warning(
                "upsert skipped %s items without fingerprint court=%s",
                items_skipped_no_fp,
                court_key,
            )

        if item_ops:
            # Chunk bulk writes to avoid huge payloads.
            chunk = 500
            for i in range(0, len(item_ops), chunk):
                bulk = items_coll.bulk_write(item_ops[i : i + chunk], ordered=False)
                items_upserted += bulk.upserted_count
                items_modified += bulk.modified_count
                items_matched += bulk.matched_count
            sample_fps = []
            for doc in documents:
                for item in doc.get("items") or []:
                    if isinstance(item, dict) and item.get("fingerprint"):
                        sample_fps.append(str(item["fingerprint"])[:16])
                    if len(sample_fps) >= 3:
                        break
                if len(sample_fps) >= 3:
                    break
            log.info(
                "upsert CauseListItem ops=%s upserted=%s modified=%s sample_fp=%s",
                len(item_ops),
                items_upserted,
                items_modified,
                sample_fps or "-",
            )

        result = _result(
            "ok",
            upserted=docs_upserted + items_upserted,
            modified=docs_modified + items_modified,
            matched=items_matched,
            documents_upserted=docs_upserted,
            documents_modified=docs_modified,
            items_upserted=items_upserted,
            items_modified=items_modified,
            document_count=len(documents),
            item_count=sum(
                len(d.get("items") or [])
                for d in documents
                if isinstance(d, dict)
            ),
            case_count=sum(
                len(d.get("items") or [])
                for d in documents
                if isinstance(d, dict)
            ),
        )
        log.info("upsert DONE court=%s %s", court_key, result)
        return result
    except (PyMongoError, ValueError, KeyError) as exc:
        reset_client()
        log.warning("upsert FAILED court=%s error=%s", court_key, exc)
        return _result(f"error: {exc}")
