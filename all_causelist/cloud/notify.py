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


def post_cause_list_notify(
    *,
    item_id: str,
    tracking_ids: list[str],
) -> dict[str, Any]:
    """POST { itemId, trackingIds }. Failures are logged; never raise."""
    url = notify_url()
    if not url:
        log.info("notify SKIP no CAUSE_LIST_NOTIFY_URL itemId=%s", item_id)
        return {"status": "skipped_no_url", "itemId": item_id}
    if not tracking_ids:
        log.info("notify SKIP no trackingIds itemId=%s", item_id)
        return {"status": "skipped_no_tracking", "itemId": item_id}

    body = {"itemId": item_id, "trackingIds": list(tracking_ids)}
    log.info(
        "notify request itemId=%s tracking_count=%s url=%s",
        item_id,
        len(tracking_ids),
        url,
    )
    try:
        response = httpx.post(url, json=body, timeout=15.0)
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
