"""PlaywrightDriver logic against a mocked Page: no browser is launched."""

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from browserforge.engine.driver import (
    DriverError,
    DriverTimeoutError,
    PageDriver,
    PlaywrightDriver,
    playwright_driver,
    translate_playwright_error,
)


class FakeDownload:
    def __init__(self, filename: str, content: str = "data", failure: str | None = None) -> None:
        self.suggested_filename = filename
        self._content = content
        self._failure = failure

    def failure(self) -> str | None:
        return self._failure

    def save_as(self, path: Any) -> None:
        Path(path).write_text(self._content)


@pytest.fixture
def page() -> MagicMock:
    return MagicMock()


@pytest.fixture
def driver(page: MagicMock) -> PlaywrightDriver:
    return PlaywrightDriver(page, timeout_ms=1234)


def download_handler(page: MagicMock) -> Callable[[Any], None]:
    """The callback the driver registered with page.on("download", ...)."""
    event, handler = page.on.call_args.args
    assert event == "download"
    return handler  # type: ignore[no-any-return]


# ---- construction ----


def test_satisfies_the_page_driver_protocol(page: MagicMock) -> None:
    driver: PageDriver = PlaywrightDriver(page)  # mypy checks every method signature
    assert driver.current_url() == page.url


def test_download_listener_is_a_bound_method_playwright_can_wrap(page: MagicMock) -> None:
    """Regression: registering list.append (a builtin) crashed the real Playwright."""
    PlaywrightDriver(page)
    listener = page.on.call_args.args[1]
    assert not inspect.isbuiltin(listener)
    assert inspect.ismethod(listener)
    listener.__self__.__dict__["_probe"] = 1  # Playwright stores a wrapper on the owner


def test_constructor_sets_default_timeout_and_listens_for_downloads(page: MagicMock) -> None:
    PlaywrightDriver(page, timeout_ms=4321)
    page.set_default_timeout.assert_called_once_with(4321)
    assert page.on.call_args.args[0] == "download"


# ---- delegation ----


def test_goto_fill_click_select_check_delegate_to_page(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    driver.goto("https://x.io")
    driver.fill("#u", "bob")
    driver.click("#go")
    driver.select_option("#c", "IN")
    driver.check("#agree")
    page.goto.assert_called_once_with("https://x.io")
    page.fill.assert_called_once_with("#u", "bob")
    page.click.assert_called_once_with("#go")
    page.select_option.assert_called_once_with("#c", "IN")
    page.check.assert_called_once_with("#agree")


def test_click_waits_for_the_document_to_finish_loading_after_clicking(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    order: list[str] = []
    page.click.side_effect = lambda *_a, **_k: order.append("click")
    page.wait_for_load_state.side_effect = lambda *_a, **_k: order.append("wait")
    driver.click("#next")
    assert order == ["click", "wait"]
    page.wait_for_load_state.assert_called_once_with("domcontentloaded")


def test_no_load_wait_when_the_click_itself_fails(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    page.click.side_effect = PlaywrightTimeoutError("t")
    with pytest.raises(DriverTimeoutError):
        driver.click("#next")
    page.wait_for_load_state.assert_not_called()


def test_a_page_that_never_finishes_parsing_is_a_timeout_error(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    page.wait_for_load_state.side_effect = PlaywrightTimeoutError("t")
    with pytest.raises(DriverTimeoutError, match="'#next'"):
        driver.click("#next")


def test_current_url_reads_the_page_url(driver: PlaywrightDriver, page: MagicMock) -> None:
    page.url = "https://x.io/secure"
    assert driver.current_url() == "https://x.io/secure"


def test_upload_passes_a_list_of_paths(driver: PlaywrightDriver, page: MagicMock) -> None:
    driver.upload("#file", ("a.txt", "b.txt"))
    page.set_input_files.assert_called_once_with("#file", ["a.txt", "b.txt"])


def test_full_page_screenshot(driver: PlaywrightDriver, page: MagicMock) -> None:
    driver.screenshot("out.png")
    page.screenshot.assert_called_once_with(path="out.png", full_page=True)


def test_viewport_screenshot_when_full_page_is_false(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    driver.screenshot("out.png", full_page=False)
    page.screenshot.assert_called_once_with(path="out.png", full_page=False)


def test_element_screenshot_uses_the_locator(driver: PlaywrightDriver, page: MagicMock) -> None:
    driver.screenshot("out.png", selector="#chart", full_page=False)
    page.locator.assert_called_once_with("#chart")
    page.locator.return_value.first.screenshot.assert_called_once_with(path="out.png")
    page.screenshot.assert_not_called()


def test_wait_for_selector_passes_its_own_timeout(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    driver.wait_for_selector("#dash", timeout_ms=250)
    page.wait_for_selector.assert_called_once_with("#dash", timeout=250)


def test_wait_for_url_contains_uses_a_substring_predicate(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    driver.wait_for_url_contains("/secure", timeout_ms=900)
    predicate = page.wait_for_url.call_args.args[0]
    assert page.wait_for_url.call_args.kwargs == {"timeout": 900}
    assert predicate("https://x.io/secure/home") is True
    assert predicate("https://x.io/login") is False


# ---- element state and tables ----


def test_element_state_is_none_when_nothing_matches(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    page.locator.return_value.count.return_value = 0
    assert driver.get_element_state("#next") is None
    page.locator.assert_called_once_with("#next")


@pytest.mark.parametrize(("visible", "disabled"), [(True, False), (True, True), (False, False)])
def test_element_state_reports_visibility_and_disabled(
    driver: PlaywrightDriver, page: MagicMock, visible: bool, disabled: bool
) -> None:
    locator = page.locator.return_value
    locator.count.return_value = 1
    locator.first.is_visible.return_value = visible
    locator.first.is_disabled.return_value = disabled
    assert driver.get_element_state("#next") == {"visible": visible, "disabled": disabled}


def test_get_table_rows_returns_cell_text_as_strings(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    page.locator.return_value.first.evaluate.return_value = [["name", "age"], ["Ann", 30]]
    assert driver.get_table_rows("#tbl") == [["name", "age"], ["Ann", "30"]]
    page.locator.assert_called_once_with("#tbl")


def test_get_table_rows_script_reads_tr_cells(driver: PlaywrightDriver, page: MagicMock) -> None:
    page.locator.return_value.first.evaluate.return_value = []
    driver.get_table_rows("#tbl")
    script = page.locator.return_value.first.evaluate.call_args.args[0]
    assert "querySelectorAll('tr')" in script
    assert "innerText" in script


# ---- error translation ----


def test_translate_timeout_names_the_selector_and_duration() -> None:
    error = translate_playwright_error(
        PlaywrightTimeoutError("Timeout 500ms exceeded"), "'#a'", 500
    )
    assert type(error) is DriverTimeoutError
    assert str(error) == "Timed out after 500ms waiting for '#a'"


def test_translate_other_errors_keeps_only_the_first_line() -> None:
    error = translate_playwright_error(
        PlaywrightError("Element is not a <select>\nCall log:\n  - waiting for locator"), "'#a'", 1
    )
    assert type(error) is DriverError
    assert str(error) == "Element is not a <select>"


def test_translate_blank_message_gets_a_generic_text() -> None:
    error = translate_playwright_error(PlaywrightError("  \n "), "'#a'", 1)
    assert str(error) == "Browser error"


@pytest.mark.parametrize(
    "call",
    [
        lambda d: d.goto("https://x.io"),
        lambda d: d.fill("#a", "v"),
        lambda d: d.click("#a"),
        lambda d: d.select_option("#a", "v"),
        lambda d: d.check("#a"),
        lambda d: d.upload("#a", ["f"]),
        lambda d: d.screenshot("o.png"),
        lambda d: d.screenshot("o.png", selector="#a"),
        lambda d: d.get_table_rows("#a"),
        lambda d: d.wait_for_selector("#a"),
        lambda d: d.wait_for_url_contains("/x"),
        lambda d: d.get_element_state("#a"),
    ],
)
def test_playwright_timeouts_surface_as_driver_timeout_errors(
    driver: PlaywrightDriver, page: MagicMock, call: Callable[[PlaywrightDriver], object]
) -> None:
    timeout = PlaywrightTimeoutError("Timeout exceeded")
    for name in (
        "goto",
        "fill",
        "click",
        "select_option",
        "check",
        "set_input_files",
        "screenshot",
        "wait_for_selector",
        "wait_for_url",
    ):
        getattr(page, name).side_effect = timeout
    page.locator.return_value.first.screenshot.side_effect = timeout
    page.locator.return_value.first.evaluate.side_effect = timeout
    page.locator.return_value.count.side_effect = timeout

    with pytest.raises(DriverTimeoutError, match="Timed out after"):
        call(driver)


def test_generic_playwright_errors_surface_as_plain_driver_errors(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    page.click.side_effect = PlaywrightError("strict mode violation: 3 elements")
    with pytest.raises(DriverError, match="strict mode violation") as exc:
        driver.click("#a")
    assert not isinstance(exc.value, DriverTimeoutError)


def test_non_playwright_exceptions_are_not_swallowed(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    page.click.side_effect = RuntimeError("real bug")
    with pytest.raises(RuntimeError, match="real bug"):
        driver.click("#a")


def test_timeout_message_uses_the_waits_own_timeout(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    page.wait_for_selector.side_effect = PlaywrightTimeoutError("t")
    with pytest.raises(DriverTimeoutError, match=r"Timed out after 250ms waiting for '#late'"):
        driver.wait_for_selector("#late", timeout_ms=250)


# ---- downloads ----


def test_wait_for_download_saves_under_the_original_file_name(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    download_handler(page)(FakeDownload("report.csv", "a,b"))
    path = Path(driver.wait_for_download())
    assert path.name == "report.csv"
    assert path.read_text() == "a,b"
    driver.close()


def test_download_that_arrives_while_waiting_is_picked_up(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    handler = download_handler(page)
    page.wait_for_timeout.side_effect = lambda _ms: handler(FakeDownload("late.csv"))
    path = Path(driver.wait_for_download())
    assert path.name == "late.csv"
    page.wait_for_timeout.assert_called_once()
    driver.close()


def test_downloads_are_returned_in_the_order_they_started(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    handler = download_handler(page)
    handler(FakeDownload("one.csv"))
    handler(FakeDownload("two.csv"))
    first, second = Path(driver.wait_for_download()), Path(driver.wait_for_download())
    assert (first.name, second.name) == ("one.csv", "two.csv")
    driver.close()


def test_same_file_name_downloaded_twice_does_not_collide(
    driver: PlaywrightDriver, page: MagicMock
) -> None:
    handler = download_handler(page)
    handler(FakeDownload("report.csv", "first"))
    handler(FakeDownload("report.csv", "second"))
    first, second = Path(driver.wait_for_download()), Path(driver.wait_for_download())
    assert first != second
    assert (first.read_text(), second.read_text()) == ("first", "second")
    driver.close()


def test_no_download_times_out(page: MagicMock) -> None:
    driver = PlaywrightDriver(page, timeout_ms=50)
    with pytest.raises(DriverTimeoutError, match="No download started within 50ms"):
        driver.wait_for_download()
    assert page.wait_for_timeout.called


def test_failed_download_is_reported(driver: PlaywrightDriver, page: MagicMock) -> None:
    download_handler(page)(FakeDownload("x.csv", failure="canceled"))
    with pytest.raises(DriverError, match="Download failed: canceled"):
        driver.wait_for_download()


@pytest.mark.parametrize(
    ("suggested", "expected"), [("../../evil.csv", "evil.csv"), ("", "download"), ("/", "download")]
)
def test_suggested_file_names_cannot_escape_the_temp_dir(
    driver: PlaywrightDriver, page: MagicMock, suggested: str, expected: str
) -> None:
    download_handler(page)(FakeDownload(suggested))
    path = Path(driver.wait_for_download())
    assert path.name == expected
    assert path.exists()
    driver.close()


def test_close_removes_downloaded_temp_files(driver: PlaywrightDriver, page: MagicMock) -> None:
    download_handler(page)(FakeDownload("report.csv"))
    path = Path(driver.wait_for_download())
    assert path.exists()
    driver.close()
    assert not path.exists()
    driver.close()  # idempotent


def test_close_without_any_download_is_a_noop(driver: PlaywrightDriver) -> None:
    driver.close()


# ---- launcher ----


def test_playwright_driver_launches_yields_and_always_closes_the_browser() -> None:
    launcher = MagicMock()
    playwright = launcher.__enter__.return_value
    browser = playwright.chromium.launch.return_value
    with patch("browserforge.engine.driver.sync_playwright", return_value=launcher):
        with playwright_driver(headless=False, timeout_ms=777) as driver:
            assert isinstance(driver, PlaywrightDriver)
            page = browser.new_context.return_value.new_page.return_value
            page.set_default_timeout.assert_called_once_with(777)
        playwright.chromium.launch.assert_called_once_with(headless=False)
        browser.new_context.assert_called_once_with(accept_downloads=True)
        browser.close.assert_called_once()


def test_browser_is_closed_even_if_the_body_raises() -> None:
    launcher = MagicMock()
    browser = launcher.__enter__.return_value.chromium.launch.return_value
    with patch("browserforge.engine.driver.sync_playwright", return_value=launcher):
        with pytest.raises(RuntimeError, match="boom"):
            with playwright_driver():
                raise RuntimeError("boom")
    browser.close.assert_called_once()
