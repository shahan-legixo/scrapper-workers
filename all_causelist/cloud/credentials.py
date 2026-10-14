"""Shared GCP credentials (ADC, credentials file, or GCS_CREDENTIALS_JSON)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config


def gcp_credentials() -> tuple[Any | None, str]:
    """Return (credentials_or_None, project_id).

    ``None`` credentials means use Application Default Credentials.
    """
    project = config.gcp_project_id()
    cred_file = config.gcs_credentials_file()
    if cred_file and Path(cred_file).is_file():
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(cred_file)
        project = getattr(credentials, "project_id", None) or project
        return credentials, project

    raw = config.gcs_credentials_json()
    if raw:
        from google.oauth2 import service_account

        if raw.startswith(("'", '"')) and raw.endswith(("'", '"')):
            raw = raw[1:-1]
        info = json.loads(raw)
        credentials = service_account.Credentials.from_service_account_info(info)
        project = info.get("project_id") or project
        return credentials, project

    return None, project
