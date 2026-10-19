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
    """POST { itemId, trackingIds } with X-Internal-Token. Failures logged; never raise."""
    url = notify_url()
    if not url:
        log.info("notify SKIP no CAUSE_LIST_NOTIFY_URL itemId=%s", item_id)
        return {"status": "skipped_no_url", "itemId": item_id}
    if not tracking_ids:
        log.info("notify SKIP no trackingIds itemId=%s", item_id)
        return {"status": "skipped_no_tracking", "itemId": item_id}

    token = notify_internal_token()
    if not token:
        log.info(
            "notify SKIP no LEX_INTERNAL_SERVICE_TOKEN itemId=%s",
            item_id,
        )
        return {"status": "skipped_no_token", "itemId": item_id}

    body = {"itemId": item_id, "trackingIds": list(tracking_ids)}
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Token": token,
    }
    log.info(
        "notify request itemId=%s tracking_count=%s url=%s",
        item_id,
        len(tracking_ids),
        url,
    )
    try:
        response = httpx.post(url, json=body, headers=headers, timeout=20.0)
        if response.status_code >= 400:
            log.warning(
                "notify HTTP %s itemId=%s tracking=%s body=%s",
                response.status_code,
                item_id,
                tracking_ids,
                (response.text or "")[:200],
            )
            return {
                "status": "http_error",
                "itemId": item_id,
                "http_status": response.status_code,
            }
        log.info(
            "notify OK itemId=%s http=%s tracking_count=%s",
            item_id,
            response.status_code,
            len(tracking_ids),
        )
        return {
            "status": "ok",
            "itemId": item_id,
            "http_status": response.status_code,
            "tracking_count": len(tracking_ids),
        }
    except Exception as exc:  # noqa: BLE001 — must not fail scrape
        log.warning(
            "notify failed itemId=%s tracking=%s error=%s",
            item_id,
            tracking_ids,
            exc,
        )
        return {"status": "error", "itemId": item_id, "error": str(exc)}
