# BrowserForge

Browser automation with a dashboard. You describe a job once in a small YAML file (a
**recipe**): log in, fill forms, upload and download files, scrape tables across pages. Then
anyone can run it from a web page and get back **screenshots, an Excel report and a
step-by-step log**.

One reusable engine plus a recipe per site. A new client portal is a new YAML file, not new code.

```
recipe.yaml  ->  [ Run ]  ->  jobs/<id>/
                              |- output.xlsx        scraped tables, one sheet per table step
                              |- screenshots/       a photo trail, plus one when a step fails
                              |- downloads/         files the recipe downloaded
                              `- run_log.json       every step, result, timing and retry
```

## Quick start

Everything is managed with [uv](https://docs.astral.sh/uv/) (dependencies, Python, tests,
running the app). There is no `pip` and no `requirements.txt`.

```
# 1. install uv once (Windows PowerShell; on macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. from the project folder
uv sync                                # installs Python packages exactly as locked
uv run playwright install chromium     # downloads the browser (once per machine)

# 3. start the dashboard
uv run browserforge
```

Open <http://127.0.0.1:8000>.

Options: `--port 9000`, `--recipes-dir`, `--jobs-dir`, `--json-logs` (JSON lines, also written
to `logs/app.log`), `--reload` (restart on code changes). `uv run browserforge --help` lists them.

> **Run it from the project folder.** Relative paths in recipes (such as an upload file) are
> resolved from where you start BrowserForge.

## Using the dashboard

1. **Choose a recipe.** Pick one from the list, or upload your own `.yaml` file. A recipe that
   is not valid is rejected with a plain message saying what to fix.
2. **Click Run.** The status panel updates every 2 seconds: which step is running, a progress
   bar, and a table of finished steps with their timing.
3. **Get the results.** When the run ends you get a download link for the Excel report,
   any downloaded files, the run log, and a gallery of screenshots. If a step fails, its
   error is shown in plain language, and a **failure screenshot** shows the page at that moment.

## Writing a recipe

```yaml
url: https://portal.example.com/login      # opened first
retries: 2                                 # optional: retry slow steps (0-10, default 0)
backoff_seconds: 1                         # optional: wait 1s, then 2s, then 4s between retries
continue_on_error: false                   # optional: true = keep going after a failed step

login:                                     # optional: runs right after the URL opens
  username_selector: "#username"
  password_selector: "#password"
  submit_selector: "button[type=submit]"
  username: alice
  password: s3cret
  success_url_contains: /dashboard         # and/or success_selector: "#welcome"

steps:
  - type: click
    name: open_reports                     # optional: shown in the log and screenshot names
    selector: "a.reports"
  - type: paginate
    name: Invoices                         # becomes the sheet name in output.xlsx
    selector: table.invoices
    next_selector: 'a[aria-label="Next"]'
    max_pages: 10
```

Every step has a `type`, may have a `name`, and may override `retries` / `backoff_seconds`.

| `type` | What it does | Settings |
|---|---|---|
| `goto` | Open a URL | `url` |
| `fill` | Type into a field | `selector`, `value` |
| `click` | Click an element | `selector` |
| `select_option` | Choose from a dropdown | `selector`, `value` |
| `check` | Tick a checkbox | `selector` |
| `login` | Fill credentials, submit, wait for the logged-in page | as in the `login:` block above; optional `timeout_ms` |
| `upload` | Attach file(s) to a file input | `selector`, `files` (one path or a list) |
| `download` | Click something that starts a download, save the file | `selector`, optional `save_as` |
| `screenshot` | Save a PNG (whole page, or one element) | optional `selector`, `full_page` |
| `extract_table` | Read one table into the Excel report | `selector`, optional `has_header` |
| `paginate` | Read a table across pages by clicking "Next" | `selector`, `next_selector`, optional `max_pages` (default 50), `has_header` |
| `wait_for_selector` | Wait until an element appears | `selector`, optional `timeout_ms` |

Good to know:

- **Selectors** are CSS selectors. To find one, open the page, right-click the element,
  *Inspect*, and copy a short, stable selector (an `id` beats a long chain).
- **Retries** apply only to problems that may be temporary (slow pages, elements not there
  yet). Bad recipe settings and **rejected logins are never retried**, so a wrong password
  can't lock an account.
- **`paginate` has a safety cap.** If a site never runs out of pages, `max_pages` stops it. When
  the cap cuts a scrape short, the dashboard shows a warning so nobody mistakes partial data
  for complete data.
- **Missing upload files fail immediately** with the file name, before the browser is touched.
- **Recipes hold passwords in plain text.** Uploaded recipes are stored in `recipes/`, which
  git ignores (only `example_*.yaml` is tracked). There is no login on the dashboard, so keep
  it on your own machine (the default `127.0.0.1`) and do not expose it to a network.

### Example recipes

Both run against public practice websites, so they work with no setup:

- [`recipes/example_login_scrape.yaml`](recipes/example_login_scrape.yaml): log in, scrape a
  paginated table, screenshots, Excel report.
- [`recipes/example_form_fill.yaml`](recipes/example_form_fill.yaml): fill a field, pick a
  dropdown option, tick a checkbox, upload a file.

## Tests

```
uv run pytest                        # unit tests: no browser, no network, seconds
uv run pytest -m integration         # real Chromium against real pages (slower)
uv run pytest -m "unit or integration"   # everything
uv run pytest tests/unit/test_retry.py -v          # one file
uv run pytest -k "login and not integration" -v    # tests matching a keyword
```

- **Unit tests** run the whole engine against a fake browser, so they are fast and exact. They
  are what you run on every change.
- **Integration tests** drive real Chromium: the driver on a local test website, the dashboard
  in a browser, and the two example recipes on public sites. The tests that need the public
  sites **skip themselves when you are offline**.

Quality checks:

```
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy src tests        # types (strict)
```

## Managing dependencies

Only uv. Never edit `uv.lock` by hand.

```
uv add <package>                    # add a runtime dependency
uv add --group dev <package>        # add a dev-only dependency (tests, linting)
uv remove <package>                 # remove one
uv lock --upgrade && uv sync        # upgrade everything within version limits
uv run playwright install chromium  # re-run after upgrading playwright
```

Run one-off tools that are not project dependencies with `uvx`, for example
`uvx playwright codegen https://your-portal.example.com` to record clicks and see the
selectors Playwright would use while you draft a recipe.

## Project layout

```
src/browserforge/
  app.py            FastAPI routes only (recipes, jobs, status, results, files)
  jobs.py           in-memory job store, status JSON, background job body
  results.py        list a job's files as URLs; safe file lookup
  cli.py            `uv run browserforge`
  logging_config.py structlog setup (console or JSON, rotating file)
  templates/dashboard.html   the whole UI
  engine/
    recipe.py       YAML -> validated Recipe
    runner.py       runs steps, retries, failure screenshots, run_log.json, Excel
    actions.py      one function per step type
    driver.py       PageDriver interface + PlaywrightDriver (the ONLY module using playwright)
    errors.py       failure kinds and plain-language messages
    pagination.py   "is there a next page?" + capped loop
    table_extract.py, excel_export.py   pure data shaping
recipes/            example recipes (and your uploads)
tests/unit/         fast tests against a fake driver
tests/integration/  real browser tests
```

The engine only ever talks to the `PageDriver` interface, so all logic is tested against a fake
driver in milliseconds and the real Playwright driver is one small, separately tested layer.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "The browser is not installed on this computer" | `uv run playwright install chromium` |
| "Couldn't find or use '#x' within 30 seconds" | The selector is wrong or the page changed. Check it in the browser's inspector, or add a `wait_for_selector` step before it. |
| "The page address never changed to what was expected" after a login | Wrong credentials, or the site sends you somewhere else. Check `success_url_contains` / `success_selector`. |
| A scrape ends early with a warning | `max_pages` was reached. Raise it. |
| Nothing was saved from a `download` step | The click did not start a download. Check the selector, or that the site does not open the file in a new tab. |
| The Excel report could not be saved | `output.xlsx` is open in Excel. Close it and run again. |

Technical details of any failure are in `jobs/<id>/run_log.json` (raw error, error kind,
attempts, retry history), and in the server log.

## 2-minute client demo

Goal: show that a non-technical person can run a browser job from a button and get evidence.

**Before the call (once):** `uv sync`, `uv run playwright install chromium`, then
`uv run browserforge` and open <http://127.0.0.1:8000>. Do one dry run so the browser is warm.

| Time | Do | Say |
|---|---|---|
| 0:00 | Show the dashboard. Open `recipes/example_login_scrape.yaml` in an editor for 10 seconds. | "This is the whole automation: a short text file. Your portal's steps go here once, and I never touch code to change a field value." |
| 0:20 | Select **example_login_scrape**, click **Run**. | "Your team clicks Run. That's it." |
| 0:30 | Watch the progress bar and the step table fill in. | "Each step is timed and logged as it happens. Nothing is a black box." |
| 0:50 | When it says **Complete**, open the screenshot gallery. | "Here's the photo trail: what the page looked like after login and of the table." |
| 1:05 | Click **Download Excel report**, open it. | "Every page of that table, merged, with a header. Ready for your finance team." |
| 1:25 | Point at the yellow warning about the page limit. | "It's careful: if it ever cuts a scrape short, it tells you rather than silently giving partial data." |
| 1:40 | Upload `example_form_fill`, run it, or show a failed run. | "And when something breaks, you get plain English and a screenshot of the failure, not a stack trace." |
| 1:55 | Close. | "Adding your portal is a new recipe. Scheduled nightly runs and hosting are the next phase." |

**Follow-up phase to offer:** scheduled runs (for example nightly), emailing the report, hosting
on the client's server, and a no-code recipe builder.

## What is not included

This version is deliberately small: a single-user local tool. There is no user login or
multi-user dashboard, no job queue (jobs run in-process, one browser per job), no captcha
solving, and no scheduling, email reports or Docker packaging.
