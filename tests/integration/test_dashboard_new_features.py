"""The dashboard's new features driven like a person would: create a recipe from a web form,
run it as a test then for real, upload a sheet, save a profile, add a schedule.

A real dashboard server, a real Chromium for the page, and another for every job.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from fixtures.live_server import (
    JOB_TIMEOUT_MS,
    LiveApp,
    register_recipe,
    running_app,
    submissions,
    write_recipe,
)


@pytest.fixture
def app(tmp_path: Path) -> Iterator[LiveApp]:
    with running_app(tmp_path) as live:
        yield live


@pytest.fixture(autouse=True)
def no_script_errors(page: Page) -> Iterator[None]:
    """Any uncaught error in the dashboard's JavaScript fails the test that caused it."""
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield
    assert errors == []


def open_dashboard(page: Page, app: LiveApp) -> None:
    page.goto(app.url)
    expect(page.locator("#recipe-select")).to_be_visible()


# ---- create a recipe from a web form ----


def test_scan_a_form_confirm_the_fields_test_run_then_submit_for_real(
    page: Page, app: LiveApp, site: str, tmp_path: Path
) -> None:
    open_dashboard(page, app)

    # scan the page and see what was found
    page.fill("#scan-url", site + "/register")
    page.click("#scan-btn")
    expect(page.locator("#scan-fields-body tr")).to_have_count(8, timeout=30_000)
    expect(page.locator("#scan-message")).to_contain_text("8 field(s) found")
    expect(page.locator("#scan-skipped")).to_contain_text("Nickname")
    expect(page.locator("#submit-select")).to_have_value("#send")
    expect(page.locator("#scan-notes .notice")).to_have_count(0)

    # leave the notes field out, then save the recipe
    notes_row = page.locator("#scan-fields-body tr", has_text="Notes")
    notes_row.locator("input[type=checkbox]").uncheck()
    page.fill("#generated-name", "registration")
    page.click("#save-generated-btn")
    expect(page.locator("#scan-message")).to_contain_text("Saved recipe 'registration'")
    expect(page.locator("#recipe-select")).to_have_value("registration")

    # the data card asks for the values; a test run is pre-selected
    expect(page.locator("#data-card")).to_be_visible()
    expect(page.locator("#dry-run")).to_be_checked()
    expect(page.locator("#typed-fields input")).to_have_count(6)  # notes was left out
    cv = tmp_path / "cv.txt"
    cv.write_text("hello cv", encoding="utf-8")
    for key, value in {
        "first_name": "Ann",
        "e_mail": "ann@example.com",
        "date_of_birth": "1990-05-17",
        "country": "IN",
        "plan": "pro",
        "cv": str(cv),
    }.items():
        page.fill(f"#value-{key}", value)

    # test run: fills the form, submits nothing
    before = len(submissions(site))
    page.click("#run-btn")
    expect(page.locator("#status-badge")).to_have_text("Complete", timeout=JOB_TIMEOUT_MS)
    expect(page.locator("#dry-run-note")).to_be_visible()
    assert len(submissions(site)) == before

    # the real run submits
    page.uncheck("#dry-run")
    page.click("#run-btn")
    expect(page.locator("#dry-run-note")).to_be_hidden()
    expect(page.locator("#status-badge")).to_have_text("Complete", timeout=JOB_TIMEOUT_MS)
    received = submissions(site)
    assert len(received) == before + 1
    assert received[-1]["first_name"] == "Ann"
    assert received[-1]["plan"] == "pro"
    assert received[-1]["cv"] == "[file cv.txt: hello cv]"
    assert "notes" not in received[-1] or received[-1]["notes"] == ""
    expect(page.locator("#gallery img").first).to_be_visible()


def test_scanning_an_unreachable_page_says_so_in_plain_words(page: Page, app: LiveApp) -> None:
    open_dashboard(page, app)
    page.fill("#scan-url", "http://127.0.0.1:1/")  # nothing listens here
    page.click("#scan-btn")
    expect(page.locator("#scan-message")).to_contain_text("Could not open the page", timeout=30_000)
    expect(page.locator("#scan-result")).to_be_hidden()


def test_a_page_with_a_captcha_shows_a_warning(page: Page, app: LiveApp, site: str) -> None:
    open_dashboard(page, app)
    page.fill("#scan-url", site + "/register-captcha")
    page.click("#scan-btn")
    expect(page.locator("#scan-notes .notice")).to_have_count(2, timeout=30_000)
    expect(page.locator("#scan-notes")).to_contain_text("CAPTCHA")


def test_a_bare_address_is_tried_as_https(page: Page, app: LiveApp, site: str) -> None:
    open_dashboard(page, app)
    page.fill("#scan-url", "  " + site.removeprefix("http://") + "/register-late")
    page.click("#scan-btn")
    # the local site is plain http, so the guessed https:// fails: the message must be clear
    expect(page.locator("#scan-message")).not_to_have_text("Opening the page...", timeout=30_000)
    expect(page.locator("#scan-message")).to_contain_text("Could not open the page")


# ---- bulk from a sheet ----


def test_uploading_a_sheet_shows_the_column_mapping_and_runs_every_row(
    page: Page, app: LiveApp, site: str, tmp_path: Path
) -> None:
    write_recipe(app.recipes, "register", register_recipe(site))
    sheet = tmp_path / "people.csv"
    sheet.write_text(
        "First Name,E-mail,plan\nAnn,ann@example.com,basic\nBob,bob@example.com,pro\n",
        encoding="utf-8",
    )
    open_dashboard(page, app)
    page.select_option("#recipe-select", "register")
    expect(page.locator("#data-card")).to_be_visible()
    expect(page.locator("#data-help")).to_have_text(
        "This recipe fills in {{email}}, {{first_name}}, {{plan}}."
    )

    page.select_option("#data-mode", "sheet")
    page.set_input_files("#data-file", str(sheet))
    expect(page.locator("#preview-summary")).to_contain_text("2 row(s) found")
    expect(page.locator("#mapping-body tr")).to_have_count(3)
    expect(page.locator("#mapping-body select").nth(0)).to_have_value("E-mail")  # email
    expect(page.locator("#mapping-body select").nth(1)).to_have_value("First Name")  # first_name
    expect(page.locator("#mapping-body select").nth(2)).to_have_value("plan")
    expect(page.locator("#preview-problems")).to_be_hidden()

    before = len(submissions(site))
    page.click("#run-btn")
    expect(page.locator("#status-badge")).to_have_text("Complete", timeout=JOB_TIMEOUT_MS)
    expect(page.locator("#progress-text")).to_contain_text("2 of 2 rows done, 0 failed")
    expect(page.locator("#steps-wrap")).to_be_hidden()
    assert [s["first_name"] for s in submissions(site)[before:]] == ["Ann", "Bob"]
    expect(page.locator("#downloads")).to_contain_text("Download Excel report")


def test_a_sheet_with_a_problem_is_flagged_before_anything_runs(
    page: Page, app: LiveApp, site: str, tmp_path: Path
) -> None:
    write_recipe(app.recipes, "register", register_recipe(site))
    sheet = tmp_path / "people.csv"
    sheet.write_text(
        "first_name,email,plan\nAnn,ann@example.com,basic\nBob,,pro\n", encoding="utf-8"
    )
    open_dashboard(page, app)
    page.select_option("#recipe-select", "register")
    page.select_option("#data-mode", "sheet")
    page.set_input_files("#data-file", str(sheet))
    expect(page.locator("#preview-problems")).to_be_visible()
    expect(page.locator("#preview-problems")).to_contain_text("Row 2: no value for 'email'")

    before = len(submissions(site))
    page.click("#run-btn")
    expect(page.locator("#message")).to_contain_text("Row 2: no value for 'email'")
    assert len(submissions(site)) == before  # nothing was started


def test_a_failing_row_is_listed_in_a_table_with_its_reason(
    page: Page, app: LiveApp, site: str, tmp_path: Path
) -> None:
    write_recipe(app.recipes, "register", register_recipe(site))
    sheet = tmp_path / "people.csv"
    sheet.write_text(
        "first_name,email,plan\nAnn,ann@example.com,basic\nBob,bob@example.com,gold\n",
        encoding="utf-8",
    )
    open_dashboard(page, app)
    page.select_option("#recipe-select", "register")
    page.select_option("#data-mode", "sheet")
    page.set_input_files("#data-file", str(sheet))
    page.click("#run-btn")

    expect(page.locator("#status-badge")).to_have_text("Failed", timeout=JOB_TIMEOUT_MS)
    expect(page.locator("#progress-text")).to_contain_text("2 of 2 rows done, 1 failed")
    expect(page.locator("#failed-rows-body tr")).to_have_count(1)
    expect(page.locator("#failed-rows-body tr")).to_contain_text("Bob")
    expect(page.locator("#failed-rows-body tr")).to_contain_text("plan")
    expect(page.locator("#message")).to_contain_text("some rows failed")
    expect(page.locator("#gallery figcaption.failure")).to_have_count(1)


# ---- typed values, secrets and profiles ----


def test_secret_looking_fields_are_masked_and_profiles_fill_the_rest(
    page: Page, app: LiveApp, site: str
) -> None:
    write_recipe(
        app.recipes,
        "login_like",
        f"""
url: {site}/register
steps:
  - type: fill
    selector: "#first_name"
    value: "{{{{first_name}}}}"
  - type: fill
    selector: "#email"
    value: "{{{{email}}}}"
  - type: fill
    selector: "#notes"
    value: "{{{{password}}}}"
""",
    )
    open_dashboard(page, app)
    page.select_option("#recipe-select", "login_like")
    expect(page.locator("#value-password")).to_have_attribute("type", "password")
    expect(page.locator("#value-first_name")).to_have_attribute("type", "text")

    # save a profile: the password is not kept
    page.fill("#value-first_name", "Ann")
    page.fill("#value-email", "ann@example.com")
    page.fill("#value-password", "hunter22")
    page.fill("#profile-name", "Ann")
    page.click("#profile-save-btn")
    expect(page.locator("#profile-message")).to_contain_text("Saved profile 'Ann' (2 field(s))")
    expect(page.locator("#profile-message")).to_contain_text("password")
    expect(page.locator("#profile-select")).to_have_value("Ann")
    assert "hunter22" not in (app.data / "profiles.json").read_text(encoding="utf-8")

    # a fresh page load: choose the profile and the fields fill in, except the secret one
    page.reload()
    page.select_option("#recipe-select", "login_like")
    page.select_option("#profile-select", "Ann")
    page.click("#profile-load-btn")
    expect(page.locator("#value-first_name")).to_have_value("Ann")
    expect(page.locator("#value-email")).to_have_value("ann@example.com")
    expect(page.locator("#value-password")).to_have_value("")
    expect(page.locator("#profile-message")).to_contain_text("Still to fill in yourself: password")

    page.click("#profile-delete-btn")
    expect(page.locator("#profile-message")).to_contain_text("Deleted profile 'Ann'")
    expect(page.locator("#profile-select option")).to_have_count(1)


def test_a_recipe_using_an_environment_secret_says_where_it_comes_from(
    page: Page, app: LiveApp, site: str
) -> None:
    write_recipe(
        app.recipes,
        "uses_env",
        f"url: {site}/register\n"
        "steps:\n"
        "  - type: fill\n"
        '    selector: "#notes"\n'
        '    value: "{{env:PORTAL_PW}}"\n',
    )
    open_dashboard(page, app)
    page.select_option("#recipe-select", "uses_env")
    expect(page.locator("#env-note")).to_be_visible()
    expect(page.locator("#env-note")).to_contain_text("PORTAL_PW")
    expect(page.locator("#typed-fields")).to_be_hidden()


# ---- schedules ----


def test_add_pause_resume_and_delete_a_schedule(page: Page, app: LiveApp, site: str) -> None:
    write_recipe(app.recipes, "look", f"url: {site}/form\nsteps: []\n")
    open_dashboard(page, app)
    page.select_option("#recipe-select", "look")
    expect(page.locator("#schedule-empty")).to_be_visible()

    page.fill("#schedule-when", "every 5 minutes")
    page.click("#schedule-btn")
    expect(page.locator("#schedule-message")).to_contain_text("Scheduled 'look' (every 5 minutes)")
    row = page.locator("#schedule-body tr")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("every 5 minutes")
    expect(row).to_contain_text("Not run yet")
    expect(page.locator("#schedule-empty")).to_be_hidden()

    row.get_by_role("button", name="Pause").click()
    expect(row).to_contain_text("Paused")
    row.get_by_role("button", name="Resume").click()
    expect(row).not_to_contain_text("Paused")

    row.get_by_role("button", name="Delete").click()
    expect(page.locator("#schedule-body tr")).to_have_count(0)
    expect(page.locator("#schedule-empty")).to_be_visible()


def test_a_bad_schedule_is_explained(page: Page, app: LiveApp, site: str) -> None:
    write_recipe(app.recipes, "look", f"url: {site}/form\nsteps: []\n")
    open_dashboard(page, app)
    page.select_option("#recipe-select", "look")
    page.fill("#schedule-when", "every 3 minutes")
    page.click("#schedule-btn")
    expect(page.locator("#schedule-message")).to_contain_text("at least 5 minutes")
    expect(page.locator("#schedule-body tr")).to_have_count(0)


def test_a_schedule_for_a_recipe_that_needs_data_asks_for_a_sheet(
    page: Page, app: LiveApp, site: str, tmp_path: Path
) -> None:
    write_recipe(app.recipes, "register", register_recipe(site))
    open_dashboard(page, app)
    page.select_option("#recipe-select", "register")
    expect(page.locator("#schedule-data-row")).to_be_visible()

    page.fill("#schedule-when", "daily 08:00")
    page.click("#schedule-btn")
    expect(page.locator("#schedule-message")).to_contain_text("needs a data file")

    sheet = tmp_path / "people.csv"
    sheet.write_text("first_name,email,plan\nAnn,ann@example.com,pro\n", encoding="utf-8")
    page.set_input_files("#schedule-file", str(sheet))
    page.click("#schedule-btn")
    expect(page.locator("#schedule-body tr")).to_contain_text("with data sheet")
