"""Upsert Legixo-shaped mainOutput into CauseList + CauseListItem.

Local-first defaults:
  MONGO_URI=mongodb://localhost:27017
  MONGO_DB=causelists

Collections (fixed to Legixo names):
  CauseList
  CauseListItem

Set MONGO_URI= (empty) to disable sync without removing code.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from pymongo import MongoClient, ReplaceOne, UpdateOne
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

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
_CAUSE_LIST = "CauseList"
_CAUSE_LIST_ITEM = "CauseListItem"

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
        items.create_index([("documentId", 1), ("fingerprint", 1)], unique=True)
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
        item_ops: list[UpdateOne] = []

        for doc in documents:
            if not isinstance(doc, dict):
                continue
            hearing = _parse_hearing_date(doc.get("hearingDate") or hearing_raw)
            filt = _parent_filter(doc, hearing)
            fields = _parent_set(doc, hearing, now)
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
                    continue
                item_fields = _item_set(item, document_id, hearing, now)
                item_ops.append(
                    UpdateOne(
                        {
                            "documentId": document_id,
                            "fingerprint": item["fingerprint"],
                        },
                        {
                            "$set": item_fields,
                            "$setOnInsert": {"createdAt": now},
                        },
                        upsert=True,
                    )
                )

        if item_ops:
            # Chunk bulk writes to avoid huge payloads.
            chunk = 500
            for i in range(0, len(item_ops), chunk):
                bulk = items_coll.bulk_write(item_ops[i : i + chunk], ordered=False)
                items_upserted += bulk.upserted_count
                items_modified += bulk.modified_count
                items_matched += bulk.matched_count

        return _result(
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
    except (PyMongoError, ValueError, KeyError) as exc:
        reset_client()
        return _result(f"error: {exc}")
