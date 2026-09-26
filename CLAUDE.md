# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

BrowserForge: YAML "recipes" run by Playwright/Chromium, driven from a FastAPI dashboard, a CLI, or a scheduler. Output per run: Excel, screenshots, `run_log.json`. Python 3.13, src-layout, managed with `uv` only (no pip / requirements.txt).

## Commands

```bash
uv sync                                   # install deps
uv run playwright install chromium        # browser, once per machine
uv run browserforge                       # dashboard (http://127.0.0.1:8000; --reload for dev)
uv run browserforge run <recipe> [--data sheet.csv] [--map placeholder=column]   # one recipe, no dashboard

uv run pytest                             # unit tests only (default addopts: -m unit)
uv run pytest -m integration              # real Chromium, manual
uv run pytest -m "unit or integration"    # all
uv run pytest tests/unit/test_runner.py::test_name   # single test
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests   # lint/format/types (mypy strict)
```

Tests are marked by folder (`tests/unit`, `tests/integration`) via `tests/conftest.py`; no per-test decorators. Run the CLI from the project root: recipe/jobs/data paths default to relative `recipes/`, `jobs/`, `data/` (override with `--*-dir` flags or `BROWSERFORGE_*_DIR` env vars). `recipes/` (except examples) and `data/` are git-ignored.

## Architecture

- **`PageDriver` protocol is the seam** (`engine/driver.py`). `PlaywrightDriver` is the *only* module importing playwright. Engine logic (actions, runner, retries, pagination) is tested against `tests/fixtures/fake_driver.py`, so unit tests need no browser. Keep new browser interaction behind `PageDriver`.
- **Pure vs side-effect split**: `table_extract`, `pagination`, `excel_export`, `data_rows`, `form_scan`, `schedule` are pure (plain data in/out). Keep decision logic there.
- **Run flow**: `recipe.py` (YAML → validated `Recipe`/`Step`) → `runner.run_recipe` (executes steps via `actions.py`, one function per step type; retries only transient failures, never a rejected login; failure screenshots; dry-run stops before submit step; writes `run_log.json` + `output.xlsx`) → `bulk.py` runs it once per data row into `row_NNNN/` folders and adds a `Run_Report` sheet.
- **Errors**: `engine/errors.py` classifies failures and gives plain-language messages; raw detail stays in the run log.
- **Web layer**: `app.py` is routes only. Job state lives in `jobs.py` (thread-safe in-memory `JobStore`; each job runs in its own worker thread with its own browser). `results.py` resolves job files safely (path-traversal guarded). UI is a single `templates/dashboard.html`, no framework, server text inserted as plain text only.
- **Scheduling**: `schedule.py` (text → cron, pure), `schedule_store.py` (`data/schedules.json`), `scheduler.py` (APScheduler in-process; only the running dashboard fires schedules). `profiles.py` saves form profiles; `jsonfile.py` does atomic JSON I/O.
- **Secrets**: never persisted. Passwords / `{{env:NAME}}` values / secret-looking columns are redacted from logs and errors, and refused or dropped from saved schedules and profiles; scheduled runs read secrets from the environment at fire time.
- Scraped text is written to Excel as text (no formula injection).

`docs/PLAN.md` is the original build plan. `docs/DEMO.md` is a demo walkthrough.
