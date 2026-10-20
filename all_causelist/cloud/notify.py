"""S2S notify client for cause-list alert hits (scraper → backend)."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("causelist.notify")


def notify_url() -> str | None:
    value = (os.environ.get("CAUSE_LIST_NOTIFY_URL") or "").strip()
    return value or None


def notify_internal_token() -> str | None:
    """Shared secret with backend_legixo ``LEX_INTERNAL_SERVICE_TOKEN``."""
    value = (os.environ.get("LEX_INTERNAL_SERVICE_TOKEN") or "").strip()
    return value or None


def post_cause_list_notify(
    *,
    item_id: str,
    tracking_ids: list[str],
) -> dict[str, Any]:
    """Legacy single-item helper → batch shape (created bucket)."""
    if not tracking_ids:
        log.info("notify SKIP no trackingIds itemId=%s", item_id)
        return {"status": "skipped_no_tracking", "itemId": item_id}
    return post_cause_list_notify_batch(
        {
            "createdListItems": [
                {"itemId": item_id, "trackingIds": list(tracking_ids)}
            ],
            "updatedListItems": [],
        }
    )


def post_cause_list_notify_batch(payload: dict[str, Any]) -> dict[str, Any]:
    """POST created/updated notify pairs. Failures logged; never raise.
    """
    created = list(payload.get("createdListItems") or [])
    updated = list(payload.get("updatedListItems") or [])

    url = notify_url()
    if not url:
        return {
            "status": "skipped_no_url",
            "createdListItems": 0,
            "updatedListItems": 0,
        }
    if not created and not updated:
        log.info("notify SKIP empty createdListItems/updatedListItems")
        return {
            "status": "skipped_no_tracking",
            "createdListItems": 0,
            "updatedListItems": 0,
        }

    token = notify_internal_token()
    if not token:
        log.info(
            "notify SKIP no LEX_INTERNAL_SERVICE_TOKEN "
            "createdListItems=%s updatedListItems=%s",
            len(created),
            len(updated),
        )
        return {
            "status": "skipped_no_token",
            "createdListItems": 0,
            "updatedListItems": 0,
        }

    body = {
        "createdListItems": created,
        "updatedListItems": updated,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Token": token,
    }
    log.info(
        "notify batch request createdListItems=%s updatedListItems=%s url=%s",
        len(created),
        len(updated),
        url,
    )
    try:
        response = httpx.post(url, json=body, headers=headers, timeout=20.0)
        if response.status_code >= 400:
            return {
                "status": "http_error",
                "http_status": response.status_code,
                "createdListItems": len(created),
                "updatedListItems": len(updated),
            }
        return {
            "status": "ok",
            "http_status": response.status_code,
            "createdListItems": len(created),
            "updatedListItems": len(updated),
        }
    except Exception as exc:  # noqa: BLE001 — must not fail scrape
        log.warning(
            "notify batch failed createdListItems=%s "
            "updatedListItems=%s error=%s",
            len(created),
            len(updated),
            exc,
        )
        return {
            "status": "error",
            "error": str(exc),
            "createdListItems": len(created),
            "updatedListItems": len(updated),
        }
