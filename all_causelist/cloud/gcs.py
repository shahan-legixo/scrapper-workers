"""Upload / download cause-list PDFs on GCS.

Locked layout (all courts)::

    gs://BUCKET/<courtKey>/<hearingDate>/<listType>/<file>.pdf

Example::

    gs://causelists-pdfs/bmhc/2026-10-12/main/coram-01.pdf

Re-runs overwrite the same object (no timestamp in the path).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config

_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def _client():
    """Build a Storage client from ADC, credentials file, or GCS_CREDENTIALS_JSON."""
    from google.cloud import storage

    from .credentials import gcp_credentials

    credentials, project = gcp_credentials()
    if credentials is not None:
        return storage.Client(project=project, credentials=credentials)
    return storage.Client(project=project)


def stable_pdf_filename(source_url: str | None = None, filename: str | None = None) -> str:
    """Deterministic object name from court source URL or local filename."""
    if filename and filename.strip():
        name = Path(filename.strip()).name
    elif source_url and source_url.strip():
        path = urlparse(source_url.strip()).path
        name = path.rsplit("/", 1)[-1] if path else ""
    else:
        name = ""
    if not name:
        digest = hashlib.sha256((source_url or "pdf").encode("utf-8")).hexdigest()[:16]
        name = f"{digest}.pdf"
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return _SAFE.sub("-", name).strip("-._") or "file.pdf"


def gcs_object_path(
    *,
    court_key: str,
    hearing_date: str,
    list_type: str,
    source_url: str | None = None,
    filename: str | None = None,
) -> str:
    """Relative object path: courtKey/date/listType/file.pdf"""
    from all_causelist.all_schemas.schema import (
        normalize_list_type_enum,
        resolve_court_key,
    )

    ck = resolve_court_key(court_key)
    lt = normalize_list_type_enum(list_type)
    name = stable_pdf_filename(source_url=source_url, filename=filename)
    return f"{ck}/{hearing_date}/{lt}/{name}"


def gcs_uri_for(
    *,
    court_key: str,
    hearing_date: str,
    list_type: str,
    source_url: str | None = None,
    filename: str | None = None,
    bucket: str | None = None,
) -> str:
    bucket_name = (bucket or config.gcs_bucket()).strip()
    obj = gcs_object_path(
        court_key=court_key,
        hearing_date=hearing_date,
        list_type=list_type,
        source_url=source_url,
        filename=filename,
    )
    return f"gs://{bucket_name}/{obj}"


def parse_gcs_uri(gcs_uri: str) -> tuple[str, str]:
    """Return (bucket, object_path) from gs://bucket/path."""
    text = (gcs_uri or "").strip()
    if not text.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {gcs_uri}")
    rest = text[5:]
    bucket, _, obj = rest.partition("/")
    if not bucket or not obj:
        raise ValueError(f"invalid gs:// URI: {gcs_uri}")
    return bucket, obj


def upload_pdf(
    *,
    local_path: Path | str,
    court_key: str,
    hearing_date: str,
    list_type: str,
    source_url: str | None = None,
    filename: str | None = None,
    content_type: str = "application/pdf",
) -> dict[str, Any]:
    """Upload a local PDF; overwrites the same object on re-run."""
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    data = path.read_bytes()
    return upload_pdf_bytes(
        data=data,
        court_key=court_key,
        hearing_date=hearing_date,
        list_type=list_type,
        source_url=source_url,
        filename=filename or path.name,
        content_type=content_type,
    )


def upload_pdf_bytes(
    *,
    data: bytes,
    court_key: str,
    hearing_date: str,
    list_type: str,
    source_url: str | None = None,
    filename: str | None = None,
    content_type: str = "application/pdf",
) -> dict[str, Any]:
    """Upload PDF bytes to the locked folder layout."""
    bucket_name = config.gcs_bucket()
    obj = gcs_object_path(
        court_key=court_key,
        hearing_date=hearing_date,
        list_type=list_type,
        source_url=source_url,
        filename=filename,
    )
    uri = f"gs://{bucket_name}/{obj}"
    sha = hashlib.sha256(data).hexdigest()

    if config.cloud_dry_run():
        return {
            "status": "dry_run",
            "gcs_uri": uri,
            "bucket": bucket_name,
            "object": obj,
            "size_bytes": len(data),
            "sha256": sha,
            "content_type": content_type,
        }

    client = _client()
    blob = client.bucket(bucket_name).blob(obj)
    blob.upload_from_string(data, content_type=content_type)
    return {
        "status": "uploaded",
        "gcs_uri": uri,
        "bucket": bucket_name,
        "object": obj,
        "size_bytes": len(data),
        "sha256": sha,
        "content_type": content_type,
    }


def download_pdf_bytes(gcs_uri: str) -> bytes:
    """Download PDF bytes from a gs:// URI."""
    if config.cloud_dry_run():
        raise RuntimeError("CLOUD_DRY_RUN=true: cannot download from GCS")
    bucket_name, obj = parse_gcs_uri(gcs_uri)
    client = _client()
    blob = client.bucket(bucket_name).blob(obj)
    return blob.download_as_bytes()


def download_pdf_to_path(gcs_uri: str, dest: Path | str) -> Path:
    """Download a PDF to a local path."""
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(download_pdf_bytes(gcs_uri))
    return out
