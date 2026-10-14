"""Supreme Court cause-list scraper.

Implementation lives in ``all_causelist.supreme``; this package is a thin shim so
old entrypoints (``python -m supreme_cause_list``) keep working.
"""

import sys
from pathlib import Path

_CAUSELISTS_ROOT = Path(__file__).resolve().parents[2]
if str(_CAUSELISTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_CAUSELISTS_ROOT))

from all_causelist.supreme import fetch_sci_cause_list  # noqa: E402

__all__ = ["fetch_sci_cause_list"]
