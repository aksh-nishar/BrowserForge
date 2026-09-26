# BrowserForge — Browser Automation Assistant
## 2-Day Build Plan (Freelance Demo Product)

## 1. Goal

Build one polished, demo-able tool: a **web dashboard that runs Playwright automation jobs** — login, form fill, file upload/download, screenshot, pagination scraping, Excel export. Not seven separate scripts. One reusable engine + config-driven "recipes" per client site. This is what actually sells: client sees a UI, clicks "Run", gets an Excel file and screenshots — not a terminal.

## 2. Tech Stack

- **Automation**: Playwright (Python, `playwright` + `playwright-stealth` optional)
- **Backend**: FastAPI (serves UI, triggers jobs, streams logs)
- **Frontend**: Single HTML page (Jinja2 template or static + fetch) — no heavy framework needed for demo
- **Excel**: `openpyxl` or `pandas.to_excel`
- **Storage**: local `./jobs/<job_id>/` folder for screenshots, downloads, output.xlsx
- **Job queue**: simple in-process async (no Celery/Redis needed for demo scale)
- **Config**: YAML per "recipe" (site + steps), so new client site = new YAML, not new code

## 3. Architecture

Key design rule for testability: **separate pure logic from Playwright/IO side effects.** Every module that touches a browser, disk, or network gets a thin wrapper; all decision-making, parsing, and data-shaping logic lives in plain functions that take/return plain data (dict, list, dataclass) — no `page` object required to test them.

```
browserforge/
├── pyproject.toml              # single source of truth: deps, tool config (pytest, ruff, mypy), package metadata
├── uv.lock                     # locked dependency versions, committed to repo — reproducible installs
├── .python-version             # pins Python version for uv
├── src/
│   └── browserforge/            # actual importable package (src-layout — prevents accidentally importing from cwd instead of installed package)
│       ├── __init__.py
│       ├── app.py                # FastAPI server (routes only, no logic)
│       └── engine/
│           ├── __init__.py
│           ├── recipe.py           # YAML load + validate + parse -> Recipe dataclass (pure, no IO except file read)
│           ├── runner.py            # orchestrates: takes Recipe + a PageDriver, executes steps in order
│           ├── actions.py            # one function per step type; each takes (driver, step_config) -> ActionResult
│           ├── driver.py              # PageDriver: thin wrapper around Playwright Page (the ONLY module that imports playwright)
│           ├── table_extract.py        # pure: raw HTML/rows (list of list[str]) -> list[dict] (headers as keys)
│           ├── pagination.py            # pure: decide "has next page?" from a passed-in state, loop-control logic separated from the click itself
│           └── excel_export.py          # pure: list[dict] -> openpyxl Workbook object (build-in-memory, testable without writing to disk)
├── recipes/
│   ├── example_login_scrape.yaml
│   └── example_form_fill.yaml
├── templates/
│   └── dashboard.html          # upload recipe / run job / view results
├── jobs/                       # per-run output (screenshots, downloads, xlsx)
├── tests/
│   ├── unit/                    # no browser, no network, runs in <1s total
│   │   ├── test_recipe.py
│   │   ├── test_actions.py       # actions tested against a FakePageDriver (see Testing Strategy)
│   │   ├── test_table_extract.py
│   │   ├── test_pagination.py
│   │   ├── test_excel_export.py
│   │   └── test_runner.py         # runner tested against FakePageDriver + a stub Recipe
│   ├── integration/             # real Playwright, real (local/public test) pages — slower, run separately
│   │   ├── test_driver_playwright.py
│   │   └── test_end_to_end_recipe.py
│   ├── fixtures/
│   │   ├── sample_recipe.yaml
│   │   ├── sample_table.html
│   │   └── fake_driver.py         # FakePageDriver + FakeElement test doubles, shared across unit tests
│   └── conftest.py
└── PLAN.md
```

### Build system: `uv` + `pyproject.toml` (industry-standard, not ad hoc `pip install`)

- **`pyproject.toml`** is the single source of truth — package metadata (`[project]`), runtime deps, and dev-only deps as a dependency group (`[dependency-groups] dev = [...]` — the current PEP 735 / uv-native way; `[project.optional-dependencies]` is the older alternative if a client's environment expects that instead). Tool config (`pytest.ini` settings, `ruff`, `mypy`) also lives here under `[tool.pytest.ini_options]`, `[tool.ruff]`, `[tool.mypy]` — one file instead of five scattered config files.
- **`uv`** replaces `pip`/`venv`/`pip-tools` for everything:
  - `uv init` scaffolds the project + `pyproject.toml`
  - `uv add <pkg>` / `uv add --group dev <pkg>` adds a runtime/dev dependency and updates `pyproject.toml` + `uv.lock` together
  - `uv sync` installs exactly what's locked (reproducible — this is what a client's machine or CI runs)
  - `uv run pytest -m unit` runs commands inside the managed venv without manually activating it
  - `uv lock` regenerates `uv.lock` after manual `pyproject.toml` edits
- **NO `requirements.txt` / `requirements-dev.txt`. Ever.** `uv` manages everything: dependencies, dev tooling, venv, Python version, running tests, lint, type-check, and the app itself. Do not create, export, or hand-write requirements files; do not use `pip`, `python -m venv`, `pip-tools`, or `poetry` anywhere (docs, Dockerfile, CI, README). Client machines/Docker/CI install `uv` and run `uv sync --frozen`.
- **Command cheat-sheet (the only way to do anything in this project):**
  - Add runtime dep: `uv add <pkg>` — Remove: `uv remove <pkg>`
  - Add dev dep: `uv add --group dev <pkg>` — Remove: `uv remove --group dev <pkg>`
  - Upgrade one/all: `uv lock --upgrade-package <pkg>` / `uv lock --upgrade`
  - Install exactly what's locked: `uv sync` (CI/client: `uv sync --frozen`)
  - Run tests: `uv run pytest -m unit` / `uv run pytest -m integration`
  - Lint/format/types: `uv run ruff check .`, `uv run ruff format .`, `uv run mypy src`
  - Run app: `uv run uvicorn browserforge.app:app --reload`
  - Playwright browsers: `uv run playwright install chromium`
  - **uv tools** (`uv tool install` / `uvx`) for standalone CLIs that are NOT project deps and shouldn't pollute the lockfile: one-off runs via `uvx <tool>` (e.g. `uvx playwright codegen <url>` to record clicks when drafting a recipe — Section 9 stretch; `uvx pip-audit` for dependency vuln scans; `uvx ruff` if wanted outside the venv). Persistent install: `uv tool install <tool>`, upgrade `uv tool upgrade <tool>`, remove `uv tool uninstall <tool>`. Rule: anything the code imports or tests need → `uv add`; anything only a human runs from the shell → `uvx`/`uv tool`. Tools used by CI/pre-commit that must match pinned versions (ruff, mypy) stay in the `dev` group and run via `uv run`.
  - Python version management: `uv python install <ver>` / `uv python pin <ver>` (writes `.python-version`); no separate pyenv.
  - Never hand-edit dependency lists or `uv.lock`; always go through `uv add`/`uv remove`/`uv lock`.
- **`src/` layout**: package code lives under `src/browserforge/`, tests import it as an installed package (`from browserforge.engine.recipe import load_recipe`), not via relative path hacks. Prevents the classic bug where tests pass locally (because cwd happens to contain the source) but fail once actually packaged/installed. Install in editable mode for dev: `uv sync` handles this automatically for a project with a `src/` layout + proper `pyproject.toml`.
- **Playwright browsers**: `uv run playwright install chromium` still required once after `uv sync` — `uv` manages the Python package, but Playwright's browser binaries are a separate download step, not a pip dependency. Document this explicitly in the README (Task 2.11) since it's the #1 "works on my machine" trap for a client.

### Why `driver.py` matters most
`PageDriver` is an interface (a Python `Protocol` or ABC) with methods like `goto(url)`, `fill(selector, value)`, `click(selector)`, `screenshot(path)`, `upload(selector, paths)`, `wait_for_download() -> path`, `get_table_rows(selector) -> list[list[str]]`. `engine/actions.py`, `engine/runner.py` and everything above them depend ONLY on this interface, never on `playwright.sync_api.Page` directly. In tests you inject a `FakePageDriver` that records calls and returns canned data — no browser launch, no network, tests run in milliseconds. In production `PlaywrightDriver(driver.py)` implements the same interface for real. This is the one architectural decision that makes "unit test every step" actually possible instead of aspirational.

## 4. Testing Strategy (applies to every task below)

- **Runner**: every test command goes through uv (`uv run pytest ...`); test deps live in the `dev` group (`uv add --group dev pytest ...`). Never call bare `pytest` or `python -m pytest`.
- **Framework**: `pytest` + `pytest-mock` (or plain `unittest.mock`) for unit tests. `pytest-playwright` for the small integration layer.
- **Test doubles**: `tests/fixtures/fake_driver.py` defines `FakePageDriver` — implements the same interface as `PlaywrightDriver` but stores an in-memory DOM-ish state (dict of selector → value/text, list of "clicks", list of "screenshots taken") that assertions read back. Also `FakeElement`/`FakeDownload` as needed.
- **Rule per task**: no task below is "done" until its unit test file exists and passes. Write the test alongside (or just before) the implementation, not after Day 2.
- **Two pytest markers**: `@pytest.mark.unit` (default, run on every save, must stay under ~2s total) and `@pytest.mark.integration` (real Playwright + real test website, run manually or once before demo). `pytest -m unit` for fast loop; `pytest -m integration` before shipping.
- **Coverage target**: every pure function in `recipe.py`, `table_extract.py`, `pagination.py`, `excel_export.py` gets tests for: normal input, empty input, malformed input, boundary (1 row, 0 rows, 1 page, N pages). Every action in `actions.py` gets a test that checks it calls the right `PageDriver` methods with the right arguments AND a test for its failure path (selector not found → raises/returns error result, doesn't crash silently).

## 5. Task Breakdown — Day 1 (Engine Core)

Each task = implement + its own test file green before moving to the next. Estimated order matters: bottom tasks depend on top ones.

**Task 1.0 — Project + build system scaffold**
- `uv init browserforge` (or `uv init` in place), set `src/browserforge/` layout, `.python-version` pinned.
- `uv add fastapi playwright openpyxl pyyaml pandas structlog` (runtime deps) and `uv add --group dev pytest pytest-mock pytest-playwright ruff mypy` (dev-only).
- `uv run playwright install chromium`.
- No requirements files. `pyproject.toml` + `uv.lock` are the only dependency records; commit both.
- Add `[tool.pytest.ini_options]` in `pyproject.toml`: register `unit`/`integration` markers, default to `-m unit` unless overridden, set `testpaths = ["tests"]`.
- Sanity test: `uv run pytest -m unit` runs (0 tests collected is fine, but it must run with no config errors) — this confirms the build system itself works before any feature code exists.

**Task 1.1 — Recipe schema + loader (`engine/recipe.py`)**
- Define `Recipe` dataclass: `url`, `login` (optional), `steps: list[Step]`.
- `load_recipe(path: str) -> Recipe`: reads YAML, validates required fields, raises `RecipeValidationError` with a clear message on bad/missing fields.
- Test alone: no Playwright, no server needed.
- Unit tests (`tests/unit/test_recipe.py`):
  - valid minimal recipe (url + 1 step) parses correctly
  - valid recipe with login block parses correctly
  - missing `url` → raises `RecipeValidationError`
  - unknown step type → raises `RecipeValidationError` with the bad type named in the message
  - empty `steps: []` → parses (0 steps is valid, just does nothing)
  - malformed YAML (syntax error) → raises a clear error, not a raw `yaml.YAMLError` leak

**Task 1.2 — PageDriver interface + FakePageDriver (`engine/driver.py`, `tests/fixtures/fake_driver.py`)**
- Define `PageDriver` Protocol: `goto`, `fill`, `click`, `select_option`, `check`, `upload`, `screenshot`, `wait_for_download`, `get_table_rows`, `wait_for_selector`.
- Build `FakePageDriver` first (test double) — this unblocks all of Task 1.3 onward without ever touching real Playwright.
- Unit tests (`tests/unit/test_fake_driver.py`): confirm the fake itself behaves as scripted (e.g. `fake.set_table("#tbl", rows)` then `fake.get_table_rows("#tbl")` returns those rows) — a test double needs its own sanity test or bugs there silently break every downstream test.

**Task 1.3 — Actions: fill, click, select, check (`engine/actions.py`)**
- One function per action type, e.g. `do_fill(driver, step) -> ActionResult`.
- Unit tests (`tests/unit/test_actions.py`):
  - `do_fill` calls `driver.fill(selector, value)` exactly once with correct args
  - `do_click` calls `driver.click(selector)`
  - `do_select_option` calls with correct value
  - missing `selector` key in step config → raises/returns clear error, no crash
  - `driver.fill` raising a timeout-style exception → action returns `ActionResult(success=False, error=...)`, doesn't propagate raw exception

**Task 1.4 — Login action (`engine/actions.py: do_login`)**
- Fills user/pass selectors, clicks submit, waits for post-login marker (selector or URL substring).
- Unit tests: correct call sequence (fill user → fill pass → click submit → wait); missing creds in recipe → clear validation error; post-login wait timeout → `ActionResult(success=False)` with a message identifying it as a login failure specifically (not a generic timeout), since this is the error a client will see most.

**Task 1.5 — File upload action**
- Unit tests: single file path → `driver.upload(selector, [path])`; multiple file paths list → passed through as list; non-existent local file path → validation error BEFORE calling driver (fail fast, don't let Playwright throw a confusing error).

**Task 1.6 — File download action**
- Unit tests: `do_download` triggers a click then calls `driver.wait_for_download()`, saves returned path into the job's `downloads/` dir (mock filesystem move with `tmp_path` pytest fixture — real temp dir, no mocking needed for this part); download timeout → clear `ActionResult` failure.

**Task 1.7 — Screenshot action**
- Unit tests: filename includes step name + timestamp/index (assert format, not exact time); full-page vs element screenshot chooses right driver call based on step config (`selector` present vs absent).

**Task 1.8 — Table extraction (`engine/table_extract.py`, pure function)**
- `extract_table(raw_rows: list[list[str]], has_header: bool = True) -> list[dict]`
- Pure — no driver needed at all for this test file.
- Unit tests (`tests/unit/test_table_extract.py`):
  - normal table (header + 3 rows) → correct list of dicts
  - 0 data rows (header only) → returns `[]`
  - ragged rows (row shorter than header) → pads with `None` or raises (pick one behavior, test it explicitly)
  - no header (`has_header=False`) → uses positional keys (`col_0`, `col_1`, ...)
  - duplicate header names → de-duplicated keys (`name`, `name_2`) or explicit error — decide + test

**Task 1.9 — Pagination logic (`engine/pagination.py`, pure decision logic + thin driver loop)**
- Pure function: `has_next_page(next_button_state: dict) -> bool` (e.g. checks `disabled`/`visible` flags passed in — no driver call inside the pure function).
- Loop function `paginate_and_collect(driver, step_config)` uses the pure function + `driver.click`/`get_table_rows` in a loop with a max-page safety cap (never infinite-loop a client's site).
- Unit tests:
  - `has_next_page`: enabled+visible → True; disabled → False; absent/None → False
  - `paginate_and_collect` against `FakePageDriver` scripted with 3 pages of data → collects all 3 pages' rows in order, stops at page 3
  - safety cap: `FakePageDriver` scripted to always report "next page available" → loop stops at `max_pages` (config value), test asserts it does NOT hang or exceed the cap

**Task 1.10 — Excel export (`engine/excel_export.py`, pure builder)**
- `build_workbook(sheets: dict[str, list[dict]]) -> openpyxl.Workbook` — returns in-memory workbook, no disk write inside this function (save-to-disk is a separate one-line wrapper, so the interesting logic is testable without filesystem).
- Unit tests (`tests/unit/test_excel_export.py`):
  - single sheet, 3 rows → workbook has 1 sheet, header row bold, 3 data rows correct values
  - multiple sheets (dict with 2 keys) → workbook has 2 named sheets
  - empty data (`[]`) for a sheet → sheet created with no data rows, doesn't crash
  - column autofit logic: longest value in a column → column width reflects it (test the width-calculation helper function directly, not just "did it run")
  - special characters / long strings in cell values → don't crash, get written correctly

**Task 1.11 — Runner orchestration (`engine/runner.py`)**
- `run_recipe(recipe: Recipe, driver: PageDriver, job_dir: str) -> RunLog`
- Executes steps in order, dispatches to the right action function by step type, appends each `ActionResult` to a `RunLog`, writes `run_log.json` at the end.
- Unit tests (`tests/unit/test_runner.py`) — this is the integration point of ALL of Task 1.3–1.10, but still runs against `FakePageDriver`, so still a fast unit test:
  - recipe with 3 mixed steps (fill, click, screenshot) → all 3 `ActionResult`s in log, in order, all `success=True` against a well-scripted fake
  - one step fails (fake configured to raise) → runner records failure in log AND continues or stops per config (`continue_on_error` flag) — test both settings
  - `run_log.json` written to `job_dir` with correct structure (use `tmp_path` fixture, this part touches real disk but not network/browser)
  - unknown step type in the middle of a recipe (shouldn't happen if 1.1 validated it, but test defensively) → runner reports it as a failed step, doesn't crash the whole run

**Task 1.12 — Real Playwright driver (`engine/driver.py: PlaywrightDriver`) + first integration test**
- Implements `PageDriver` for real using `playwright.sync_api`.
- This is the ONLY task in Day 1 that needs a real browser.
- Integration test (`tests/integration/test_driver_playwright.py`, `@pytest.mark.integration`): launch headless browser, `goto` a public test page (e.g. `https://the-internet.herokuapp.com/login`), `fill` + `click`, assert resulting URL/text — confirms the real driver satisfies the same interface the fakes were standing in for.
- End of Day 1 checkpoint: run the FULL recipe engine (Task 1.11) with `PlaywrightDriver` instead of the fake, against Recipe A's site, and get a real `run_log.json` + screenshots + `output.xlsx`. This is your Day 1 demo-to-self.

## 6. Task Breakdown — Day 2 (Dashboard + Resilience + Demo Packaging)

**Task 2.1 — FastAPI scaffold + recipe upload endpoint**
- Routes: `GET /` (dashboard page), `POST /recipes` (upload YAML, saves to `recipes/`), `GET /recipes` (list available).
- Unit tests (`tests/unit/test_app.py`, using FastAPI `TestClient` — no real server process, no browser, still fast): upload valid YAML → 200 + saved; upload invalid YAML → 400 with validation error message from Task 1.1's `RecipeValidationError` surfaced in the response; list endpoint returns recipes present in test `tmp_path` recipes folder.

**Task 2.2 — Job trigger + in-memory job store**
- `POST /jobs {recipe_name}` → creates job id, kicks off run in background task (FastAPI `BackgroundTasks`), returns job id immediately.
- In-memory `JobStore` (dict job_id → status/log) — pure enough to unit test directly: `test_job_store.py` — create job → status "pending"; mark running/complete/failed → status transitions correct; unknown job_id lookup → clear 404-style error, not KeyError leak.
- Route test: `POST /jobs` with a recipe that uses a stubbed/fake runner (dependency-injected) → returns job id, `GET /status/{id}` reflects store state without actually running Playwright in this test.

**Task 2.3 — Status polling + run log endpoint**
- `GET /status/{job_id}` → current step, success/fail counts, done flag.
- Unit tests: mid-run status shape correct; completed job status includes final summary; failed job status includes the failing step + error message (client needs to see WHY it failed, in plain language).

**Task 2.4 — Results endpoint (screenshots, Excel, downloads)**
- `GET /jobs/{id}/results` → lists screenshot file paths, `output.xlsx` path, any downloaded files, all as URLs the dashboard can link to.
- Unit tests: job with known `job_dir` contents (populate a `tmp_path` with dummy files) → endpoint lists exactly those files with correct URLs; job dir missing/job never ran → clear 404, not a crash.

**Task 2.5 — Dashboard HTML/JS (manual test, not unit-tested — UI only)**
- Upload recipe form, run button, poll status every 2s (`fetch` loop), render step log, thumbnail gallery, download links.
- No unit test needed for the HTML/JS itself (out of scope per project conventions — UI correctness is verified by hand in browser per this session's UI-change rule), but every endpoint it calls (2.1–2.4) is already unit tested above, so the JS layer is thin by construction.
- Manual test: open dashboard in real browser, run Recipe A end-to-end via the UI, confirm screenshots + Excel appear and download correctly.

**Task 2.6 — Retry logic (`engine/actions.py` wrapper or `runner.py`)**
- Wrap action execution with configurable retry (`retries`, `backoff_seconds` from recipe or global config).
- Unit tests (`tests/unit/test_retry.py`): `FakePageDriver` scripted to fail twice then succeed → retry wrapper succeeds on 3rd attempt, log shows 2 failed attempts + 1 success; always-failing fake → gives up after configured max retries, final result is failure (not infinite loop — assert call count exactly equals `retries + 1`).

**Task 2.7 — Screenshot-on-failure**
- Any action failure triggers an automatic screenshot attempt (best-effort — don't let the screenshot itself failing mask the original error).
- Unit tests: fake action failure → `driver.screenshot` called once with a `failure_` prefixed name; if the screenshot call ALSO raises (fake configured to do so) → original error is still what's reported, screenshot error is logged as secondary, not swapped in as the primary error.

**Task 2.8 — Error message clarity layer**
- Map raw exception types (timeout, selector not found, navigation failure) to short human messages shown in dashboard, with the raw exception kept in `run_log.json` for debugging.
- Unit tests: each mapped exception type → correct human message; unmapped/unknown exception type → falls back to generic-but-still-not-a-stack-trace message.

**Task 2.9 — Recipe A: login + scrape + paginate + Excel (integration, public test site)**
- Build the YAML recipe, run it against `PlaywrightDriver` for real.
- Integration test (`tests/integration/test_end_to_end_recipe.py`, `@pytest.mark.integration`): run Recipe A start to finish, assert `output.xlsx` exists and has expected row count, assert at least N screenshots exist, assert `run_log.json` shows all steps succeeded.

**Task 2.10 — Recipe B: form fill + upload (integration, public test site)**
- Same pattern as 2.9 for the form-fill/upload flow.

**Task 2.11 — README + demo script**
- Document: uv-only setup (`uv sync`, `uv run playwright install chromium`), how to add a new recipe, how to run `uv run pytest -m unit` vs `uv run pytest -m integration`, how to add/remove libs (`uv add`/`uv remove`), 2-minute client demo walkthrough script. No pip/requirements instructions.
- No test — documentation task.

**Task 2.12 — ~~(Stretch) Dockerfile, cron via APScheduler, email report~~ — SKIPPED (descoped, not built)**
- Decision: out of scope for this version. Nothing in Tasks 1.0–2.11 depends on it; the project ends at 2.11.
- If ever revisited, keep the uv-only rules: Dockerfile installs via uv (`COPY --from=ghcr.io/astral-sh/uv` + `uv sync --frozen --no-dev`), never pip/requirements; new deps (APScheduler etc.) go in via `uv add`; each item gets at least one unit test for its non-IO logic (cron schedule parsing, email body templating).

## 6b. Task Breakdown — Day 3 (Bulk Data, Scheduling, Recipe Generator)

Three gaps found when checking the pitch scenarios (daily price-list scrape, bulk listing updates, users who can't hand-write YAML). Build in this order: 3.1 unlocks the bulk pitch and 3.3 depends on its placeholders. Same rules as before: pure logic separated from IO, unit test alongside, `uv` only.

Scope note: 3.2 reverses the "no cron scheduling" descoping in Task 2.12 / Section 7 for scheduling only. Docker and email reports stay skipped.

**Task 3.1 — Bulk run from a spreadsheet (CSV/XLSX), one recipe run per row**
- Recipe values can hold placeholders: `value: "{{first_name}}"`, also usable in `url`, `login` and `files`. Column headers in the sheet are the placeholder names.
- New pure module `engine/data_rows.py`:
  - `load_rows(path) -> list[dict]` reads CSV (stdlib `csv`) or XLSX (`openpyxl`, already a dep); header row becomes keys; blank rows skipped; values kept as strings.
  - `find_placeholders(recipe) -> set[str]` lists every `{{name}}` a recipe uses.
  - `validate_rows(placeholders, rows) -> list[RowError]` reports rows or columns missing a needed value BEFORE any browser starts.
  - `expand_recipe(recipe, row) -> Recipe` returns a copy with placeholders substituted (pure, no mutation of the original).
- `runner.py`: `run_bulk(recipe, rows, driver_factory, job_dir)` runs each row in its own subfolder (`job_dir/row_001/`), records per-row success/failure, and continues past a failed row. Fresh browser page per row so one bad row can't poison the next.
- Results: extra `Run_Report` sheet in the Excel output (row number, key columns, status, error message, screenshot link). A failed row gets its failure screenshot (Task 2.7).
- API/UI/CLI: `POST /jobs` accepts an optional data file; dashboard gets a file input and a row-progress line ("37 of 150 done, 2 failed"); CLI gets `--data <file>`.
- Column mapping: when sheet headers don't exactly match the recipe's placeholder names, pure `suggest_mapping(placeholders, headers) -> dict` auto-matches (case, spaces, punctuation, close spellings such as "E-mail" vs `email`) and flags anything unmatched. The dashboard shows the suggested mapping and lets the user fix it before the run; the CLI fails with the list of unmatched names unless `--map placeholder=column` is given.
- Secrets: passwords, ID numbers and similar values never get written into `recipes/`, `schedules.json` or saved run files. They come from the data sheet, environment variables (`{{env:NAME}}` placeholder form), or a prompt at run time, and are redacted in logs and `run_log.json` (same rule as Section 10). Scheduled runs use environment variables only.
- Unit tests:
  - `suggest_mapping`: exact match, case/space/punctuation differences, near-miss spelling, ambiguous match flagged not guessed, no match, header used twice
  - secrets: `{{env:NAME}}` resolved from the environment, missing env var gives a clear error before the browser starts, secret values absent from the saved recipe, log and `run_log.json`
  - `load_rows`: CSV and XLSX, blank rows, header only (0 rows), duplicate headers, UTF-8 and special characters
  - `find_placeholders` and `expand_recipe`: substitution in nested fields, missing key raises a clear error, no `{{}}` left over, original recipe unchanged, literal braces that aren't placeholders untouched
  - `validate_rows`: missing column, empty cell, extra columns ignored
  - `run_bulk` against `FakePageDriver`: 3 rows all pass; middle row fails and the others still run; per-row counts correct; row limit/safety cap; report sheet contents
  - route test: `POST /jobs` with a data file, status shows row progress

**Task 3.2 — Scheduled runs**
- `uv add apscheduler`. In-process scheduler started with the FastAPI app (`lifespan`), so it lives and dies with the server. Document that the server must stay running (or use the OS scheduler + CLI as the alternative, in the README).
- New pure module `engine/schedule.py`: `parse_schedule(text) -> Schedule` (accept a cron expression and simple presets like `daily 08:00`, `weekly mon 09:00`; raise `ScheduleValidationError` with a plain message), `next_run_times(schedule, after, n)`, and a `Schedule` dataclass (id, recipe_name, cron, optional data file, enabled, timezone).
- Persistence: schedules stored in `schedules.json` (read/write wrapper separate from the pure logic) so they survive restarts. Scheduled runs go through the same job-start path as the Run button, so logs, screenshots and results behave identically.
- Endpoints: `POST /schedules`, `GET /schedules` (with next run time and last result), `DELETE /schedules/{id}`, pause/resume. Dashboard section listing them.
- Policy: if the server was off at run time, skip the missed run and log it (no catch-up storm); an overlapping run of the same recipe is skipped, not stacked; timezone is explicit (default local).
- Unit tests:
  - `parse_schedule`: valid cron, presets, bad text, out-of-range values, 5 vs 6 field cron
  - `next_run_times`: known dates including month/year boundary and DST
  - store: round-trip save/load, corrupt file handled, unknown id gives clear error
  - scheduler with a fake clock and stub job starter: fires once at the right time, skips overlap, paused schedule doesn't fire, deleted schedule stops firing
  - route tests for all four endpoints

**Task 3.3 — Form-to-recipe generator (paste URL, get a recipe)**
- Depends on 3.1 (generated recipes use `{{placeholders}}` so values come from a form or sheet, never hard-coded).
- `PageDriver` gets one new method, `scan_form(url) -> list[RawField]` (the tag, type, id, name, label text, placeholder, options and required flag of each visible input, select, textarea and button). Implemented in `PlaywrightDriver` and `FakePageDriver`.
- New pure module `engine/form_scan.py`:
  - `build_selector(field) -> str` prefers `#id`, then `[name=...]`, then label/placeholder-based, and refuses to emit a selector that isn't unique
  - `field_to_step(field) -> Step` maps input to `fill`, select to `select_option`, checkbox to `check`, file input to `upload`
  - `fields_to_recipe(url, fields, submit_selector) -> dict` builds the recipe YAML structure with placeholders and a final screenshot step
  - `normalize_label(text) -> placeholder_name` (`"E-mail Address *"` becomes `email_address`, with de-duplication)
- Flow: `POST /scan {url}` returns the detected fields; the dashboard shows a table where the user confirms fields, fixes names, unticks fields to skip, picks the submit button, then either types one set of values or uploads a sheet (3.1); `POST /recipes/generate` saves the recipe.
- Test run: a `dry_run` flag makes the runner skip the final submit click and take a screenshot instead, so the user reviews before the real submit.
- Out of scope for v1: forms behind a login, multi-page wizards, CAPTCHA, fields inside iframes or shadow DOM. Detect and show a clear "not supported yet" message rather than a broken recipe.
- Unit tests (all pure, against canned field lists and a `sample_form.html` fixture):
  - `build_selector`: id wins, name fallback, duplicate ids or names avoided, special characters escaped
  - `field_to_step`: each field type, hidden and disabled fields skipped, unknown type flagged
  - `normalize_label`: punctuation, casing, empty label, duplicates
  - `fields_to_recipe`: output passes `load_recipe` validation (round-trip), placeholders match `find_placeholders`
  - `dry_run`: submit click not issued, screenshot taken
  - route tests for `/scan` (fake driver) and `/recipes/generate`
- Integration test (`@pytest.mark.integration`): scan `the-internet.herokuapp.com/inputs` and generate a recipe that runs.

**Task 3.4 — Step-type audit (do BEFORE 3.3)**
- List every step type `actions.py` supports and compare with what personal-details forms need: text, dropdown, checkbox, file upload, button, and radio buttons (unconfirmed; may be missing), plus date inputs and textareas.
- Add whatever is missing (at least a radio-button choice, e.g. `choose` by selector/value) as a normal action with recipe validation, following the Task 1.3 pattern.
- Unit tests: each new action calls the right `PageDriver` method with the right arguments; missing selector or value gives a clear error; driver failure returns `ActionResult(success=False)`; recipe validation accepts the new type and still rejects unknown ones. 3.3's `field_to_step` maps radio groups to it.

**Task 3.5 — Docs: README examples and demo script refresh**
- Section 8 and Section 7 still say scheduling is "next phase" / out of scope; update both to match 3.2.
- Add a "How BrowserForge helps" section to the README with worked, plain-language examples, each with the problem, the time cost by hand, what the user does in BrowserForge, and the time saved:
  - Example 1: coaching institute registering 150 students on an exam portal from an Excel sheet (bulk run, 3.1)
  - Example 2: a company pulling a supplier price list from a login-gated, 20-page portal every morning (login + paginate + Excel + schedule, 3.2)
  - Example 3: a freelancer or agency updating 300 product listings in a client's admin panel (bulk run, 3.1)
  - Include who it fits (back-office staff, agencies, HR, accountants, e-commerce sellers) and an honest "when it does NOT pay off" note (one-off forms, CAPTCHA/2FA sites, sites that redesign often).
- Add the 3.3 flow (paste URL, confirm fields, upload sheet, test run, real run) with screenshots, and the bulk-from-sheet demo to the demo script (`docs/DEMO.md`).
- Put the pitch structure (pain, number, trust via screenshots and report, live demo with the prospect's own rows, free trial task) in the demo doc.
- No test — documentation task. Manual check: every command in the new sections runs as written.

**Task 3.6 — Saved profiles (required, after 3.3)**
- Let a user save one set of personal details under a name and reuse it across generated recipes, instead of retyping. Stored as a local JSON file, separate from recipes; secret-type fields are excluded and follow the 3.1 secrets rule.
- Pure `apply_profile(placeholders, profile) -> dict` fills the values it can and reports the ones still missing.
- Unit tests: full match, partial match (missing listed), unknown extra keys ignored, secret fields not persisted, profile file round-trip and corrupt-file handling.

## 7. What to SKIP for the 2-day version (cut scope ruthlessly)

- No user auth / multi-tenant dashboard — single-user local tool is fine for demo
- No Celery/Redis job queue — in-process async is enough for demo scale
- No captcha-solving — flag as "requires manual intervention" in recipe, out of scope (also often against ToS)
- No cloud deployment, Dockerfile or email reports (Task 2.12 skipped) — the tool runs locally via `uv run browserforge`; mention hosting and email as "next phase" upsells. In-process scheduling was added later as Task 3.2.
- No visual recipe builder — YAML is fine for v1, mention "next phase: no-code recipe builder" as upsell

## 8. Client-Facing Selling Points (what to say in proposals/demos)

- "Give me your portal's steps once (recipe), I automate it forever — no code changes needed to adjust field values or add rows."
- "Every run produces a screenshot trail + Excel report — you get an audit log, not a black box."
- "Runs on-demand from a button in the dashboard, or on a schedule (daily, weekly, every N minutes) while the dashboard is running. Hosting it always-on and emailing the report are the next phase."
- Target verticals from the brief: HR portals (attendance/payroll download), gov portal form submissions, CRM bulk updates, repetitive data entry, internal dashboard report pulls.

## 9. Stretch Goals (only if Day 2 finishes early)

Descoped (Task 2.12 skipped, not to be built in this version): cron scheduling via `APScheduler`, emailing the Excel report, Dockerfile deploy. Kept as upsell talking points only.

- Simple recipe builder: record clicks via Playwright Codegen, then hand-edit into YAML (mention this as your recipe-creation workflow — fast to build recipes for new clients)

## 10. Logging Strategy

App needs real logging, not `print()`. Two concerns are different and both matter: (a) **operational logs** — what the server/engine is doing internally, for debugging your own code; (b) **the client-facing run log** (`run_log.json`, from Task 1.11) — proof-of-execution artifact. Keep these separate; don't conflate app logging with the job audit trail.

**Library choice: `structlog`** (industry standard for structured logging in modern Python services, widely used — e.g. by companies running FastAPI/Django backends at scale).
- Why over stdlib `logging` alone: stdlib logging gives you plain text lines; `structlog` gives you **structured key-value events** (`logger.info("step_completed", step="login", job_id=job_id, duration_ms=120)`) that render as human-readable text in dev and as JSON in production — critical once you have concurrent jobs and need to filter "show me every log line for job_id=abc123" instead of grepping text.
- Why over `loguru`: `loguru` is simpler/prettier for a single-script tool, but `structlog` integrates cleanly with stdlib `logging` (so FastAPI/Uvicorn/Playwright's own internal logs flow through the same pipeline) and is the more common choice in production codebases. Since this app has a web server + background jobs + multiple modules, `structlog` is the better fit.
- Add via `uv add structlog`.

**Design**:
- One `configure_logging()` call in `app.py` at startup: dev mode → colored console output; prod/demo-recording mode → JSON lines to `logs/app.log` (rotating file handler, e.g. stdlib `RotatingFileHandler` wrapped by structlog).
- Every module gets its own logger: `logger = structlog.get_logger(__name__)`.
- **Job-scoped context**: bind `job_id` (and `recipe_name`) once per job run via `structlog.contextvars.bind_contextvars(job_id=..., recipe=...)` at the top of `run_recipe()` — every log line emitted anywhere during that job automatically carries `job_id` without passing it through every function call. Unbind at job end.
- Log at minimum: job start/end, each step start/result (success/fail + duration), every retry attempt (Task 2.6), every screenshot-on-failure trigger (Task 2.7), every FastAPI request that triggers a job.
- Never log credentials (login passwords from Task 1.4) — redact before logging, even at debug level.

**Testability**: `structlog` supports a test mode (`structlog.testing.capture_logs()` context manager) — use it to assert specific events were logged during a unit test, e.g. Task 1.4's login-failure test can also assert a `login_failed` event was emitted with the right `job_id`, without parsing log strings.

**New task — Task 1.0b (part of scaffold, right after 1.0)**: `uv add structlog`, write `configure_logging()`, wire it into `app.py` startup and into `runner.py`'s job-scoped bind/unbind. Unit test: `test_logging.py` — run a fake job through `run_recipe`, use `capture_logs()`, assert `job_start`, at least one `step_result`, and `job_end` events appear with correct `job_id` bound.

## 11. Day-by-Day Checklist (tracks Section 5/6 tasks 1:1)

**Day 1 — Engine Core**
- [x] 1.0 uv + pyproject.toml scaffold, deps installed via `uv add`, `uv.lock` committed (no requirements*.txt), pytest markers configured
- [x] 1.1 Recipe schema + loader + tests
- [x] 1.2 PageDriver interface + FakePageDriver + tests
- [x] 1.3 Actions: fill/click/select/check + tests
- [x] 1.4 Login action + tests
- [x] 1.5 File upload action + tests
- [x] 1.6 File download action + tests
- [x] 1.7 Screenshot action + tests
- [x] 1.8 Table extraction (pure) + tests
- [x] 1.9 Pagination logic + tests
- [x] 1.10 Excel export (pure) + tests
- [x] 1.11 Runner orchestration + tests
- [x] 1.12 Real PlaywrightDriver + first integration test + full Day-1 checkpoint run

**Day 2 — Dashboard + Resilience + Demo**
- [x] 2.1 FastAPI scaffold + recipe upload endpoint + tests
- [x] 2.2 Job trigger + in-memory job store + tests
- [x] 2.3 Status polling endpoint + tests
- [x] 2.4 Results endpoint + tests
- [x] 2.5 Dashboard HTML/JS + manual browser test
- [x] 2.6 Retry logic + tests
- [x] 2.7 Screenshot-on-failure + tests
- [x] 2.8 Error message clarity layer + tests
- [x] 2.9 Recipe A end-to-end integration test
- [x] 2.10 Recipe B end-to-end integration test
- [x] 2.11 README + demo script
- [x] ~~2.12 (Stretch) Dockerfile, cron scheduling, email report~~ — SKIPPED (descoped; scheduling revived as 3.2)

**Day 3 — Bulk Data, Scheduling, Recipe Generator**
- [x] 3.1 Bulk run from CSV/XLSX (`{{placeholders}}`, per-row runs, Run_Report sheet) + tests
- [x] 3.2 Scheduled runs (APScheduler, `schedules.json`, schedule endpoints + dashboard) + tests
- [x] 3.4 Step-type audit + missing actions (radio buttons etc.) + tests — do before 3.3
- [x] 3.3 Form-to-recipe generator (`scan_form`, `form_scan.py`, dry-run, dashboard flow) + tests
- [x] 3.5 README "How BrowserForge helps" examples, updated demo/pitch doc, fix Section 7/8 scheduling wording
- [x] 3.6 Saved profiles + tests (after 3.3)

## 12. Immediate Next Step

Run Task 1.0: `uv init`, set up `src/browserforge/` layout, add deps via `uv add`, install Playwright browsers, configure pytest markers in `pyproject.toml`. Then create `tests/fixtures/fake_driver.py` and start Task 1.1 (`engine/recipe.py` + `tests/unit/test_recipe.py`). Do not touch Playwright itself until Task 1.12 — everything before it is built and proven against the fake driver.
