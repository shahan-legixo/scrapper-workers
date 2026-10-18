"""HC/SC scrape contract §6 — scraper-side unit checks (no live courts/GCS)."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bson import ObjectId

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from all_causelist.all_schemas.schema import (  # noqa: E402
    compute_checksum,
    compute_fingerprint,
    make_cause_list_item,
    make_external_id,
)
from all_causelist.cloud.court_map import scrape_court_id_from_cnr  # noqa: E402
from all_causelist.cloud.notify import post_cause_list_notify  # noqa: E402
from db.mongo import (  # noqa: E402
    _item_upsert_filter,
    match_tracking_ids_for_item,
)

# Minimal valid-enough PDF bytes for checksum / upload-skip tests.
_FIXTURE_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
_FIXTURE_PDF_V2 = b"%PDF-1.4\n1 0 obj<< /Changed >>endobj\ntrailer<<>>\n%%EOF\n"


def _fp(**overrides: object) -> str:
    base = {
        "identities": {
            "caseType": "WP",
            "rootCaseNumber": "1234",
            "caseYear": "2024",
            "diaryNo": None,
        },
        "hearing_date": "2026-10-14",
        "listing_no": 1,
        "listing_section": None,
        "purpose": "EVIDENCE",
        "court_no": None,
        "bench": "BENCH",
        "judges": ["J1"],
    }
    base.update(overrides)
    return compute_fingerprint(**base)  # type: ignore[arg-type]


class FingerprintContractTests(unittest.TestCase):
    def test_purpose_in_party_out_order(self) -> None:
        a = _fp()
        # party is not an arg — changing party via item builder must not affect hash
        item_a = make_cause_list_item(
            hearing_date="2026-10-14",
            case_numbers="WP/1234/2024",
            party="A vs B",
            judges=["J1"],
            listing_no=1,
            purpose="EVIDENCE",
            bench="BENCH",
        )
        item_b = make_cause_list_item(
            hearing_date="2026-10-14",
            case_numbers="WP/1234/2024",
            party="X vs Y",
            judges=["J1"],
            listing_no=1,
            purpose="EVIDENCE",
            bench="BENCH",
        )
        self.assertEqual(item_a["fingerprint"], item_b["fingerprint"])
        self.assertEqual(item_a["fingerprint"], a)

    def test_purpose_or_listing_no_changes_hash(self) -> None:
        base = _fp()
        self.assertNotEqual(base, _fp(purpose="APPEARANCE"))
        self.assertNotEqual(base, _fp(listing_no=2))

    def test_missing_slots_are_null_in_material(self) -> None:
        # diaryNo / section / courtNo null → still stable
        fp = _fp(listing_section=None, court_no=None)
        self.assertEqual(len(fp), 64)
        self.assertEqual(fp, _fp())


class ChecksumContractTests(unittest.TestCase):
    def test_byte_checksum_sha256_hex(self) -> None:
        raw = b"%PDF-1.4 fake"
        self.assertEqual(compute_checksum(raw), hashlib.sha256(raw).hexdigest())
        self.assertEqual(compute_checksum(raw), compute_checksum(raw))


class ItemUpsertKeyTests(unittest.TestCase):
    def test_filter_uses_identity_trio_diary_listing(self) -> None:
        doc_id = ObjectId()
        filt = _item_upsert_filter(
            {
                "identities": {
                    "caseType": "WP",
                    "rootCaseNumber": "1234",
                    "caseYear": "2024",
                    "diaryNo": None,
                },
                "listingNo": 5,
                "fingerprint": "abc",
            },
            doc_id,
        )
        self.assertEqual(filt["documentId"], doc_id)
        self.assertEqual(filt["identities.caseType"], "WP")
        self.assertEqual(filt["identities.rootCaseNumber"], "1234")
        self.assertEqual(filt["identities.caseYear"], "2024")
        self.assertIsNone(filt["identities.diaryNo"])
        self.assertEqual(filt["listingNo"], 5)
        self.assertNotIn("fingerprint", filt)


class CourtMapTests(unittest.TestCase):
    def test_dlhc_and_cross_court(self) -> None:
        self.assertEqual(scrape_court_id_from_cnr("DLHC010012342024"), "dlhc")
        self.assertEqual(scrape_court_id_from_cnr("HCBM010012342024"), "bmhc")
        self.assertNotEqual(scrape_court_id_from_cnr("HCBM010012342024"), "dlhc")


class MatchTrackingTests(unittest.TestCase):
    def test_enabled_trio_hit_and_cnr_court_filter(self) -> None:
        item = {
            "identities": {
                "caseType": "WP",
                "rootCaseNumber": "1234",
                "caseYear": "2024",
            }
        }
        hit = {"_id": ObjectId(), "cnr": "DLHC010012342024"}
        miss_court = {"_id": ObjectId(), "cnr": "HCBM010012342024"}
        disabled_would_not_be_in_cursor = hit  # query already filters enabled

        coll = MagicMock()
        coll.find.return_value = [hit, miss_court]
        db = MagicMock()
        db.__getitem__.return_value = coll

        with patch("db.mongo._db", return_value=db):
            ids = match_tracking_ids_for_item(item, court_key="dlhc")

        self.assertEqual(ids, [str(hit["_id"])])
        query = coll.find.call_args[0][0]
        self.assertTrue(query["enabled"])
        self.assertTrue(query["events.causeList"])
        self.assertEqual(query["identities.caseType"], "WP")

    def test_diary_required_when_item_has_diary(self) -> None:
        item = {
            "identities": {
                "caseType": "WP",
                "rootCaseNumber": "1234",
                "caseYear": "2024",
                "diaryNo": "12/2024",
            }
        }
        coll = MagicMock()
        coll.find.return_value = []
        db = MagicMock()
        db.__getitem__.return_value = coll

        with patch("db.mongo._db", return_value=db):
            match_tracking_ids_for_item(item, court_key="dlhc")

        query = coll.find.call_args[0][0]
        self.assertEqual(query["identities.diaryNo"], "12/2024")


class NotifyClientTests(unittest.TestCase):
    def test_http_error_does_not_raise(self) -> None:
        with patch.dict(os.environ, {"CAUSE_LIST_NOTIFY_URL": "https://example.invalid/x"}):
            with patch("httpx.post", side_effect=TimeoutError("boom")):
                result = post_cause_list_notify(
                    item_id=str(ObjectId()),
                    tracking_ids=[str(ObjectId())],
                )
        self.assertEqual(result["status"], "error")

    def test_skipped_when_url_empty(self) -> None:
        with patch.dict(os.environ, {"CAUSE_LIST_NOTIFY_URL": ""}, clear=False):
            # ensure empty wins
            os.environ["CAUSE_LIST_NOTIFY_URL"] = ""
            result = post_cause_list_notify(
                item_id=str(ObjectId()),
                tracking_ids=[str(ObjectId())],
            )
        self.assertEqual(result["status"], "skipped_no_url")


class ExternalIdContractTests(unittest.TestCase):
    def test_court_date_list_bench_format(self) -> None:
        eid = make_external_id(
            "dlhc",
            "2026-10-14",
            "advance",
            "HON'BLE MR. JUSTICE ANISH DAYAL (C)",
        )
        self.assertEqual(
            eid,
            "dlhc:2026-10-14:advance:HON'BLE MR. JUSTICE ANISH DAYAL (C)",
        )
        self.assertNotIn("http", eid)
        self.assertNotIn("gs://", eid)


class GcsPathTests(unittest.TestCase):
    def test_causelist_prefix(self) -> None:
        from all_causelist.cloud.gcs import gcs_object_path

        path = gcs_object_path(
            court_key="bmhc",
            hearing_date="2026-10-12",
            list_type="main",
            filename="coram-01.pdf",
        )
        self.assertEqual(path, "causelist/bmhc/2026-10-12/main/coram-01.pdf")


class GcsUploadSkipTests(unittest.TestCase):
    def test_skip_upload_when_checksum_and_rawref_match(self) -> None:
        from all_causelist.cloud.handlers import _upload_pdf_unless_checksum_unchanged

        sha = compute_checksum(_FIXTURE_PDF)
        existing = {
            "_id": ObjectId(),
            "checksum": sha,
            "rawRef": "gs://legixo-docs-dev/causelist/dlhc/2026-10-14/advance/list.pdf",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.pdf"
            path.write_bytes(_FIXTURE_PDF)
            with patch(
                "all_causelist.cloud.handlers.find_cause_list_unit",
                return_value=existing,
            ):
                with patch("all_causelist.cloud.handlers.upload_pdf") as upload:
                    result = _upload_pdf_unless_checksum_unchanged(
                        local_path=path,
                        court_key="dlhc",
                        hearing_date="2026-10-14",
                        list_type="advance",
                        bench_label="HON'BLE MR. JUSTICE ANISH DAYAL (C)",
                        source_url="https://example.com/x.pdf",
                        filename="list.pdf",
                    )
        self.assertTrue(result["skipped_upload"])
        self.assertEqual(result["gcs_uri"], existing["rawRef"])
        upload.assert_not_called()

    def test_upload_when_checksum_differs(self) -> None:
        from all_causelist.cloud.handlers import _upload_pdf_unless_checksum_unchanged

        existing = {
            "_id": ObjectId(),
            "checksum": compute_checksum(_FIXTURE_PDF),
            "rawRef": "gs://legixo-docs-dev/causelist/dlhc/2026-10-14/advance/list.pdf",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.pdf"
            path.write_bytes(_FIXTURE_PDF_V2)
            with patch(
                "all_causelist.cloud.handlers.find_cause_list_unit",
                return_value=existing,
            ):
                with patch(
                    "all_causelist.cloud.handlers.upload_pdf",
                    return_value={"gcs_uri": "gs://new/path.pdf", "dry_run": True},
                ) as upload:
                    result = _upload_pdf_unless_checksum_unchanged(
                        local_path=path,
                        court_key="dlhc",
                        hearing_date="2026-10-14",
                        list_type="advance",
                        bench_label="BENCH",
                        source_url=None,
                        filename="list.pdf",
                    )
        self.assertFalse(result["skipped_upload"])
        self.assertEqual(result["gcs_uri"], "gs://new/path.pdf")
        upload.assert_called_once()


class StoreRematchFlowTests(unittest.TestCase):
    """Fixture-unit style: same PDF twice → skip rewrite; still rematch/notify."""

    def test_handle_parse_checksum_unchanged_skips_upsert_still_notifies(self) -> None:
        from all_causelist.cloud import handlers

        sha = compute_checksum(_FIXTURE_PDF)
        doc_id = ObjectId()
        item_id = ObjectId()
        existing = {
            "_id": doc_id,
            "checksum": sha,
            "rawRef": "gs://legixo-docs-dev/causelist/dlhc/2026-10-14/advance/x.pdf",
        }
        items = [
            {
                "_id": item_id,
                "documentId": doc_id,
                "fingerprint": "abc",
                "identities": {
                    "caseType": "WP",
                    "rootCaseNumber": "1",
                    "caseYear": "2024",
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.pdf"
            path.write_bytes(_FIXTURE_PDF)
            with patch.object(handlers, "find_cause_list_unit", return_value=existing):
                with patch.object(
                    handlers, "list_items_for_document", return_value=items
                ) as list_items:
                    with patch.object(
                        handlers,
                        "match_and_notify_items",
                        return_value={"matched_items": 1, "notify_ok": 0},
                    ) as notify:
                        with patch.object(handlers, "upsert_main_output") as upsert:
                            with patch.object(handlers, "bump_parsed", return_value={}):
                                result = handlers.handle_parse(
                                    {
                                        "court": "dlhc",
                                        "date": "2026-10-14",
                                        "list_type": "advance",
                                        "label": "HON'BLE MR. JUSTICE ANISH DAYAL (C)",
                                        "gcs_uri": existing["rawRef"],
                                        "local_path": str(path),
                                    }
                                )
        self.assertTrue(result["checksum_unchanged"])
        upsert.assert_not_called()
        list_items.assert_called_once_with(doc_id)
        notify.assert_called_once()
        self.assertEqual(result["mongodb"]["status"], "checksum_unchanged")


class MongoCollectionNamesTests(unittest.TestCase):
    def test_collection_names_default(self) -> None:
        import db.mongo as mongo
        self.assertEqual(mongo.mongo_collection_name(), "causelists")
        self.assertEqual(mongo._CAUSE_LIST, "causelists")
        self.assertEqual(mongo._CAUSE_LIST_ITEM, "causelistsitems")
        result = mongo._result("ok")
        self.assertEqual(result["collection"], "causelists+causelistsitems")

    def test_find_cause_list_unit_targets_causelists_collection(self) -> None:
        import db.mongo as mongo
        mock_db = MagicMock()
        with patch("db.mongo._db", return_value=mock_db):
            mongo.find_cause_list_unit(
                source="test",
                court_key="dlhc",
                hearing_date="2026-10-14",
                list_type="main",
                bench_label="BENCH",
                external_id="ext-1",
            )
        mock_db.__getitem__.assert_called_with("causelists")

    def test_list_items_targets_causelistsitems_collection(self) -> None:
        import db.mongo as mongo
        mock_db = MagicMock()
        mock_coll = MagicMock()
        mock_coll.find.return_value = []
        mock_db.__getitem__.return_value = mock_coll
        with patch("db.mongo._db", return_value=mock_db):
            mongo.list_items_for_document(ObjectId())
        mock_db.__getitem__.assert_called_with("causelistsitems")


if __name__ == "__main__":
    unittest.main()

