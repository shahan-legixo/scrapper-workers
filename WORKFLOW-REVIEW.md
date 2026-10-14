# Cause Lists — Workflow Review & Cleanup Plan

**Date:** 2026-10-13  
**Scope:** `causelists/` only  
**Action taken:** Report only. **Nothing was deleted.**

This document reviews the current end-to-end flow and lists what is required vs optional so you can clean up later.

---

## 1. Current workflow (what we use now)

```text
POST /scrape  { court: "dlhc"|"bmx"|"mphc"|..., date }
        │
        ▼
   server.py  (route map → runner)
        │
        ▼
   run_all.py runners  OR  all_causelist/<court>.py
        │
        ├─ HTML scrapers: delhi, telangana, mphc
        └─ PDF scrapers:  bombay, odisha
                │
                └─ pdfLayout/*_pdf_layout.py  (PyMuPDF)
        │
        ▼
   main_output.py  →  mainOutput.json
        │
        ▼
   (MongoDB later — not wired)
```

### Court codes → scrapers

| Code(s) | Scraper | Output |
|---|---|---|
| `dlhc`, `delhi` | Delhi HTML | `output/delhi/mainOutput.json` |
| `bmhc`, `bmx`, `bhc`, `bombay` | Bombay PDF | `output/bombay/causelist-<date>/mainOutput.json` |
| `mphc`, `mp` | Madhya Pradesh HTML | `output/mphc/mainOutput.json` |
| `tshc`, `tghc`, `telangana` | Telangana HTML | `output/telangana/mainOutput.json` |
| `orhc`, `ohc`, `odisha`, `orissa` | Odisha PDF | `output/odisha/causelist-<date>/mainOutput.json` |

### Common case fields in `mainOutput.json`

`date`, `courtName`, `courtNo`, `listingNo`, `caseNumber[]`, `party`, `judge[]`, `list_type`, `section`, `remark`, `advocates[]`

---

## 2. Files required for the current workflow

Keep these. They are on the hot path for `server.py` / scrapers / `mainOutput.json`.

### Root

| File | Role |
|---|---|
| `server.py` | HTTP router (POST `/scrape`, GET `/courts`, `/main-output/...`) |
| `run_all.py` | Shared runners called by `server.py` and CLI batch runs |
| `pyproject.toml` | Dependencies + package metadata |
| `uv.lock` | Locked deps (keep if you use `uv`) |

### Package `all_causelist/`

| File | Role |
|---|---|
| `__init__.py` | Package exports |
| `__main__.py` | CLI: `python -m all_causelist <court\|all>` |
| `schema.py` | `make_case`, common keys, list_type normalize |
| `main_output.py` | Builds / writes `mainOutput.json` |
| `delhi.py` | Delhi HC HTML scraper |
| `telangana.py` | Telangana HC scraper |
| `mphc.py` | Madhya Pradesh HC scraper |
| `bombay.py` | Bombay HC PDF download + wire to layout |
| `odisha.py` | Odisha HC PDF download + wire to layout |
| `pdfLayout/__init__.py` | Layout package exports |
| `pdfLayout/bombay_pdf_layout.py` | Bombay PyMuPDF parser |
| `pdfLayout/odisha_pdf_layout.py` | Odisha PyMuPDF parser |

### Runtime output (generated — keep while testing)

| Path | Role |
|---|---|
| `output/<court>/.../mainOutput.json` | Canonical consistent cases |
| `output/<court>/causelist-*.json` / `parsed.json` / PDFs | Full scrape artifacts |

---

## 3. Not required for current workflow (delete candidates — later)

These are **not** on the main `server.py` → 5 HC scrapers → `mainOutput.json` path.  
**Do not delete yet** unless you confirm you will not use Supreme / Delhi-PDF / PowerShell wrappers.

### Priority A — Safe to remove for *this* HC-only + server workflow

| Path | Why optional now |
|---|---|
| `all_causelist/delhi_pdfs.py` | Stub only (`pdf_count: 0`). Not used by `server.py`. Delhi HTML (`delhi.py`) is the real path. |
| `run_all.ps1` | Thin PowerShell wrapper around `run_all.py`. Redundant if you use `uv run python run_all.py` or `server.py`. |
| `supremeCauseList/` **entire folder** | Separate Supreme Court project. Not in `server.py` route map. Only optional via `run_all.py --include-supreme`. |
| `output/run_summary_*.json` | Batch summary from `run_all.py`; regenerable. |

### Priority B — Keep if you still want CLI batch / optional features

| Path | Notes |
|---|---|
| `run_all.py` `--include-delhi-pdf` / `--include-supreme` branches | Dead weight for server flow, but useful for local batch. Trim later if desired. |
| `__main__.py` `delhi-pdf` / `all --include-supreme` flags | Same as above. |

### Priority C — Never commit / ignore (not “delete product code”)

| Path | Why |
|---|---|
| `.venv/` | Local virtualenv |
| `**/__pycache__/` | Bytecode |
| `supremeCauseList/.venv/` | Nested venv |
| `supremeCauseList/.pytest_cache/` | Test cache |
| `supremeCauseList/.env` | Secrets (if real keys present) |
| `output/**/pdfs/**` | Large downloaded PDFs (regenerable; keep if you need reparse without re-download) |

### Dependency note (not a file delete)

| Item | Note |
|---|---|
| `pypdf` in root `pyproject.toml` | **Not imported** by `all_causelist/` (Bombay/Odisha use `pymupdf`). Only Supreme uses `pypdf`. Can drop from root deps once Supreme is out of this package’s concern. |

---

## 4. Suggested cleanup plan (for you to do later)

Ordered steps. Stop after any step if you still need that feature.

1. **Decide product scope**
   - HC-only via `server.py`? → Supreme + Delhi-PDF are out.
   - Still need Supreme later? → keep `supremeCauseList/`, but do not wire into `server.py` until ready.

2. **Remove stubs / wrappers (low risk)**
   - Delete or archive: `all_causelist/delhi_pdfs.py`
   - Delete or archive: `run_all.ps1`
   - Remove `delhi-pdf` subcommand from `__main__.py` and `run_delhi_pdf` / `--include-delhi-pdf` from `run_all.py`
   - Remove `delhi_pdfs` exports from `__init__.py`

3. **Park Supreme outside this app (medium risk)**
   - Move `supremeCauseList/` to a sibling repo/folder, **or** delete if abandoned
   - Remove `run_supreme` + `--include-supreme` from `run_all.py` / `__main__.py`
   - Drop `pypdf` from root `pyproject.toml` if nothing else needs it

4. **Trim generated noise (safe anytime)**
   - Delete old `output/run_summary_*.json`
   - Optionally prune old `output/**/pdfs` after you are happy with `mainOutput.json`
   - Keep at least one good day’s `mainOutput.json` per court as a sample

5. **Do not remove (core)**
   - `server.py`, `run_all.py` runners for the 5 courts, `schema.py`, `main_output.py`, five scrapers, `pdfLayout/`

---

## 5. Overall process summary

| Stage | What happens |
|---|---|
| **1. Request** | Client `POST /scrape` with court code + date |
| **2. Route** | `server.py` maps `dlhc`→Delhi, `bmx`→Bombay, `mphc`→MPHC, etc. |
| **3. Scrape** | Existing scraper runs unchanged (HTML or PDF+PyMuPDF) |
| **4. Normalize** | `make_case` + `main_output.py` project to common fields |
| **5. Persist (now)** | Write `mainOutput.json` under that court’s `output/` path |
| **6. Persist (later)** | MongoDB hook exists as stub only (`mongodb: not_configured`) |

### What is working

- Five High Court scrapers with shared schema + `advocates[]`
- Bombay PyMuPDF serial/column fix → real per-case rows in `mainOutput.json`
- Router server without changing scraper internals
- Batch runner `run_all.py` still available

### What is not in scope yet

- MongoDB insert of `mainOutput.json` cases
- Real Delhi Drupal PDF scraper (current file is a placeholder)
- Supreme Court as a first-class `server.py` route
- Auth / queue / async job worker for long scrapes

---

## 6. Quick “keep vs drop” cheat sheet

| Keep | Drop later (if HC+server only) |
|---|---|
| `server.py` | `supremeCauseList/` |
| `run_all.py` (5 HC runners) | `delhi_pdfs.py` |
| `all_causelist/{delhi,telangana,mphc,bombay,odisha,schema,main_output}.py` | `run_all.ps1` |
| `all_causelist/pdfLayout/` | Delhi-PDF / Supreme CLI flags |
| `pyproject.toml` (+ `uv.lock`) | Root `pypdf` dep (after Supreme gone) |
| `output/**/mainOutput.json` samples | Old PDFs / run summaries (optional) |

---

*End of report. No files were modified or deleted by this review except creation of this markdown file.*
