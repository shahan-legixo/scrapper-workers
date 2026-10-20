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
    parse_printed_case_number,
)
from all_causelist.cloud.court_map import scrape_court_id_from_cnr  # noqa: E402
from all_causelist.cloud.notify import (  # noqa: E402
    post_cause_list_notify,
    post_cause_list_notify_batch,
)
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
        "list_type": "main",
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
            list_type="main",
        )
        item_b = make_cause_list_item(
            hearing_date="2026-10-14",
            case_numbers="WP/1234/2024",
            party="X vs Y",
            judges=["J1"],
            listing_no=1,
            purpose="EVIDENCE",
            bench="BENCH",
            list_type="main",
        )
        self.assertEqual(item_a["fingerprint"], item_b["fingerprint"])
        self.assertEqual(item_a["fingerprint"], a)

    def test_purpose_or_listing_no_changes_hash(self) -> None:
        base = _fp()
        self.assertNotEqual(base, _fp(purpose="APPEARANCE"))
        self.assertNotEqual(base, _fp(listing_no=2))

    def test_list_type_changes_hash(self) -> None:
        """Main vs supplementary must differ so Step 3 notify is not fingerprint-gated."""
        main_fp = _fp(list_type="main")
        supp_fp = _fp(list_type="supplementary")
        self.assertNotEqual(main_fp, supp_fp)
        item_main = make_cause_list_item(
            hearing_date="2026-10-14",
            case_numbers="WP/1234/2024",
            judges=["J1"],
            listing_no=1,
            purpose="EVIDENCE",
            bench="BENCH",
            list_type="main",
        )
        item_supp = make_cause_list_item(
            hearing_date="2026-10-14",
            case_numbers="WP/1234/2024",
            judges=["J1"],
            listing_no=1,
            purpose="EVIDENCE",
            bench="BENCH",
            list_type="supplementary",
        )
        self.assertNotEqual(item_main["fingerprint"], item_supp["fingerprint"])
        self.assertEqual(item_main["fingerprint"], main_fp)
        self.assertEqual(item_supp["fingerprint"], supp_fp)

    def test_missing_slots_are_null_in_material(self) -> None:
        # diaryNo / section / courtNo null → still stable
        fp = _fp(listing_section=None, court_no=None)
        self.assertEqual(len(fp), 64)
        self.assertEqual(fp, _fp())


class PrintedCaseTypeTests(unittest.TestCase):
    def test_keeps_dots_uppercase_drops_spaces_and_trailing_hyphen(self) -> None:
        samples = {
            "CO.PET. - 153/1998": "CO.PET.",
            "co.pet. - 153/1998": "CO.PET.",
            "W.P.(C) No. 180/2004": "W.P.(C)",
            "w.p.(c) no. 180/2004": "W.P.(C)",
            "WP/2166/2009": "WP",
            "C.A. No. 329-330/2004 101. Connected": "C.A.",
            "CO.APPL. 1804/2017": "CO.APPL.",
            "COMP - 7/2008": "COMP",
            "WP - 19459/2016 (S)": "WP",
            "WP-2166/2009": "WP",
        }
        for printed, case_type in samples.items():
            parsed = parse_printed_case_number(printed)
            self.assertEqual(parsed.get("caseType"), case_type, printed)

    def test_cause_list_item_stores_printed_type(self) -> None:
        item = make_cause_list_item(
            hearing_date="2026-10-14",
            case_numbers="CO.PET. - 153/1998",
        )
        self.assertEqual(item["identities"]["caseType"], "CO.PET.")
        self.assertEqual(item["identities"]["rootCaseNumber"], "153")
        self.assertEqual(item["identities"]["caseYear"], "1998")


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
            "fingerprint": "fp-listing-1",
            "identities": {
                "caseType": "WP",
                "rootCaseNumber": "1234",
                "caseYear": "2024",
            },
        }
        hit = {"_id": ObjectId(), "cnr": "DLHC010012342024"}
        miss_court = {"_id": ObjectId(), "cnr": "HCBM010012342024"}

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
        projection = coll.find.call_args[0][1]
        self.assertIn("lastFingerprints.causeList", projection)

    def test_diary_required_when_item_has_diary(self) -> None:
        item = {
            "fingerprint": "fp-listing-1",
            "identities": {
                "caseType": "WP",
                "rootCaseNumber": "1234",
                "caseYear": "2024",
                "diaryNo": "12/2024",
            },
        }
        coll = MagicMock()
        coll.find.return_value = []
        db = MagicMock()
        db.__getitem__.return_value = coll

        with patch("db.mongo._db", return_value=db):
            match_tracking_ids_for_item(item, court_key="dlhc")

        query = coll.find.call_args[0][0]
        self.assertEqual(query["identities.diaryNo"], "12/2024")

    def test_skips_tracking_when_last_fingerprint_matches(self) -> None:
        item = {
            "fingerprint": "fp-same",
            "identities": {
                "caseType": "WP",
                "rootCaseNumber": "1234",
                "caseYear": "2024",
            },
        }
        already_alerted = {
            "_id": ObjectId(),
            "cnr": "DLHC010012342024",
            "lastFingerprints": {"causeList": "fp-same"},
        }
        needs_alert = {
            "_id": ObjectId(),
            "cnr": "DLHC010099992024",
            "lastFingerprints": {"causeList": "fp-old"},
        }
        first_alert = {
            "_id": ObjectId(),
            "cnr": "DLHC010088882024",
        }

        coll = MagicMock()
        coll.find.return_value = [already_alerted, needs_alert, first_alert]
        db = MagicMock()
        db.__getitem__.return_value = coll

        with patch("db.mongo._db", return_value=db):
            ids = match_tracking_ids_for_item(item, court_key="dlhc")

        self.assertEqual(
            ids,
            [str(needs_alert["_id"]), str(first_alert["_id"])],
        )

    def test_no_fingerprint_returns_empty(self) -> None:
        item = {
            "identities": {
                "caseType": "WP",
                "rootCaseNumber": "1234",
                "caseYear": "2024",
            }
        }
        coll = MagicMock()
        db = MagicMock()
        db.__getitem__.return_value = coll

        with patch("db.mongo._db", return_value=db):
            ids = match_tracking_ids_for_item(item, court_key="dlhc")

        self.assertEqual(ids, [])
        coll.find.assert_not_called()


class NotifyClientTests(unittest.TestCase):
    def test_http_error_does_not_raise(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CAUSE_LIST_NOTIFY_URL": "https://example.invalid/x",
                "LEX_INTERNAL_SERVICE_TOKEN": "test-internal-token",
            },
        ):
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

    def test_skipped_when_token_empty(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CAUSE_LIST_NOTIFY_URL": "https://example.invalid/x",
                "LEX_INTERNAL_SERVICE_TOKEN": "",
            },
            clear=False,
        ):
            with patch("httpx.post") as post:
                result = post_cause_list_notify(
                    item_id=str(ObjectId()),
                    tracking_ids=[str(ObjectId())],
                )
        self.assertEqual(result["status"], "skipped_no_token")
        post.assert_not_called()

    def test_sends_internal_token_header(self) -> None:
        item_id = str(ObjectId())
        tracking_id = str(ObjectId())
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = ""

        with patch.dict(
            os.environ,
            {
                "CAUSE_LIST_NOTIFY_URL": "https://example.invalid/internal/cause-list-alerts",
                "LEX_INTERNAL_SERVICE_TOKEN": "test-internal-token",
            },
        ):
            with patch("httpx.post", return_value=mock_response) as post:
                result = post_cause_list_notify(
                    item_id=item_id,
                    tracking_ids=[tracking_id],
                )

        self.assertEqual(result["status"], "ok")
        post.assert_called_once()
        kwargs = post.call_args.kwargs
        self.assertEqual(
            kwargs["json"],
            {
                "createdListItems": [
                    {"itemId": item_id, "trackingIds": [tracking_id]}
                ],
                "updatedListItems": [],
            },
        )
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(
            kwargs["headers"]["X-Internal-Token"],
            "test-internal-token",
        )

    def test_batch_separates_created_and_updated(self) -> None:
        created_item = str(ObjectId())
        updated_item = str(ObjectId())
        tid = str(ObjectId())
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = ""
        payload = {
            "createdListItems": [
                {"itemId": created_item, "trackingIds": [tid]}
            ],
            "updatedListItems": [
                {"itemId": updated_item, "trackingIds": [tid]}
            ],
        }
        with patch.dict(
            os.environ,
            {
                "CAUSE_LIST_NOTIFY_URL": "https://example.invalid/internal/cause-list-alerts",
                "LEX_INTERNAL_SERVICE_TOKEN": "test-internal-token",
            },
        ):
            with patch("httpx.post", return_value=mock_response) as post:
                result = post_cause_list_notify_batch(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(post.call_args.kwargs["json"], payload)


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


class NotPublishedGateTests(unittest.TestCase):
    """Empty court lists → not_published (HTTP 200); captcha/CSRF stay failures."""

    def test_empty_bench_is_not_published_error(self) -> None:
        from all_causelist.cloud.handlers import _is_not_published_error
        from all_causelist.delhi import CauseListError

        self.assertTrue(
            _is_not_published_error(
                CauseListError("Bench dropdown was empty for this date.")
            )
        )

    def test_empty_judge_is_not_published_error(self) -> None:
        from all_causelist.cloud.handlers import _is_not_published_error
        from all_causelist.telangana import TelanganaCauseListError

        self.assertTrue(
            _is_not_published_error(
                TelanganaCauseListError("Judge dropdown was empty for this date.")
            )
        )

    def test_coram_table_missing_is_not_published_error(self) -> None:
        from all_causelist.bombay import BombayCauseListError
        from all_causelist.cloud.handlers import _is_not_published_error

        self.assertTrue(
            _is_not_published_error(
                BombayCauseListError("Coram HTML table was missing.")
            )
        )

    def test_captcha_and_csrf_are_not_not_published(self) -> None:
        from all_causelist.cloud.handlers import _is_not_published_error
        from all_causelist.delhi import CauseListError

        self.assertFalse(
            _is_not_published_error(
                CauseListError("Captcha refresh did not return a code.")
            )
        )
        self.assertFalse(
            _is_not_published_error(CauseListError("CSRF token was missing."))
        )
        self.assertFalse(
            _is_not_published_error(
                CauseListError("Validation code was rejected after one refresh.")
            )
        )

    def test_handle_scrape_empty_bench_returns_not_published(self) -> None:
        from all_causelist.cloud import handlers
        from all_causelist.delhi import CauseListError

        with patch.object(handlers, "mark_downloading"):
            with patch.object(
                handlers,
                "_scrape_html_court",
                side_effect=CauseListError(
                    "Bench dropdown was empty for this date."
                ),
            ):
                with patch.object(handlers, "mark_not_published") as mnp:
                    with patch.object(handlers, "mark_failed") as failed:
                        result = handlers.handle_scrape(
                            court="dlhc", date="2026-10-14"
                        )
        self.assertEqual(result["status"], "not_published")
        self.assertEqual(result["court"], "dlhc")
        self.assertEqual(result["date"], "2026-10-14")
        mnp.assert_called_once_with(court="dlhc", date="2026-10-14")
        failed.assert_not_called()

    def test_handle_scrape_captcha_still_raises(self) -> None:
        from all_causelist.cloud import handlers
        from all_causelist.delhi import CauseListError

        with patch.object(handlers, "mark_downloading"):
            with patch.object(
                handlers,
                "_scrape_html_court",
                side_effect=CauseListError(
                    "Captcha refresh did not return a code."
                ),
            ):
                with patch.object(handlers, "mark_not_published") as mnp:
                    with patch.object(handlers, "mark_failed") as failed:
                        with self.assertRaises(CauseListError):
                            handlers.handle_scrape(court="dlhc", date="2026-10-14")
        mnp.assert_not_called()
        failed.assert_called_once()
        self.assertIn("Captcha", failed.call_args.kwargs["error"])


class TrackedItemFilterTests(unittest.TestCase):
    """Step 1: only upsert CauseListItems that match enabled cause-list tracking."""

    def test_identity_key_trio_only(self) -> None:
        import db.mongo as mongo

        self.assertEqual(
            mongo._identity_key(
                {
                    "caseType": "wp",
                    "rootCaseNumber": "1234",
                    "caseYear": "2024",
                }
            ),
            ("WP", "1234", "2024"),
        )
        # diaryNo is ignored for Step 1 match keys
        self.assertEqual(
            mongo._identity_key(
                {
                    "caseType": "WP",
                    "rootCaseNumber": "1234",
                    "caseYear": "2024",
                    "diaryNo": " 99 ",
                }
            ),
            ("WP", "1234", "2024"),
        )
        self.assertIsNone(mongo._identity_key({"caseType": "WP"}))

    def test_filter_keeps_tracked_drops_untracked(self) -> None:
        import db.mongo as mongo

        tracked = {("WP", "1234", "2024")}
        docs = [
            {
                "courtKey": "dlhc",
                "items": [
                    {
                        "fingerprint": "a",
                        "identities": {
                            "caseType": "WP",
                            "rootCaseNumber": "1234",
                            "caseYear": "2024",
                        },
                    },
                    {
                        "fingerprint": "b",
                        "identities": {
                            "caseType": "CRL",
                            "rootCaseNumber": "9",
                            "caseYear": "2020",
                        },
                    },
                ],
            }
        ]
        filtered, items_in, items_kept = mongo.filter_documents_to_tracked(
            docs, court_key="dlhc", tracked_keys=tracked
        )
        self.assertEqual(items_in, 2)
        self.assertEqual(items_kept, 1)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(filtered[0]["items"]), 1)
        self.assertEqual(
            filtered[0]["items"][0]["identities"]["rootCaseNumber"], "1234"
        )

    def test_filter_drops_empty_documents(self) -> None:
        import db.mongo as mongo

        docs = [
            {
                "courtKey": "dlhc",
                "items": [
                    {
                        "fingerprint": "x",
                        "identities": {
                            "caseType": "WP",
                            "rootCaseNumber": "1",
                            "caseYear": "2020",
                        },
                    }
                ],
            }
        ]
        filtered, items_in, items_kept = mongo.filter_documents_to_tracked(
            docs, court_key="dlhc", tracked_keys=set()
        )
        self.assertEqual(items_in, 1)
        self.assertEqual(items_kept, 0)
        self.assertEqual(filtered, [])

    def test_upsert_skips_when_no_tracked_items(self) -> None:
        import db.mongo as mongo

        main = {
            "courtKey": "dlhc",
            "hearingDate": "2026-10-19",
            "documents": [
                {
                    "source": "delhi",
                    "courtKey": "dlhc",
                    "courtLevel": "HC",
                    "hearingDate": "2026-10-19",
                    "listType": "main",
                    "benchLabel": "BENCH",
                    "externalId": "ext-1",
                    "checksum": "abc",
                    "items": [
                        {
                            "fingerprint": "fp1",
                            "identities": {
                                "caseType": "WP",
                                "rootCaseNumber": "1",
                                "caseYear": "2024",
                            },
                        }
                    ],
                }
            ],
        }
        mock_db = MagicMock()
        mock_db.client.admin.command.return_value = {"ok": 1}
        with patch.object(mongo, "mongo_uri", return_value="mongodb://test"):
            with patch.object(mongo, "_db", return_value=mock_db):
                with patch.object(
                    mongo, "load_enabled_cause_list_tracking_keys", return_value=set()
                ):
                    result = mongo.upsert_main_output(main)
        self.assertEqual(result["status"], "skipped_no_tracked_items")
        self.assertEqual(result["items_kept"], 0)
        self.assertEqual(result["items_discarded"], 1)

    def test_upsert_writes_only_tracked_items(self) -> None:
        import db.mongo as mongo

        main = {
            "courtKey": "dlhc",
            "hearingDate": "2026-10-19",
            "documents": [
                {
                    "source": "delhi",
                    "courtKey": "dlhc",
                    "courtLevel": "HC",
                    "hearingDate": "2026-10-19",
                    "listType": "main",
                    "benchLabel": "BENCH",
                    "externalId": "ext-1",
                    "checksum": "abc",
                    "items": [
                        {
                            "fingerprint": "fp-tracked",
                            "identities": {
                                "caseType": "WP",
                                "rootCaseNumber": "1234",
                                "caseYear": "2024",
                            },
                        },
                        {
                            "fingerprint": "fp-other",
                            "identities": {
                                "caseType": "CRL",
                                "rootCaseNumber": "9",
                                "caseYear": "2020",
                            },
                        },
                    ],
                }
            ],
        }
        mock_db = MagicMock()
        mock_docs = MagicMock()
        mock_items = MagicMock()
        mock_db.client.admin.command.return_value = {"ok": 1}
        mock_db.__getitem__.side_effect = lambda name: {
            "causelists": mock_docs,
            "causelistitems": mock_items,
        }[name]
        parent_id = ObjectId()
        upsert_res = MagicMock()
        upsert_res.upserted_id = parent_id
        upsert_res.modified_count = 0
        mock_docs.update_one.return_value = upsert_res
        mock_items.find_one.return_value = None  # create path
        bulk = MagicMock()
        bulk.inserted_count = 1
        bulk.modified_count = 0
        bulk.matched_count = 0
        mock_items.bulk_write.return_value = bulk

        def _find_created(filt, projection=None):
            ids = (filt.get("_id") or {}).get("$in") or []
            return [
                {
                    "_id": oid,
                    "documentId": parent_id,
                    "fingerprint": "fp-tracked",
                    "identities": {
                        "caseType": "WP",
                        "rootCaseNumber": "1234",
                        "caseYear": "2024",
                    },
                }
                for oid in ids
            ]

        mock_items.find.side_effect = _find_created

        with patch.object(mongo, "mongo_uri", return_value="mongodb://test"):
            with patch.object(mongo, "_db", return_value=mock_db):
                with patch.object(
                    mongo,
                    "load_enabled_cause_list_tracking_keys",
                    return_value={("WP", "1234", "2024")},
                ):
                    result = mongo.upsert_main_output(main)

        from pymongo import InsertOne

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["items_in"], 2)
        self.assertEqual(result["items_kept"], 1)
        self.assertEqual(result["items_discarded"], 1)
        self.assertEqual(result["items_inserted"], 1)
        self.assertEqual(len(result["created_items"]), 1)
        mock_docs.update_one.assert_called_once()
        mock_items.find_one.assert_called()
        mock_items.bulk_write.assert_called_once()
        ops = mock_items.bulk_write.call_args[0][0]
        self.assertEqual(len(ops), 1)
        self.assertIsInstance(ops[0], InsertOne)
        self.assertEqual(ops[0]._doc["documentId"], parent_id)
        self.assertIn("_id", ops[0]._doc)

    def test_main_then_supplementary_same_identity_two_parents_two_items(self) -> None:
        """Same case on main then supplementary: two CauseLists + two items; main untouched."""
        import db.mongo as mongo
        from pymongo import InsertOne, UpdateOne

        def _path_get(doc: dict, path: str):
            cur: object = doc
            for part in path.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return None
                cur = cur[part]
            return cur

        def _matches(doc: dict, filt: dict) -> bool:
            for key, val in filt.items():
                got = _path_get(doc, key) if "." in key else doc.get(key)
                if got != val:
                    return False
            return True

        class _FakeColl:
            def __init__(self) -> None:
                self.docs: list[dict] = []

            def update_one(self, filt, update, upsert=False):
                for doc in self.docs:
                    if _matches(doc, filt):
                        before = dict(doc)
                        doc.update(update.get("$set") or {})
                        res = MagicMock()
                        res.upserted_id = None
                        res.modified_count = 1 if doc != before else 0
                        return res
                if not upsert:
                    res = MagicMock()
                    res.upserted_id = None
                    res.modified_count = 0
                    return res
                new_id = ObjectId()
                row: dict = {"_id": new_id}
                row.update(update.get("$set") or {})
                for k, v in (update.get("$setOnInsert") or {}).items():
                    row.setdefault(k, v)
                for k, v in filt.items():
                    if "." not in k:
                        row.setdefault(k, v)
                self.docs.append(row)
                res = MagicMock()
                res.upserted_id = new_id
                res.modified_count = 0
                return res

            def find_one(self, filt, projection=None):
                for doc in self.docs:
                    if _matches(doc, filt):
                        if projection:
                            return {k: doc[k] for k in projection if k in doc}
                        return dict(doc)
                return None

            def find(self, filt, projection=None):
                id_clause = filt.get("_id") if isinstance(filt, dict) else None
                if isinstance(id_clause, dict) and "$in" in id_clause:
                    wanted = set(id_clause["$in"])
                    return [dict(d) for d in self.docs if d["_id"] in wanted]
                return [dict(d) for d in self.docs if _matches(d, filt)]

            def bulk_write(self, ops, ordered=False):
                inserted = 0
                modified = 0
                matched = 0
                for op in ops:
                    if isinstance(op, InsertOne):
                        doc = dict(op._doc)
                        doc.setdefault("_id", ObjectId())
                        self.docs.append(doc)
                        inserted += 1
                        continue
                    if not isinstance(op, UpdateOne):
                        raise TypeError(f"expected InsertOne|UpdateOne, got {type(op)}")
                    if op._upsert:
                        raise AssertionError("item UpdateOne must not use upsert=True")
                    filt = op._filter
                    update = op._doc
                    found = None
                    for doc in self.docs:
                        if _matches(doc, filt):
                            found = doc
                            break
                    if found is None:
                        raise AssertionError("UpdateOne with no match")
                    matched += 1
                    found.update(update.get("$set") or {})
                    modified += 1
                res = MagicMock()
                res.inserted_count = inserted
                res.modified_count = modified
                res.matched_count = matched
                res.upserted_count = 0
                res.upserted_ids = {}
                return res

        fake_docs = _FakeColl()
        fake_items = _FakeColl()

        mock_db = MagicMock()
        mock_db.client.admin.command.return_value = {"ok": 1}
        mock_db.__getitem__.side_effect = lambda name: {
            "causelists": fake_docs,
            "causelistitems": fake_items,
        }[name]

        identities = {
            "caseType": "WP",
            "rootCaseNumber": "1234",
            "caseYear": "2024",
        }
        tracked = {("WP", "1234", "2024")}

        def _board(list_type: str, external_id: str, checksum: str, fp: str) -> dict:
            return {
                "source": "delhi",
                "courtKey": "dlhc",
                "courtLevel": "HC",
                "hearingDate": "2026-10-19",
                "listType": list_type,
                "benchLabel": "BENCH-A",
                "externalId": external_id,
                "checksum": checksum,
                "items": [
                    {
                        "fingerprint": fp,
                        "identities": dict(identities),
                        "listingNo": 1,
                    }
                ],
            }

        main_payload = {
            "courtKey": "dlhc",
            "hearingDate": "2026-10-19",
            "documents": [_board("main", "ext-main", "chk-main", "fp-main")],
        }
        supp_payload = {
            "courtKey": "dlhc",
            "hearingDate": "2026-10-19",
            "documents": [
                _board("supplementary", "ext-supp", "chk-supp", "fp-supp")
            ],
        }
        main_fp_changed = {
            "courtKey": "dlhc",
            "hearingDate": "2026-10-19",
            "documents": [_board("main", "ext-main", "chk-main2", "fp-main-v2")],
        }

        with patch.object(mongo, "mongo_uri", return_value="mongodb://test"):
            with patch.object(mongo, "_db", return_value=mock_db):
                with patch.object(
                    mongo,
                    "load_enabled_cause_list_tracking_keys",
                    return_value=tracked,
                ):
                    r1 = mongo.upsert_main_output(main_payload)
                    main_parents = [dict(d) for d in fake_docs.docs]
                    main_items = [dict(d) for d in fake_items.docs]
                    r2 = mongo.upsert_main_output(supp_payload)
                    # Same main + same fp → skip write
                    r3 = mongo.upsert_main_output(main_payload)
                    # Same main + different fp → UpdateOne (not create)
                    r4 = mongo.upsert_main_output(main_fp_changed)

        self.assertEqual(r1["status"], "ok", r1)
        self.assertEqual(r2["status"], "ok", r2)
        self.assertEqual(r1["documents_upserted"], 1)
        self.assertEqual(r1["items_inserted"], 1)
        self.assertEqual(len(r1["created_items"]), 1)
        self.assertEqual(r2["documents_upserted"], 1)
        self.assertEqual(r2["items_inserted"], 1)
        self.assertEqual(len(r2["created_items"]), 1)
        self.assertEqual(r3["status"], "ok", r3)
        self.assertEqual(r3["items_inserted"], 0)
        self.assertEqual(r3["items_unchanged"], 1)
        self.assertEqual(len(r3["created_items"]), 0)
        self.assertEqual(r4["items_inserted"], 0)
        self.assertEqual(r4["items_modified"], 1)
        self.assertEqual(len(r4["created_items"]), 0)
        self.assertEqual(len(r4["updated_items"]), 1)
        self.assertEqual(r4["updated_items"][0]["fingerprint"], "fp-main-v2")

        # Two CauseList parents: main + supplementary
        self.assertEqual(len(fake_docs.docs), 2)
        list_types = sorted(d["listType"] for d in fake_docs.docs)
        self.assertEqual(list_types, ["main", "supplementary"])
        parent_ids = {d["_id"] for d in fake_docs.docs}
        self.assertEqual(len(parent_ids), 2)

        main_parent = next(d for d in fake_docs.docs if d["listType"] == "main")
        supp_parent = next(
            d for d in fake_docs.docs if d["listType"] == "supplementary"
        )
        self.assertNotEqual(main_parent["_id"], supp_parent["_id"])

        # Two CauseListItems; same identity trio; different documentIds
        self.assertEqual(len(fake_items.docs), 2)
        item_doc_ids = {i["documentId"] for i in fake_items.docs}
        self.assertEqual(item_doc_ids, parent_ids)

        main_item = next(
            i for i in fake_items.docs if i["documentId"] == main_parent["_id"]
        )
        supp_item = next(
            i for i in fake_items.docs if i["documentId"] == supp_parent["_id"]
        )
        self.assertEqual(main_item["identities"], identities)
        self.assertEqual(supp_item["identities"], identities)
        self.assertEqual(main_item["fingerprint"], "fp-main-v2")
        self.assertEqual(supp_item["fingerprint"], "fp-supp")
        self.assertNotEqual(main_item["_id"], supp_item["_id"])
        self.assertEqual(r1["created_items"][0]["_id"], main_item["_id"])
        self.assertEqual(r2["created_items"][0]["_id"], supp_item["_id"])

        # Main parent + item id frozen after supplementary (no replace)
        self.assertEqual(main_parents[0]["_id"], main_parent["_id"])
        self.assertEqual(main_parents[0]["listType"], "main")
        self.assertEqual(len(main_items), 1)
        self.assertEqual(main_items[0]["_id"], main_item["_id"])
        self.assertEqual(main_items[0]["documentId"], main_parent["_id"])
        self.assertEqual(main_items[0]["identities"], identities)

    def test_suppl_created_item_notifies_despite_prior_main_fingerprint(self) -> None:
        """Step 3: new list item is notified even if main already alerted same case."""
        import db.mongo as mongo

        main_fp = _fp(list_type="main")
        supp_fp = _fp(list_type="supplementary")
        self.assertNotEqual(main_fp, supp_fp)

        tracking_row = {
            "_id": ObjectId(),
            "cnr": None,
            "lastFingerprints": {"causeList": main_fp},
        }
        coll = MagicMock()
        coll.find.return_value = [tracking_row]
        db = MagicMock()
        db.__getitem__.return_value = coll

        supp_item = {
            "_id": ObjectId(),
            "documentId": ObjectId(),
            "fingerprint": supp_fp,
            "identities": {
                "caseType": "WP",
                "rootCaseNumber": "1234",
                "caseYear": "2024",
                "diaryNo": None,
            },
        }

        with patch("db.mongo._db", return_value=db):
            ids = mongo.match_tracking_ids_for_item(supp_item, court_key="dlhc")
        self.assertEqual(ids, [str(tracking_row["_id"])])

        with patch("db.mongo._db", return_value=db):
            with patch(
                "all_causelist.cloud.notify.post_cause_list_notify_batch",
                return_value={"status": "ok"},
            ) as post:
                summary = mongo.match_and_notify_items(
                    [supp_item], court_key="dlhc"
                )

        self.assertEqual(summary["matched_items"], 1)
        self.assertEqual(summary["notify_ok"], 1)
        self.assertEqual(summary["updated_pairs"], 1)
        self.assertEqual(summary["created_pairs"], 0)
        post.assert_called_once()
        body = post.call_args.args[0]
        self.assertEqual(body["createdListItems"], [])
        self.assertEqual(
            body["updatedListItems"],
            [
                {
                    "itemId": str(supp_item["_id"]),
                    "trackingIds": [str(tracking_row["_id"])],
                }
            ],
        )

    def test_build_notify_batches_created_vs_updated(self) -> None:
        import db.mongo as mongo

        tid = ObjectId()
        created = {
            "_id": ObjectId(),
            "fingerprint": _fp(list_type="main"),
            "identities": {
                "caseType": "WP",
                "rootCaseNumber": "1234",
                "caseYear": "2024",
            },
        }
        updated = {
            "_id": ObjectId(),
            "fingerprint": _fp(list_type="supplementary"),
            "identities": {
                "caseType": "WP",
                "rootCaseNumber": "1234",
                "caseYear": "2024",
            },
        }
        coll = MagicMock()
        coll.find.return_value = [
            {"_id": tid, "cnr": None, "lastFingerprints": {}},
        ]
        db = MagicMock()
        db.__getitem__.return_value = coll
        with patch("db.mongo._db", return_value=db):
            batches = mongo.build_notify_batches(
                [created], [updated], court_key="dlhc"
            )
        self.assertEqual(
            batches["createdListItems"],
            [{"itemId": str(created["_id"]), "trackingIds": [str(tid)]}],
        )
        self.assertEqual(
            batches["updatedListItems"],
            [{"itemId": str(updated["_id"]), "trackingIds": [str(tid)]}],
        )


class MongoCollectionNamesTests(unittest.TestCase):
    def test_collection_names_default(self) -> None:
        import db.mongo as mongo
        self.assertEqual(mongo.mongo_collection_name(), "causelists")
        self.assertEqual(mongo._CAUSE_LIST, "causelists")
        self.assertEqual(mongo._CAUSE_LIST_ITEM, "causelistitems")
        result = mongo._result("ok")
        self.assertEqual(result["collection"], "causelists+causelistitems")

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
        mock_db.__getitem__.assert_called_with("causelistitems")


if __name__ == "__main__":
    unittest.main()

