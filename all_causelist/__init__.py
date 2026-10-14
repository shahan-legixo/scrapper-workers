"""Cause-list scrapers (Delhi, Odisha, Telangana, Bombay, MPHC, Supreme)."""

from .bombay import fetch_bombay_cause_list
from .delhi import fetch_cause_list, fetch_cause_list as fetch_delhi_cause_list
from .mphc import fetch_mphc_cause_list
from .odisha import fetch_odisha_cause_list
from .main_output import write_main_output, write_main_output_from_payload
from .all_schemas.schema import (
    COMMON_CASE_KEYS,
    COURT_KEYS,
    COURT_NAMES,
    make_case,
    make_cause_list,
    make_cause_list_item,
    normalize_list_type,
    normalize_list_type_enum,
    resolve_court_key,
)
from .supreme import fetch_sci_cause_list
from .telangana import fetch_telangana_cause_list

__all__ = [
    "COMMON_CASE_KEYS",
    "COURT_KEYS",
    "COURT_NAMES",
    "fetch_bombay_cause_list",
    "fetch_cause_list",
    "fetch_delhi_cause_list",
    "fetch_mphc_cause_list",
    "fetch_odisha_cause_list",
    "fetch_sci_cause_list",
    "fetch_telangana_cause_list",
    "make_case",
    "make_cause_list",
    "make_cause_list_item",
    "normalize_list_type",
    "normalize_list_type_enum",
    "resolve_court_key",
    "write_main_output",
    "write_main_output_from_payload",
]
