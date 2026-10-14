"""Shim: SCI scraper now lives in ``all_causelist.supreme``."""

from all_causelist.supreme import SciCauseListError, fetch_sci_cause_list

__all__ = ["SciCauseListError", "fetch_sci_cause_list"]
