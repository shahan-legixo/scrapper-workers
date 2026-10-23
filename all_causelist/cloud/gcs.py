"""Upload / download cause-list PDFs on GCS.

Locked layout (all courts)::

    gs://BUCKET/causelist/<courtKey>/<hearingDate>/<listType>/<edition>/<benchType>/<file>.pdf

Empty ``edition`` / ``benchType`` use ``_``. Example::

    gs://legixo-docs-dev/causelist/bmhc/2026-10-12/daily/_/_/HON_BLE_....pdf

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


def _bmhc_safe_stem(text: str, *, max_len: int = 120) -> str:
    cleaned = _SAFE.sub("_", (text or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned[:max_len]


def bmhc_coram_pdf_filename(
    coram_label: str,
    *,
    source_list: str | None = None,
) -> str:
    """BMHC-only GCS object name from the **coram / bench name** (not Daily_Main).

    Path shape::

        causelist/bmhc/<date>/main/HON_BLE_....pdf
        causelist/bmhc/<date>/weekly/HON_BLE_....__Weekly_Main_-_5.pdf

    Daily main/supp use coram name only (folder separates them). Weekly appends
    the site tag (e.g. ``Weekly Main - 5``) so multiple weeklies per coram do not collide.
    """
    base = _bmhc_safe_stem(coram_label) or "coram"
    src = (source_list or "").strip()
    if src and src.casefold().startswith("weekly"):
        tag = _bmhc_safe_stem(src, max_len=80) or "weekly"
        if tag.lower().endswith(".pdf"):
            tag = tag[:-4]
        name = f"{base}__{tag}"
    else:
        name = base
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return name[:200]


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


def _gcs_folder_seg(value: str | None) -> str:
    """GCS path segment: empty → ``_``; sanitize unsafe chars."""
    raw = (value or "").strip()
    if not raw:
        return "_"
    cleaned = _SAFE.sub("-", raw).strip("-._")
    return cleaned or "_"


def gcs_object_path(
    *,
    court_key: str,
    hearing_date: str,
    list_type: str,
    source_url: str | None = None,
    filename: str | None = None,
    edition: str = "",
    bench_type: str = "",
) -> str:
    """Relative path: causelist/courtKey/date/listType/edition/benchType/file.pdf"""
    from all_causelist.all_schemas.schema import (
        normalize_list_type_enum,
        resolve_court_key,
    )

    ck = resolve_court_key(court_key)
    lt = _gcs_folder_seg(normalize_list_type_enum(list_type))
    ed = _gcs_folder_seg(edition)
    bt = _gcs_folder_seg(bench_type)
    name = stable_pdf_filename(source_url=source_url, filename=filename)
    return f"causelist/{ck}/{hearing_date}/{lt}/{ed}/{bt}/{name}"


def gcs_uri_for(
    *,
    court_key: str,
    hearing_date: str,
    list_type: str,
    source_url: str | None = None,
    filename: str | None = None,
    bucket: str | None = None,
    edition: str = "",
    bench_type: str = "",
) -> str:
    bucket_name = (bucket or config.gcs_bucket()).strip()
    obj = gcs_object_path(
        court_key=court_key,
        hearing_date=hearing_date,
        list_type=list_type,
        source_url=source_url,
        filename=filename,
        edition=edition,
        bench_type=bench_type,
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
    edition: str = "",
    bench_type: str = "",
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
        edition=edition,
        bench_type=bench_type,
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
    edition: str = "",
    bench_type: str = "",
) -> dict[str, Any]:
    """Upload PDF bytes to the locked folder layout."""
    bucket_name = config.gcs_bucket()
    obj = gcs_object_path(
        court_key=court_key,
        hearing_date=hearing_date,
        list_type=list_type,
        source_url=source_url,
        filename=filename,
        edition=edition,
        bench_type=bench_type,
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
