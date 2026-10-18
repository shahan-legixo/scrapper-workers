# Cause-list scraper — code overview

High-level map of `scrapper-workers`: what each file does, and how the HC/SC pipeline fits together.

Related deeper docs: [ARCHITECTURE.md](ARCHITECTURE.md), Legixo contract [backend_legixo/hc_sc_scrape_contract_md.md](../backend_legixo/hc_sc_scrape_contract_md.md).

---

## Big picture

```text
POST /scrape (local CLI path)
  → court runner (delhi/bombay/…/supreme)
  → mainOutput.json
  → Mongo upsert (CauseList + CauseListItem)

POST /tasks/scrape  (cloud / contract path)
  → handle_scrape
       HTML courts: scrape → checksum gate → upsert → match → notify
       PDF courts:  download → GCS (skip if same checksum) → enqueue/inline parse
  → handle_parse
       checksum gate → parse PDF → upsert → match → notify
```

**Collections:** `causelists` (one board unit), `causelistsitems` (one listing row).  
**GCS layout:** `gs://BUCKET/causelist/<courtKey>/<date>/<listType>/<file>.pdf`  
**Notify body:** `{ "itemId", "trackingIds" }` → `CAUSE_LIST_NOTIFY_URL`

---



## Entry points (root)


| File                 | Role                                                                                              |
| -------------------- | ------------------------------------------------------------------------------------------------- |
| `server.py`          | FastAPI app: `POST /scrape`, `POST /tasks/scrape`, `POST /tasks/parse`, mongo sync, health, docs. |
|                      |                                                                                                   |
| `load_env.py`        | Loads project `.env` into `os.environ` once.                                                      |
| `pyproject.toml`     | Package metadata + deps (httpx, fastapi, pymongo, pymupdf, GCS, Cloud Tasks).                     |
| `.env.example`       | Local env template (`MONGO_URI`, `MONGO_DB`, `GCS_BUCKET`, notify URL, dry-run flags).            |
| `.env.cloud.example` | Cloud-oriented env sample.                                                                        |
| `Dockerfile`         | Container image for Cloud Run worker.                                                             |
| `ARCHITECTURE.md`    | Detailed scrape architecture per court.                                                           |
| `WORKFLOW-REVIEW.md` | Workflow notes / review.                                                                          |
| `OVERVIEW.md`        | This file.                                                                                        |




### `server.py` — main HTTP surface


| Symbol                                           | Function                                                                       |
| ------------------------------------------------ | ------------------------------------------------------------------------------ |
| `POST /scrape`                                   | Local path: run court runner → write `mainOutput.json` → `upsert_main_output`. |
| `POST /tasks/scrape`                             | Cloud worker 1: `handle_scrape`.                                               |
| `POST /tasks/parse`                              | Cloud worker 2: `handle_parse`.                                                |
| `POST /mongo/sync`                               | Upsert one court’s existing `mainOutput.json`.                                 |
| `POST /mongo/sync-all`                           | Upsert all found mainOutputs.                                                  |
| `GET /health`, `/courts`, `/main-output/{court}` | Health / routing / read file.                                                  |


---



## Package `all_causelist/` — scrapers + schema


| File                  | Role                                                                       |
| --------------------- | -------------------------------------------------------------------------- |
| `__init__.py`         | Package marker.                                                            |
| `__main__.py`         | `python -m all_causelist` CLI entry.                                       |
| `main_output.py`      | Builds Legixo-shaped `mainOutput.json` (`documents[]` + nested `items[]`). |
| `delhi.py`            | Delhi HC HTML scraper (captcha form → benches → tables).                   |
| `mphc.py`             | Madhya Pradesh HC HTML scraper.                                            |
| `telangana.py`        | Telangana HC HTML scraper.                                                 |
| `bombay.py`           | Bombay HC PDF scraper (index → download → layout parse).                   |
| `odisha.py`           | Orissa HC PDF scraper (eCourts index → PDFs).                              |
| `supreme.py`          | Supreme Court glance page → PDF download + parse (active SCI path).        |
| `supreme_schema.py`   | Maps SCI layout rows into common `make_case` fields.                       |




### Typical court-module API (bombay / odisha / supreme)


| Function               | Role                                                    |
| ---------------------- | ------------------------------------------------------- |
| `fetch_*_cause_list`   | Full download + parse + write output (local `/scrape`). |
| `download_*_pdfs`      | Download only (cloud scrape stage).                     |
| `iter_downloaded_pdfs` | Normalize download payload into parse-task items.       |
| `cases_from_pdf`       | Parse one local PDF into flat case rows.                |




### HTML courts (delhi / mphc / telangana)

Fetch HTML, parse tables with BeautifulSoup, emit cases via `make_case`, then `main_output` / upsert.

---



## `all_causelist/all_schemas/` — Legixo shape


| File               | Role                                                            |
| ------------------ | --------------------------------------------------------------- |
| `schema.py`        | Canonical builders, enums, checksum, fingerprint, `externalId`. |
| `sc_schema_map.py` | Re-exports / SCI schema map helpers.                            |
| `__init__.py`      | Public exports.                                                 |




### Important functions in `schema.py`


| Function                                          | Role                                                                       |
| ------------------------------------------------- | -------------------------------------------------------------------------- |
| `resolve_court_key` / `script_key_from_court_key` | `delhi` ↔ `dlhc`, etc.                                                     |
| `normalize_list_type_enum`                        | `advance` / `main` / `supplementary` / …                                   |
| `parse_case_identities`                           | Printed case string → `caseType`, `rootCaseNumber`, `caseYear`, `diaryNo`. |
| `compute_checksum`                                | SHA-256 of PDF bytes (or structured fallback).                             |
| `make_external_id`                                | `courtKey:YYYY-MM-DD:listType:benchLabel`.                                 |
| `compute_fingerprint`                             | Alert material hash (purpose in, party out).                               |
| `make_case`                                       | Flat intermediate row from scrapers.                                       |
| `make_cause_list` / `make_cause_list_item`        | Parent / child Mongo shapes.                                               |
| `flat_case_to_item`                               | Flat row → CauseListItem.                                                  |
| `group_flat_cases_to_documents`                   | Group rows into CauseList units + items.                                   |


---



## `all_causelist/pdfLayout/` — PDF column parsers


| File                    | Role                                              |
| ----------------------- | ------------------------------------------------- |
| `pdf_layout.py`         | Shared PyMuPDF helpers (words → lines → columns). |
| `bombay_pdf_layout.py`  | Bombay PDF geometry → cases.                      |
| `odisha_pdf_layout.py`  | Odisha PDF geometry → cases.                      |
| `supreme_pdf_layout.py` | SCI PDF geometry → cases.                         |


---



## `all_causelist/cloud/` — Cloud Tasks + GCS + contract flow


| File             | Role                                                                           |
| ---------------- | ------------------------------------------------------------------------------ |
| `__init__.py`    | Lazy exports (so light imports don’t need Cloud Tasks).                        |
| `config.py`      | Env helpers: dry-run, queues, service URLs, bucket.                            |
| `credentials.py` | GCP credentials resolution.                                                    |
| `gcs.py`         | Upload/download PDFs; path `causelist/<court>/<date>/<listType>/<file>.pdf`.   |
| `tasks.py`       | Enqueue Cloud Tasks scrape/parse jobs.                                         |
| `jobs.py`        | `scrape_jobs` progress collection (downloading / parsing / complete / failed). |
| `handlers.py`    | Worker logic: `handle_scrape`, `handle_parse`, checksum gates, HTML gate.      |
| `court_map.py`   | `scrape_court_id_from_cnr` — CNR prefix → `dlhc` / `bmhc` / `sci` / …          |
| `notify.py`      | HTTP POST `{ itemId, trackingIds }` to backend (failures don’t fail scrape).   |




### `handlers.py` — key functions


| Function                                    | Role                                                         |
| ------------------------------------------- | ------------------------------------------------------------ |
| `handle_scrape`                             | Route PDF vs HTML court.                                     |
| `_scrape_pdf_court`                         | Download PDFs → GCS (or skip) → enqueue/inline parse.        |
| `_upload_pdf_unless_checksum_unchanged`     | Same unit + same checksum → skip GCS rewrite.                |
| `handle_parse`                              | Load PDF → checksum gate → parse/upsert or rematch → notify. |
| `_scrape_html_court`                        | HTML scrape → mainOutput → checksum gate → notify.           |
| `_upsert_html_documents_with_checksum_gate` | Per-document skip/upsert + rematch.                          |
| `_cases_from_pdf_for_court`                 | Dispatch to bombay/odisha/supreme `cases_from_pdf`.          |


---



## `db/` — Mongo


| File          | Role                                               |
| ------------- | -------------------------------------------------- |
| `mongo.py`    | All Mongo access for cause lists + tracking match. |
| `__init__.py` | Package marker.                                    |




### `mongo.py` — key functions


| Function                      | Role                                                                      |
| ----------------------------- | ------------------------------------------------------------------------- |
| `upsert_main_output`          | Upsert CauseList parents + CauseListItem children.                        |
| `_item_upsert_filter`         | Contract key: `documentId` + trio + `diaryNo` + `listingNo`.              |
| `find_cause_list_unit`        | `findOne` by unit identity.                                               |
| `list_items_for_document`     | Load items for rematch.                                                   |
| `match_tracking_ids_for_item` | Query `CaseAlertTracking` (enabled + causeList + identities + CNR court). |
| `match_and_notify_items`      | Match loop → `post_cause_list_notify`.                                    |


---



## `tests/`


| File                     | Role                                                                                                   |
| ------------------------ | ------------------------------------------------------------------------------------------------------ |
| `test_hc_sc_contract.py` | Unit tests for fingerprint, checksum, upsert key, match, notify, GCS path, upload skip, parse rematch. |


```powershell
uv run python -m unittest tests.test_hc_sc_contract -v
```

---



## `scripts/`


| File                          | Role                                 |
| ----------------------------- | ------------------------------------ |
| `deploy-worker.ps1`           | Deploy Cloud Run worker.             |
| `setup-cloud-tasks.ps1`       | Create/configure Cloud Tasks queues. |
| `probe_gcp_perms.py`          | Probe GCP permissions.               |
| `grant-legixo-cloud-tasks.md` | IAM / grant notes.                   |


---



## Data model cheat-sheet


| Concept            | Meaning                                                                            |
| ------------------ | ---------------------------------------------------------------------------------- |
| **CauseList unit** | One board: `source + courtKey + hearingDate + listType + benchLabel + externalId`. |
| **checksum**       | SHA-256 of PDF bytes; same → skip GCS + skip rewrite; still rematch.               |
| **externalId**     | `courtKey:date:listType:benchLabel` (not download URL).                            |
| **CauseListItem**  | One case row; upsert by identity trio + diary + listingNo.                         |
| **fingerprint**    | Stored on item; **checked on backend** at notify (not scraper).                    |
| **listingSection** | Board section heading (e.g. AFTER NOTICE MISC.).                                   |
| **purpose**        | Listing stage (e.g. EVIDENCE).                                                     |


---



## How to run (quick)

```powershell
cd scrapper-workers
uv sync
uv run uvicorn server:app --reload --host 127.0.0.1 --port 8000

# Local scrape (file + mongo upsert)
POST http://127.0.0.1:8000/scrape
{ "court": "sci", "date": "2026-10-16" }

# Contract / cloud path (GCS + checksum + notify)
POST http://127.0.0.1:8000/tasks/scrape
{ "court": "sci", "date": "2026-10-16" }
# Use LOCAL_INLINE_PARSE=true + CLOUD_DRY_RUN=true for laptop
```

Courts: `dlhc`, `bmhc`, `mphc`, `tshc`, `orhc`, `sci` (aliases exist: `delhi`, `bombay`, `supreme`, …).