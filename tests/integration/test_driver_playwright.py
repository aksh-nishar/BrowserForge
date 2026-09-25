"""Real Chromium + real (local) pages: proves PlaywrightDriver honours the PageDriver contract
that FakePageDriver stood in for in the unit tests."""

import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from browserforge.engine.actions import ActionContext, do_download, do_upload
from browserforge.engine.driver import (
    DriverError,
    DriverTimeoutError,
    PageDriver,
    PlaywrightDriver,
    playwright_driver,
)
from browserforge.engine.pagination import paginate_and_collect
from browserforge.engine.recipe import Step
from fixtures.local_site import PASSWORD, USERNAME

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def log_in(driver: PageDriver, base: str, password: str = PASSWORD) -> None:
    driver.goto(f"{base}/login")
    driver.fill("#username", USERNAME)
    driver.fill("#password", password)
    driver.click("#submit")


# ---- login flow ----


def test_login_success_lands_on_secure_page(driver: PlaywrightDriver, site: str) -> None:
    log_in(driver, site)
    driver.wait_for_url_contains("/secure")
    driver.wait_for_selector("#dashboard")
    assert driver.current_url().endswith("/secure")


def test_login_failure_stays_on_login_and_shows_flash(driver: PlaywrightDriver, site: str) -> None:
    log_in(driver, site, password="wrong")
    driver.wait_for_selector("#flash")
    assert driver.current_url().endswith("/login")
    assert driver.get_element_state("#flash") == {"visible": True, "disabled": False}
    with pytest.raises(DriverTimeoutError, match="URL containing '/secure'"):
        driver.wait_for_url_contains("/secure", timeout_ms=300)


# ---- form actions ----


def test_fill_select_and_check_change_the_page(driver: PlaywrightDriver, site: str) -> None:
    driver.goto(f"{site}/form")
    driver.fill("#name", "bob")
    driver.select_option("#country", "IN")
    driver.check("#agree")
    assert driver.page.input_value("#name") == "bob"
    assert driver.page.input_value("#country") == "IN"
    assert driver.page.is_checked("#agree")


# ---- element state ----


def test_element_state_absent_visible_hidden_and_disabled(
    driver: PlaywrightDriver, site: str
) -> None:
    driver.goto(f"{site}/form")
    assert driver.get_element_state("#nope") is None
    assert driver.get_element_state("#name") == {"visible": True, "disabled": False}
    assert driver.get_element_state("#hidden-note") == {"visible": False, "disabled": False}

    driver.goto(f"{site}/table?page=3")
    assert driver.get_element_state("#next") == {"visible": True, "disabled": True}


# ---- tables and pagination ----


def test_get_table_rows_returns_header_and_data_rows_as_text(
    driver: PlaywrightDriver, site: str
) -> None:
    driver.goto(f"{site}/table?page=1")
    assert driver.get_table_rows("#employees") == [
        ["ID", "Name", "Dept"],
        ["1", "Ann", "Sales"],
        ["2", "Bob", "IT"],
    ]


def test_missing_table_times_out_with_the_selector_in_the_message(
    driver: PlaywrightDriver, site: str
) -> None:
    driver.goto(f"{site}/form")
    with pytest.raises(DriverTimeoutError, match="#employees"):
        driver.get_table_rows("#employees")


def test_paginate_and_collect_walks_all_three_real_pages(
    driver: PlaywrightDriver, site: str
) -> None:
    driver.goto(f"{site}/table?page=1")
    result = paginate_and_collect(driver, "#employees", "#next")
    assert result.pages == 3
    assert result.hit_cap is False
    assert result.rows[0] == ["ID", "Name", "Dept"]
    assert [row[1] for row in result.rows[1:]] == ["Ann", "Bob", "Cy", "Di", "Ed", "Flo"]


def test_pagination_cap_stops_a_site_whose_next_never_disables(
    driver: PlaywrightDriver, site: str
) -> None:
    driver.goto(f"{site}/table-endless")
    result = paginate_and_collect(driver, "#employees", "#next", max_pages=3)
    assert result.pages == 3
    assert result.hit_cap is True


def test_pagination_waits_for_a_next_page_that_loads_slowly(
    driver: PlaywrightDriver, site: str
) -> None:
    """Regression: click() used to return while the new page was still half-parsed, so a slow
    site's table looked short and its Next link missing: pagination stopped early with
    silently partial data (seen on a real public site)."""
    driver.goto(f"{site}/slow-table?page=1")
    result = paginate_and_collect(driver, "#slow", "#next")
    assert result.pages == 3
    assert result.hit_cap is False
    assert result.rows[0] == ["ID", "Name"]
    assert len(result.rows) - 1 == 12  # 3 pages x 4 rows, none cut short
    assert result.rows[-1] == ["34", "Row 3.4"]


# ---- waiting ----


def test_wait_for_selector_waits_for_a_late_element(driver: PlaywrightDriver, site: str) -> None:
    driver.goto(f"{site}/delayed")
    driver.wait_for_selector("#late")
    assert driver.get_element_state("#late") == {"visible": True, "disabled": False}


def test_wait_for_missing_selector_times_out_with_the_given_timeout(
    driver: PlaywrightDriver, site: str
) -> None:
    driver.goto(f"{site}/form")
    with pytest.raises(DriverTimeoutError, match=r"Timed out after 300ms waiting for '#nope'"):
        driver.wait_for_selector("#nope", timeout_ms=300)


def test_acting_on_a_missing_element_times_out_naming_it(
    driver: PlaywrightDriver, site: str
) -> None:
    driver.goto(f"{site}/form")
    with pytest.raises(DriverTimeoutError, match="'#nope'"):
        driver.click("#nope")


def test_navigation_to_a_dead_port_is_a_plain_driver_error(driver: PlaywrightDriver) -> None:
    with socket.socket() as sock:  # grab a free port, then release it so nothing listens there
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    with pytest.raises(DriverError, match="ERR_CONNECTION_REFUSED") as exc:
        driver.goto(f"http://127.0.0.1:{dead_port}/")
    assert not isinstance(exc.value, DriverTimeoutError)


# ---- files ----


def test_download_is_saved_under_its_original_name(driver: PlaywrightDriver, site: str) -> None:
    driver.goto(f"{site}/download")
    driver.click("#export")
    path = Path(driver.wait_for_download())
    assert path.name == "report.csv"
    assert path.read_text() == "id,name\n1,Ann\n2,Bob\n"


def test_no_download_times_out(driver: PlaywrightDriver, site: str) -> None:
    driver.goto(f"{site}/form")
    with pytest.raises(DriverTimeoutError, match="No download started"):
        driver.wait_for_download()


def test_download_action_moves_the_file_into_the_job_folder(
    driver: PlaywrightDriver, site: str, tmp_path: Path
) -> None:
    driver.goto(f"{site}/download")
    job_dir = tmp_path / "job"
    step = Step(type="download", config={"selector": "#export"})
    result = do_download(driver, step, ActionContext(job_dir=job_dir))
    assert result.success, result.error
    saved = job_dir / "downloads" / "report.csv"
    assert result.data["path"] == str(saved)
    assert saved.read_text() == "id,name\n1,Ann\n2,Bob\n"


def test_upload_sends_multiple_files_to_the_server(
    driver: PlaywrightDriver, site: str, tmp_path: Path
) -> None:
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("alpha")
    second.write_text("beta")
    driver.goto(f"{site}/upload")
    result = do_upload(
        driver,
        Step(type="upload", config={"selector": "#file", "files": [str(first), str(second)]}),
    )
    assert result.success, result.error
    driver.click("#submit-upload")
    driver.wait_for_selector("#uploaded")
    assert driver.page.inner_text("#uploaded").splitlines() == ["a.txt: alpha", "b.txt: beta"]


def test_upload_to_a_missing_input_times_out(
    driver: PlaywrightDriver, site: str, tmp_path: Path
) -> None:
    file = tmp_path / "a.txt"
    file.write_text("x")
    driver.goto(f"{site}/form")
    with pytest.raises(DriverTimeoutError, match="'#file'"):
        driver.upload("#file", [str(file)])


# ---- screenshots ----


def test_full_page_and_element_screenshots_are_real_png_files(
    driver: PlaywrightDriver, site: str, tmp_path: Path
) -> None:
    driver.goto(f"{site}/form")
    full, element = tmp_path / "full.png", tmp_path / "chart.png"
    driver.screenshot(str(full))
    driver.screenshot(str(element), selector="#chart")
    for file in (full, element):
        assert file.read_bytes().startswith(PNG_SIGNATURE)
    assert element.stat().st_size < full.stat().st_size


def test_screenshot_of_a_missing_element_times_out(
    driver: PlaywrightDriver, site: str, tmp_path: Path
) -> None:
    driver.goto(f"{site}/form")
    with pytest.raises(DriverTimeoutError, match="'#nope'"):
        driver.screenshot(str(tmp_path / "x.png"), selector="#nope")


# ---- launcher ----


def test_playwright_driver_launcher_runs_a_real_headless_browser(site: str) -> None:
    def job() -> tuple[str, object]:
        with playwright_driver(timeout_ms=2_000) as launched:
            launched.goto(f"{site}/form")
            return launched.current_url(), launched.get_element_state("#name")

    # Worker thread, like production jobs: the shared pytest-playwright browser already owns
    # Playwright's event loop in the main thread, and sync Playwright allows one per thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        url, state = pool.submit(job).result(timeout=60)
    assert url == f"{site}/form"
    assert state == {"visible": True, "disabled": False}


# ---- public site (from the plan); skipped when offline ----


def _public_site_reachable() -> bool:
    try:
        socket.create_connection(("the-internet.herokuapp.com", 443), timeout=3).close()
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _public_site_reachable(), reason="public test site not reachable")
def test_public_login_page(driver: PlaywrightDriver) -> None:
    driver.page.set_default_timeout(20_000)  # real internet: far slower than the local site
    driver.goto("https://the-internet.herokuapp.com/login")
    driver.fill("#username", "tomsmith")
    driver.fill("#password", "SuperSecretPassword!")
    driver.click("button[type=submit]")
    driver.wait_for_url_contains("/secure", timeout_ms=10_000)
    assert driver.current_url().endswith("/secure")
