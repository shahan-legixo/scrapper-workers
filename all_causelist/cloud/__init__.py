"""Cloud Tasks + GCS helpers for the two-stage cause-list pipeline."""

from __future__ import annotations

from typing import Any

__all__ = [
    "handle_parse",
    "handle_scrape",
    "bump_parsed",
    "download_pdf_bytes",
    "download_pdf_to_path",
    "enqueue_parse_job",
    "enqueue_scrape_job",
    "ensure_job",
    "gcs_object_path",
    "gcs_uri_for",
    "get_job",
    "job_id",
    "mark_downloading",
    "mark_failed",
    "mark_not_published",
    "mark_parsing",
    "upload_pdf",
    "upload_pdf_bytes",
]


def __getattr__(name: str) -> Any:
    """Lazy exports so light modules (court_map, notify) import without Cloud Tasks."""
    if name in ("handle_parse", "handle_scrape"):
        from . import handlers

        return getattr(handlers, name)
    if name in ("enqueue_parse_job", "enqueue_scrape_job"):
        from . import tasks

        return getattr(tasks, name)
    if name in (
        "bump_parsed",
        "ensure_job",
        "get_job",
        "job_id",
        "mark_downloading",
        "mark_failed",
        "mark_not_published",
        "mark_parsing",
    ):
        from . import jobs

        return getattr(jobs, name)
    if name in (
        "download_pdf_bytes",
        "download_pdf_to_path",
        "gcs_object_path",
        "gcs_uri_for",
        "upload_pdf",
        "upload_pdf_bytes",
    ):
        from . import gcs

        return getattr(gcs, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
