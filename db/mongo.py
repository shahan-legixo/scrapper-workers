"""Upsert Legixo-shaped mainOutput into causelists + causelistitems.

Local-first defaults:
  MONGO_URI=mongodb://localhost:27017
  MONGO_DB=causelists

Collections (fixed):
  causelists
  causelistitems
  casealerttrackings

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
from pymongo import InsertOne, MongoClient, UpdateOne
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
_DEFAULT_DB = "test"
_CAUSE_LIST = (
    os.environ.get("MONGO_CAUSE_LISTS_COLLECTION")
    or os.environ.get("MONGO_CAUSE_LIST_COLLECTION")
    or "causelists"
).strip()
_CAUSE_LIST_ITEM = (
    os.environ.get("MONGO_CAUSE_LISTS_ITEMS_COLLECTION")
    or os.environ.get("MONGO_CAUSE_LIST_ITEM_COLLECTION")
    or "causelistitems"
).strip()
_CASE_ALERT_TRACKING = (
    os.environ.get("MONGO_CASE_ALERT_TRACKING_COLLECTION")
    or "casealerttrackings"
).strip()

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
        # Contract upsert key: documentId + identities (+ listingNo).
        # Drop legacy fingerprint unique so same case can exist on main + supplementary.
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
        "created_items": [],
        "updated_items": [],
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

def cause_list_items_enabled_tracking() -> list[dict[str, Any]]:
    """Load enabled CaseAlertTracking rows with cause-list toggle on."""
    db = _db()
    if db is None:
        return []
    return list(
        db[_CASE_ALERT_TRACKING].find(
            {"enabled": True, "events.causeList": True},
            {"identities": 1, "cnr": 1},
        )
    )


def _identity_key(
    identities: dict[str, Any] | None,
) -> tuple[str, str, str] | None:
    """Normalize identities to (caseType, rootCaseNumber, caseYear), or None."""
    idn = identities or {}
    case_type = (idn.get("caseType") or "").strip().upper()
    root = (idn.get("rootCaseNumber") or "").strip()
    year = (idn.get("caseYear") or "").strip()
    if not case_type or not root or not year:
        return None
    return (case_type, root, year)


def load_enabled_cause_list_tracking_keys(
    court_key: str,
) -> set[tuple[str, str, str]]:
    """Build identity key set for tracked cases (optional CNR→court filter)."""
    from all_causelist.cloud.court_map import scrape_court_id_from_cnr

    keys: set[tuple[str, str, str]] = set()
    for row in cause_list_items_enabled_tracking():
        cnr = row.get("cnr")
        if cnr:
            mapped = scrape_court_id_from_cnr(str(cnr))
            if mapped is not None and mapped != court_key:
                continue
        raw_idn = row.get("identities")
        key = _identity_key(raw_idn if isinstance(raw_idn, dict) else None)
        if key is not None:
            keys.add(key)
    return keys


def filter_documents_to_tracked(
    documents: list[Any],
    *,
    court_key: str,
    tracked_keys: set[tuple[str, str, str]] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Keep only items whose identities match enabled cause-list tracking.

    Returns (filtered_documents, items_in, items_kept).
    Parents with zero kept items are dropped.
    """
    keys = (
        tracked_keys
        if tracked_keys is not None
        else load_enabled_cause_list_tracking_keys(court_key)
    )
    items_in = 0
    items_kept = 0
    out: list[dict[str, Any]] = []

    for doc in documents:
        if not isinstance(doc, dict):
            continue
        raw_items = doc.get("items") or []
        if not isinstance(raw_items, list):
            raw_items = []
        items_in += len(raw_items)
        kept: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            raw_idn = item.get("identities")
            key = _identity_key(raw_idn if isinstance(raw_idn, dict) else None)
            if key is not None and key in keys:
                kept.append(item)
        items_kept += len(kept)
        if not kept:
            continue
        filtered = dict(doc)
        filtered["items"] = kept
        out.append(filtered)
    return out, items_in, items_kept


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
    """Query CaseAlertTracking; return IDs that still need notify (fingerprint gate).

    Skips trackings where ``lastFingerprints.causeList`` already equals the
    item fingerprint (same listing already alerted).
    """
    db = _db()
    if db is None:
        return []

    identities = item.get("identities") or {}
    case_type = (identities.get("caseType") or "").strip().upper()
    root = (identities.get("rootCaseNumber") or "").strip()
    year = (identities.get("caseYear") or "").strip()


    if not case_type or not root or not year:
        return []

    item_fp = str(item.get("fingerprint") or "").strip()
    if not item_fp:
        log.info(
            "tracking SKIP no item fingerprint court=%s itemId=%s",
            court_key,
            item.get("_id"),
        )
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
    skipped_fingerprint = 0
    for row in db[_CASE_ALERT_TRACKING].find(
        query,
        {"_id": 1, "cnr": 1, "lastFingerprints.causeList": 1},
    ):
        cnr = row.get("cnr")
        if cnr:
            mapped = scrape_court_id_from_cnr(str(cnr))
            if mapped is not None and mapped != court_key:
                continue
        last_fps = row.get("lastFingerprints") or {}
        last_fp = str(last_fps.get("causeList") or "").strip()
        if last_fp and last_fp == item_fp:
            skipped_fingerprint += 1
            log.info(
                "tracking SKIP fingerprint court=%s itemId=%s trackingId=%s fp=%s...",
                court_key,
                item.get("_id"),
                row["_id"],
                item_fp[:16],
            )
            continue
        tracking_ids.append(str(row["_id"]))
    if tracking_ids or skipped_fingerprint:
        log.info(
            "tracking MATCH court=%s itemId=%s fp=%s... case=%s/%s/%s "
            "notify_ids=%s skipped_fingerprint=%s",
            court_key,
            item.get("_id"),
            item_fp[:16],
            case_type,
            root,
            year,
            len(tracking_ids),
            skipped_fingerprint,
        )
    return tracking_ids


def _notify_pairs_for_items(
    items: list[dict[str, Any]],
    *,
    court_key: str,
) -> list[dict[str, Any]]:
    """Match tracking for each item; omit entries with no trackingIds."""
    pairs: list[dict[str, Any]] = []
    for item in items:
        if not item.get("_id"):
            continue
        tracking_ids = match_tracking_ids_for_item(item, court_key=court_key)
        if not tracking_ids:
            continue
        pairs.append(
            {
                "itemId": str(item["_id"]),
                "trackingIds": tracking_ids,
            }
        )
    return pairs


def build_notify_batches(
    created_items: list[dict[str, Any]] | None,
    updated_items: list[dict[str, Any]] | None,
    *,
    court_key: str,
) -> dict[str, Any]:
    """Build notify payload: created/updated [{itemId, trackingIds}, ...]."""
    return {
        "createdListItems": _notify_pairs_for_items(
            list(created_items or []), court_key=court_key
        ),
        "updatedListItems": _notify_pairs_for_items(
            list(updated_items or []), court_key=court_key
        ),
    }


def match_and_notify_created_updated(
    created_items: list[dict[str, Any]] | None,
    updated_items: list[dict[str, Any]] | None,
    *,
    court_key: str,
) -> dict[str, Any]:
    """Match tracking for created/updated items → one batch POST. Never raises."""
    from all_causelist.cloud.notify import post_cause_list_notify_batch

    batches = build_notify_batches(
        created_items, updated_items, court_key=court_key
    )
    created_n = len(batches["createdListItems"])
    updated_n = len(batches["updatedListItems"])
    log.info(
        "notify batch START court=%s createdListItems=%s updatedListItems=%s",
        court_key,
        created_n,
        updated_n,
    )
    result = post_cause_list_notify_batch(batches)
    status = str(result.get("status") or "")
    summary = {
        **result,
        "created_pairs": created_n,
        "updated_pairs": updated_n,
        "matched_items": created_n + updated_n,
        "notify_ok": int(status == "ok"),
        "notify_fail": int(status in ("http_error", "error")),
        "notify_skip": int(status.startswith("skipped_")),
        "batches": batches,
    }
    log.info(
        "notify batch DONE court=%s %s",
        court_key,
        {
            k: summary[k]
            for k in (
                "status",
                "created_pairs",
                "updated_pairs",
                "notify_ok",
                "notify_fail",
                "notify_skip",
            )
            if k in summary
        },
    )
    return summary


def match_and_notify_items(
    items: list[dict[str, Any]],
    *,
    court_key: str,
) -> dict[str, Any]:
    """Rematch path: treat items as updated (no creates). Never raises."""
    return match_and_notify_created_updated(
        created_items=[],
        updated_items=items,
        court_key=court_key,
    )


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
        items_inserted = 0
        items_modified = 0
        items_matched = 0
        items_unchanged = 0
        items_skipped_no_fp = 0
        item_ops: list[InsertOne | UpdateOne] = []
        created_ids: list[ObjectId] = []
        updated_ids: list[ObjectId] = []

        tracked_keys = load_enabled_cause_list_tracking_keys(court_key)
        documents, items_in, items_kept = filter_documents_to_tracked(
            documents,
            court_key=court_key,
            tracked_keys=tracked_keys,
        )
        items_discarded = items_in - items_kept
        log.info(
            "upsert tracking filter court=%s items_in=%s items_kept=%s "
            "items_discarded=%s tracked_keys=%s docs_kept=%s",
            court_key,
            items_in,
            items_kept,
            items_discarded,
            len(tracked_keys),
            len(documents),
        )
        if not documents:
            return _result(
                "skipped_no_tracked_items",
                items_in=items_in,
                items_kept=0,
                items_discarded=items_discarded,
                document_count=0,
                item_count=0,
                case_count=0,
            )

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
            # Parent: match list key → reuse _id; missing → create (upsert=True).
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
                # No item upsert=True: find by documentId+identities, then
                # missing → InsertOne; same fp → skip; different fp → UpdateOne.
                item_filt = _item_upsert_filter(item, document_id)
                existing_item = items_coll.find_one(
                    item_filt, {"_id": 1, "fingerprint": 1}
                )
                item_fields = _item_set(item, document_id, hearing, now)
                incoming_fp = str(item.get("fingerprint") or "").strip()
                if existing_item is None:
                    new_id = ObjectId()
                    item_fields["_id"] = new_id
                    item_fields["createdAt"] = now
                    item_ops.append(InsertOne(item_fields))
                    created_ids.append(new_id)
                elif str(existing_item.get("fingerprint") or "").strip() == incoming_fp:
                    items_unchanged += 1
                else:
                    updated_ids.append(existing_item["_id"])
                    item_ops.append(
                        UpdateOne(
                            {"_id": existing_item["_id"]},
                            {"$set": item_fields},
                            upsert=False,
                        )
                    )

        if items_skipped_no_fp:
            log.warning(
                "upsert skipped %s items without fingerprint court=%s",
                items_skipped_no_fp,
                court_key,
            )

        if item_ops:
            chunk = 500
            for i in range(0, len(item_ops), chunk):
                bulk = items_coll.bulk_write(item_ops[i : i + chunk], ordered=False)
                items_inserted += int(getattr(bulk, "inserted_count", 0) or 0)
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
                "write CauseListItem ops=%s inserted=%s modified=%s "
                "unchanged=%s sample_fp=%s",
                len(item_ops),
                items_inserted,
                items_modified,
                items_unchanged,
                sample_fps or "-",
            )

        created_items: list[dict[str, Any]] = []
        if created_ids:
            created_items = list(items_coll.find({"_id": {"$in": created_ids}}))
        updated_items: list[dict[str, Any]] = []
        if updated_ids:
            updated_items = list(items_coll.find({"_id": {"$in": updated_ids}}))

        result = _result(
            "ok",
            upserted=docs_upserted + items_inserted,
            modified=docs_modified + items_modified,
            matched=items_matched,
            documents_upserted=docs_upserted,
            documents_modified=docs_modified,
            items_inserted=items_inserted,
            items_modified=items_modified,
            items_unchanged=items_unchanged,
            items_in=items_in,
            items_kept=items_kept,
            items_discarded=items_discarded,
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
            created_items=created_items,
            updated_items=updated_items,
        )
        return result
    except (PyMongoError, ValueError, KeyError) as exc:
        reset_client()
        log.warning("upsert FAILED court=%s error=%s", court_key, exc)
        return _result(f"error: {exc}")
