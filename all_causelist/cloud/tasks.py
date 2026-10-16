"""Create Cloud Tasks for scrape-jobs and parse-jobs queues.

Push model: Cloud Tasks POSTs JSON to Cloud Run worker URLs.
Idempotent task names: scrape-<courtKey>-<date>, parse-<stable-pdf-key>.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from google.api_core import exceptions as gcp_exceptions
from google.cloud import tasks_v2

from . import config

_TASK_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _client() -> tasks_v2.CloudTasksClient:
    """Cloud Tasks client uses ADC (user/gcloud), not the GCS SA JSON.

    The storage SA is often bucket-scoped and cannot create tasks. Enqueue
    from a principal that has ``cloudtasks.tasks.create`` on the queues.
    """
    return tasks_v2.CloudTasksClient()


def _safe_task_id(raw: str) -> str:
    text = _TASK_NAME_SAFE.sub("-", raw.strip()).strip("-")
    return text[:500] or "task"


def scrape_task_id(court_key: str, hearing_date: str) -> str:
    return _safe_task_id(f"scrape-{court_key}-{hearing_date}")


def parse_task_id(court_key: str, hearing_date: str, pdf_key: str) -> str:
    return _safe_task_id(f"parse-{court_key}-{hearing_date}-{pdf_key}")


def _enqueue(
    *,
    queue_name: str,
    worker_url: str,
    relative_path: str,
    body: dict[str, Any],
    task_id: str,
) -> dict[str, Any]:
    if config.cloud_dry_run():
        return {
            "status": "dry_run",
            "task_id": task_id,
            "queue": queue_name,
            "url": f"{worker_url}{relative_path}",
            "body": body,
        }

    if not worker_url:
        raise ValueError(
            "SERVICE_URL / SCRAPER_SERVICE_URL / PARSER_SERVICE_URL is required "
            "to enqueue Cloud Tasks (or set CLOUD_DRY_RUN=true)"
        )

    import logging

    project = config.gcp_project_id()
    location = config.gcp_location()
    client = _client()
    parent = client.queue_path(project, location, queue_name)
    full_url = f"{worker_url}{relative_path}"
    logging.getLogger("causelist.tasks").info(
        "enqueue queue=%s task_id=%s url=%s project=%s",
        queue_name,
        task_id,
        full_url,
        project,
    )
    headers = {"Content-Type": "application/json"}
    # ngrok free browser interstitial blocks bare POSTs from Cloud Tasks.
    if "ngrok" in worker_url.lower() or "ngrok" in full_url.lower():
        headers["ngrok-skip-browser-warning"] = "1"
    http_request: dict[str, Any] = {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": full_url,
        "headers": headers,
        "body": json.dumps(body).encode("utf-8"),
    }
    invoker = config.tasks_invoker_sa()
    if invoker:
        http_request["oidc_token"] = {
            "service_account_email": invoker,
            "audience": worker_url,
        }

    task: dict[str, Any] = {
        "name": f"{parent}/tasks/{task_id}",
        "http_request": http_request,
    }

    def _create(one_id: str):
        one_task = {
            "name": f"{parent}/tasks/{one_id}",
            "http_request": http_request,
        }
        return client.create_task(request={"parent": parent, "task": one_task})

    try:
        created = _create(task_id)
        status = "created"
    except (gcp_exceptions.AlreadyExists, gcp_exceptions.NotFound):
        # Same name is in the queue, or Cloud Tasks still reserves it after a
        # delete (API returns NOT_FOUND). A new id lets this run dispatch.
        task_id = _safe_task_id(f"{task_id}-{int(time.time())}")
        logging.getLogger("causelist.tasks").warning(
            "task name busy, creating %s instead", task_id
        )
        created = _create(task_id)
        status = "created_fresh"
    return {
        "status": status,
        "task_id": task_id,
        "queue": queue_name,
        "name": created.name,
        "url": full_url,
    }


def enqueue_scrape_job(
    *,
    court: str,
    date: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backend / producer: push one scrape job for a court + date.

    Body example::

        { "court": "bmhc", "date": "2026-10-12" }
    """
    from all_causelist.all_schemas.schema import resolve_court_key

    court_key = resolve_court_key(court)
    body: dict[str, Any] = {"court": court_key, "date": date}
    if extra:
        body.update(extra)
    return _enqueue(
        queue_name=config.scrape_queue_name(),
        worker_url=config.scraper_service_url(),
        relative_path="/tasks/scrape",
        body=body,
        task_id=scrape_task_id(court_key, date),
    )


def enqueue_parse_job(
    *,
    job_id: str,
    court: str,
    date: str,
    list_type: str,
    source_url: str,
    gcs_uri: str,
    label: str = "",
    pdf_key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Worker 1: push one parse job per uploaded PDF."""
    from all_causelist.all_schemas.schema import (
        normalize_list_type_enum,
        resolve_court_key,
    )

    court_key = resolve_court_key(court)
    list_enum = normalize_list_type_enum(list_type)
    key = pdf_key or _pdf_key_from_url(source_url or gcs_uri)
    body: dict[str, Any] = {
        "job_id": job_id,
        "court": court_key,
        "date": date,
        "list_type": list_enum,
        "label": label or "",
        "source_url": source_url,
        "gcs_uri": gcs_uri,
    }
    if extra:
        body.update(extra)
    return _enqueue(
        queue_name=config.parse_queue_name(),
        worker_url=config.parser_service_url(),
        relative_path="/tasks/parse",
        body=body,
        task_id=parse_task_id(court_key, date, key),
    )


def _pdf_key_from_url(url: str) -> str:
    text = (url or "").strip().rstrip("/")
    if not text:
        return "pdf"
    name = text.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return _safe_task_id(stem)[:120] or "pdf"
