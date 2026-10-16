"""HTTP router for High Court cause-list scrapers.

POST a court code â†’ matching scraper runs â†’ data lands in that court's
``mainOutput.json``, then cases are upserted into one MongoDB collection
(default local ``mongodb://localhost:27017``; see ``.env.example``).

Examples:
  uvicorn server:app --reload
  curl -X POST http://127.0.0.1:8000/scrape -H "Content-Type: application/json" ^
       -d "{\"court\":\"dlhc\",\"date\":\"2026-10-12\"}"
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from load_env import load_env  # noqa: E402

load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
log = logging.getLogger("causelist.server")

from all_causelist.cloud import config as cloud_config  # noqa: E402
from all_causelist.cloud.handlers import handle_parse, handle_scrape  # noqa: E402
from all_causelist.cloud.jobs import ensure_job  # noqa: E402
from all_causelist.cloud.tasks import enqueue_scrape_job  # noqa: E402
from db.mongo import upsert_main_output  # noqa: E402
from run_all import (  # noqa: E402
    run_bombay,
    run_delhi,
    run_mphc,
    run_odisha,
    run_supreme,
    run_telangana,
)

OUTPUT_BASE = PROJECT_ROOT / "output"

# Court-code â†’ scraper runner. Aliases normalize to one canonical key.
Runner = Callable[[str, Path, float], dict[str, Any]]

COURT_ROUTES: dict[str, tuple[str, str, Runner]] = {
    # code: (canonical_key, display_name, runner)
    "dlhc": ("delhi", "Delhi High Court", run_delhi),
    "delhi": ("delhi", "Delhi High Court", run_delhi),
    "bmhc": ("bombay", "Bombay High Court", run_bombay),
    "bmx": ("bombay", "Bombay High Court", run_bombay),
    "bhc": ("bombay", "Bombay High Court", run_bombay),
    "bombay": ("bombay", "Bombay High Court", run_bombay),
    "mphc": ("mphc", "High Court of Madhya Pradesh", run_mphc),
    "mp": ("mphc", "High Court of Madhya Pradesh", run_mphc),
    "tshc": ("telangana", "Telangana High Court", run_telangana),
    "tghc": ("telangana", "Telangana High Court", run_telangana),
    "telangana": ("telangana", "Telangana High Court", run_telangana),
    "orhc": ("odisha", "Orissa High Court", run_odisha),
    "ohc": ("odisha", "Orissa High Court", run_odisha),
    "odisha": ("odisha", "Orissa High Court", run_odisha),
    "orissa": ("odisha", "Orissa High Court", run_odisha),
    "sci": ("supreme", "Supreme Court of India", run_supreme),
    "scic": ("supreme", "Supreme Court of India", run_supreme),
    "supreme": ("supreme", "Supreme Court of India", run_supreme),
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

app = FastAPI(
    title="Cause List Scraper Router",
    description=(
        "Routes POST body court codes (dlhc, bmhc/bmx, mphc, tshc, orhc, sci) "
        "to the matching scraper. Output: mainOutput.json + MongoDB upsert."
    ),
    version="0.2.0",
)


class ScrapeRequest(BaseModel):
    court: str = Field(
        ...,
        description="Court code, e.g. dlhc, bmhc, bmx, mphc, tshc, orhc.",
        examples=["dlhc", "bmx", "mphc", "sci"],
    )
    date: str | None = Field(
        default=None,
        description="Cause-list date YYYY-MM-DD. Defaults to today.",
        examples=["2026-10-12"],
    )
    delay: float = Field(
        default=1.0,
        ge=0.0,
        description="Seconds between requests inside the scraper.",
    )


class ScrapeResponse(BaseModel):
    status: str
    court_code: str
    court: str
    courtName: str
    date: str
    case_count: int | None = None
    main_output: str | None = None
    # ok | skipped_no_uri | skipped_no_file | error: <message>
    mongodb: str = "skipped_no_file"
    mongodb_detail: dict[str, Any] = Field(default_factory=dict)
    detail: dict[str, Any] = Field(default_factory=dict)


class MongoSyncRequest(BaseModel):
    court: str = Field(
        ...,
        description="Court code, same as /scrape (bmhc, dlhc, mphc, tshc, orhc, sci).",
        examples=["bmhc"],
    )
    date: str | None = Field(
        default=None,
        description="Cause-list date YYYY-MM-DD. Defaults to today.",
        examples=["2026-10-12"],
    )


class MongoSyncAllRequest(BaseModel):
    date: str | None = Field(
        default=None,
        description="Cause-list date YYYY-MM-DD. Defaults to today.",
    )


class EnqueueJobRequest(BaseModel):
    court: str = Field(
        ...,
        description="Court code (bmhc, dlhc, mphc, tshc, orhc, sci).",
        examples=["bmhc"],
    )
    date: str | None = Field(
        default=None,
        description="Cause-list date YYYY-MM-DD. Defaults to today.",
        examples=["2026-10-12"],
    )


class ScrapeTaskRequest(BaseModel):
    court: str = Field(..., examples=["bmhc"])
    date: str = Field(..., examples=["2026-10-12"])
    delay: float = Field(default=1.0, ge=0.0)


class ParseTaskRequest(BaseModel):
    job_id: str | None = None
    court: str
    date: str
    list_type: str = "main"
    label: str = ""
    source_url: str = ""
    gcs_uri: str = ""
    local_path: str | None = None
    msb: str | None = None
    filename: str | None = None


def _normalize_court_code(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip()).casefold()
    text = text.replace(" high court", "").replace("-", "").replace("_", "").strip()
    aliases = {
        "delhi high court": "dlhc",
        "delhihc": "dlhc",
        "bombay high court": "bmhc",
        "mumbai": "bmhc",
        "madhya pradesh": "mphc",
        "madhyapradesh": "mphc",
        "telangana high court": "tshc",
        "orissa high court": "orhc",
        "odisha high court": "orhc",
        "supreme court": "sci",
        "supreme court of india": "sci",
        "supremecourt": "sci",
        "sci": "sci",
        "scic": "sci",
    }
    if text in aliases:
        return aliases[text]
    return text


def resolve_route(court_raw: str) -> tuple[str, str, str, Runner]:
    """Return (request_code, canonical_key, display_name, runner)."""
    code = _normalize_court_code(court_raw)
    if code not in COURT_ROUTES:
        known = sorted({k for k in COURT_ROUTES if len(k) <= 5})
        raise HTTPException(
            status_code=400,
            detail=f"Unknown court '{court_raw}'. Use one of: {', '.join(known)}",
        )
    canonical, name, runner = COURT_ROUTES[code]
    return code, canonical, name, runner


def main_output_path(canonical: str, cause_date: str) -> Path:
    """Where each scraper writes mainOutput.json today."""
    if canonical in ("bombay", "odisha", "supreme"):
        return OUTPUT_BASE / canonical / f"causelist-{cause_date}" / "mainOutput.json"
    return OUTPUT_BASE / canonical / "mainOutput.json"


def load_main_output(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def sync_main_output_file(path: Path) -> tuple[str, dict[str, Any]]:
    """Upsert one mainOutput.json into Mongo. Never raises."""
    try:
        doc = load_main_output(path)
    except (OSError, json.JSONDecodeError) as exc:
        return f"error: {exc}", {}
    if doc is None:
        return "skipped_no_file", {}
    result = upsert_main_output(doc)
    return str(result.get("status", "error: unknown")), result


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/courts")
def list_courts() -> dict[str, Any]:
    """List court codes and which scraper they trigger."""
    seen: set[str] = set()
    courts: list[dict[str, str]] = []
    for code, (canonical, name, _) in COURT_ROUTES.items():
        if canonical in seen:
            continue
        seen.add(canonical)
        aliases = sorted(k for k, v in COURT_ROUTES.items() if v[0] == canonical)
        courts.append(
            {
                "court": canonical,
                "courtName": name,
                "codes": ", ".join(aliases),
            }
        )
    return {"courts": courts}


#asyncrous testing
@app.post("/scrape", response_model=ScrapeResponse)
def scrape(body: ScrapeRequest) -> ScrapeResponse:
    """Route by court code, run that scraper, return mainOutput.json path."""
    cause_date = body.date or date.today().isoformat()
    if not _DATE_RE.match(cause_date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    code, canonical, court_name, runner = resolve_route(body.court)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    try:
        result = runner(cause_date, OUTPUT_BASE)
    except Exception as exc:  # noqa: BLE001 â€” surface scraper errors to client
        raise HTTPException(
            status_code=502,
            detail=f"{court_name} scraper failed: {exc}",
        ) from exc

    main_path = Path(result.get("main_output") or main_output_path(canonical, cause_date))
    if not main_path.is_file():
        # Bombay/Odisha write under causelist-<date>/; resolve if runner omitted it.
        fallback = main_output_path(canonical, cause_date)
        if fallback.is_file():
            main_path = fallback

    mongo_status, mongo_detail = sync_main_output_file(main_path)
    return ScrapeResponse(
        status=result.get("status", "SUCCESS"),
        court_code=code,
        court=canonical,
        courtName=court_name,
        date=cause_date,
        case_count=result.get("case_count"),
        main_output=str(main_path) if main_path.is_file() else None,
        mongodb=mongo_status,
        mongodb_detail=mongo_detail,
        detail={
            k: v
            for k, v in result.items()
            if k not in {"status"}
        },
    )



def _validated_day(value: str | None) -> str:
    day = value or date.today().isoformat()
    if not _DATE_RE.match(day):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    return day


# #send tasks to cloud tasks
# @app.post("/jobs")
# def enqueue_job(body: EnqueueJobRequest) -> dict[str, Any]:
#     """Backend producer: enqueue a scrape Cloud Task for court + date.

#     Does not scrape locally. Worker 1 will pick up ``POST /tasks/scrape``.
#     With ``CLOUD_DRY_RUN=true`` the task is only simulated.
#     """
#     day = _validated_day(body.date)
#     code, canonical, court_name, _ = resolve_route(body.court)
#     job = ensure_job(court=code, date=day, status="QUEUED")
#     task = enqueue_scrape_job(court=code, date=day)
#     return {
#         "court_code": code,
#         "court": canonical,
#         "courtName": court_name,
#         "date": day,
#         "job": job,
#         "task": task,
#     }


@app.post("/tasks/scrape")
def tasks_scrape(request: Request, body: ScrapeTaskRequest) -> dict[str, Any]:
    """Worker 1 — Cloud Tasks target for scrape-jobs queue."""
    day = _validated_day(body.date)
    code, _, _, _ = resolve_route(body.court)
    auth = request.headers.get("authorization") or ""
    log.info(
        "HTTP POST /tasks/scrape court=%s date=%s oidc=%s client=%s",
        code,
        day,
        "yes" if auth.lower().startswith("bearer ") else "no",
        request.client.host if request.client else "-",
    )
    try:
        result = handle_scrape(court=code, date=day)
        log.info("HTTP /tasks/scrape OK court=%s status=%s", code, result.get("status"))
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("HTTP /tasks/scrape ERROR court=%s: %s", code, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/tasks/parse")
def tasks_parse(request: Request, body: ParseTaskRequest) -> dict[str, Any]:
    """Worker 2 — Cloud Tasks target for parse-jobs queue."""
    day = _validated_day(body.date)
    code, _, _, _ = resolve_route(body.court)
    auth = request.headers.get("authorization") or ""
    log.info(
        "HTTP POST /tasks/parse court=%s date=%s gcs=%s oidc=%s",
        code,
        day,
        body.gcs_uri or "-",
        "yes" if auth.lower().startswith("bearer ") else "no",
    )
    try:
        result = handle_parse(body.model_dump() | {"court": code, "date": day})
        log.info(
            "HTTP /tasks/parse OK court=%s items=%s",
            code,
            result.get("item_count") or result.get("case_count"),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("HTTP /tasks/parse ERROR court=%s: %s", code, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/mongo/sync")
def mongo_sync(body: MongoSyncRequest) -> dict[str, Any]:
    """Upsert an existing mainOutput.json into Mongo without re-scraping."""
    day = _validated_day(body.date)
    code, canonical, court_name, _ = resolve_route(body.court)
    path = main_output_path(canonical, day)
    status, detail = sync_main_output_file(path)
    if status == "skipped_no_file":
        raise HTTPException(
            status_code=404,
            detail=f"No mainOutput.json for {court_name} on {day} at {path}",
        )
    return {
        "court_code": code,
        "court": canonical,
        "courtName": court_name,
        "date": day,
        "main_output": str(path),
        "mongodb": status,
        "mongodb_detail": detail,
    }


@app.post("/mongo/sync-all")
def mongo_sync_all(body: MongoSyncAllRequest | None = None) -> dict[str, Any]:
    """Upsert every court's mainOutput.json that exists for the date."""
    day = _validated_day(body.date if body else None)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for canonical, name, _ in COURT_ROUTES.values():
        if canonical in seen:
            continue
        seen.add(canonical)
        path = main_output_path(canonical, day)
        status, detail = sync_main_output_file(path)
        results.append(
            {
                "court": canonical,
                "courtName": name,
                "main_output": str(path),
                "mongodb": status,
                "mongodb_detail": detail,
            }
        )
    return {"date": day, "results": results}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
