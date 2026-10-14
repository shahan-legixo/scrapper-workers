"""Cloud env config for Cloud Tasks + GCS (asia-south1 by default)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure causelists/.env is loaded when helpers are imported standalone.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from load_env import load_env

    load_env()
except ImportError:
    pass


def gcp_project_id() -> str:
    """Project that owns Cloud Tasks queues / Cloud Run (not necessarily GCS)."""
    return (
        os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("PROJECT_ID")
        or "legixo"
    ).strip()


def gcp_location() -> str:
    return (
        os.environ.get("GCP_LOCATION")
        or os.environ.get("LOCATION")
        or "asia-south1"
    ).strip()


def scrape_queue_name() -> str:
    name = (os.environ.get("SCRAPE_QUEUE") or "cause-list-scrape").strip()
    # Legacy doc names → current GCP queues
    if name in {"scrape-jobs", "dev-causeList-Scraper"}:
        return "cause-list-scrape"
    return name


def parse_queue_name() -> str:
    name = (os.environ.get("PARSE_QUEUE") or "cause-list-parse").strip()
    # Stale shell/env still had parse-jobs; that queue does not exist.
    if name in {"parse-jobs", "parse_jobs"}:
        return "cause-list-parse"
    return name


def scraper_service_url() -> str:
    """Cloud Run URL for Worker 1 (scraper)."""
    return (
        os.environ.get("SCRAPER_SERVICE_URL")
        or os.environ.get("SERVICE_URL")
        or ""
    ).rstrip("/")


def parser_service_url() -> str:
    """Cloud Run URL for Worker 2 (parser). Defaults to scraper URL."""
    return (
        os.environ.get("PARSER_SERVICE_URL")
        or scraper_service_url()
    ).rstrip("/")


def tasks_invoker_sa() -> str:
    return (
        os.environ.get("TASK_SERVICE_ACCOUNT_EMAIL")
        or os.environ.get("CLOUD_TASKS_INVOKER_SA")
        or ""
    ).strip()


def gcs_bucket() -> str:
    return (
        os.environ.get("GCS_BUCKET")
        or os.environ.get("GCS_BUCKET_NAME")
        or "causelists-pdfs"
    ).strip()


def gcs_credentials_json() -> str:
    """Optional inline SA JSON (prefer GOOGLE_APPLICATION_CREDENTIALS file)."""
    return (os.environ.get("GCS_CREDENTIALS_JSON") or "").strip()


def gcs_credentials_file() -> str:
    return (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()


def cloud_dry_run() -> bool:
    """When true, tasks/GCS helpers no-op and return fake names (local)."""
    return os.environ.get("CLOUD_DRY_RUN", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def local_inline_parse() -> bool:
    """After scrape, run parse handlers in-process (no Cloud Tasks delivery)."""
    return os.environ.get("LOCAL_INLINE_PARSE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
