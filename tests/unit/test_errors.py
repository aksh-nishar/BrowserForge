from typing import get_args

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from browserforge.engine.driver import (
    DriverError,
    DriverTimeoutError,
    ElementNotFoundError,
    translate_playwright_error,
)
from browserforge.engine.errors import RETRYABLE_KINDS, ErrorKind, classify, humanize
from fixtures.fake_driver import FakePageDriver

# ---- classify ----


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (ElementNotFoundError("No element matches selector '#a'"), "not_found"),
        (DriverTimeoutError("Timed out after 30000ms waiting for '#a'"), "timeout"),
        (
            DriverTimeoutError("Timed out after 5000ms waiting for https://x.io to load"),
            "navigation",
        ),
        (
            DriverTimeoutError("Timed out after 900ms waiting for URL containing '/secure'"),
            "url_wait",
        ),
        (DriverTimeoutError("No download started within 50ms"), "download"),
        (
            DriverTimeoutError("Timed out after 100ms waiting for the download to finish"),
            "download",
        ),
        (
            DriverError("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://nope.invalid/"),
            "navigation",
        ),
        (DriverError("Page.goto: net::ERR_CONNECTION_REFUSED"), "navigation"),
        (DriverError("strict mode violation: '#a' resolved to 3 elements"), "driver"),
        (DriverError("Download failed: canceled"), "driver"),
        (ValueError("bug"), "unexpected"),
        (RuntimeError("bug"), "unexpected"),
        (KeyError("k"), "unexpected"),
    ],
)
def test_classify_maps_each_exception_type_to_a_kind(exc: BaseException, kind: str) -> None:
    assert classify(exc) == kind


def test_a_selector_that_merely_contains_the_word_download_is_still_a_timeout() -> None:
    error = DriverTimeoutError("Timed out after 30000ms waiting for '#download-btn'")
    assert classify(error) == "timeout"


def test_classification_matches_the_messages_the_real_driver_produces() -> None:
    """If driver.py rewords its errors, this fails instead of silently misclassifying them."""
    timeout = PlaywrightTimeoutError("Timeout 5000ms exceeded")
    cases = {
        "https://x.io to load": "navigation",
        "'#login'": "timeout",
        "table '#t'": "timeout",
        "URL containing '/secure'": "url_wait",
        "the download to finish": "download",
    }
    for what, kind in cases.items():
        assert classify(translate_playwright_error(timeout, what, 5000)) == kind, what
    refused = PlaywrightError("Page.goto: net::ERR_CONNECTION_REFUSED at http://x/")
    assert classify(translate_playwright_error(refused, "x to load", 5000)) == "navigation"
    assert classify(translate_playwright_error(PlaywrightError("boom"), "'#a'", 5000)) == "driver"


def test_classification_matches_the_messages_the_fake_driver_produces() -> None:
    fake = FakePageDriver()
    fake.set_missing("#gone")
    with pytest.raises(DriverTimeoutError) as wait_selector:
        fake.wait_for_selector("#gone", timeout_ms=10)
    with pytest.raises(DriverTimeoutError) as wait_url:
        fake.wait_for_url_contains("/never", timeout_ms=10)
    with pytest.raises(DriverTimeoutError) as wait_download:
        fake.wait_for_download()
    with pytest.raises(ElementNotFoundError) as click:
        fake.click("#gone")
    assert classify(wait_selector.value) == "timeout"
    assert classify(wait_url.value) == "url_wait"
    assert classify(wait_download.value) == "download"
    assert classify(click.value) == "not_found"


def test_only_transient_kinds_are_retryable() -> None:
    assert {"timeout", "not_found", "navigation", "download", "driver"} == RETRYABLE_KINDS
    assert not {"url_wait", "unexpected"} & RETRYABLE_KINDS


# ---- humanize ----


def test_timeout_message_names_the_target_and_the_wait_in_seconds() -> None:
    raw = "Step 'press_go': Timed out after 30000ms waiting for '#go'"
    text = humanize("timeout", raw)
    assert "'#go'" in text
    assert "30 seconds" in text
    assert "selector" in text
    assert "Timed out after" not in text  # the technical wording stays in the run log
    assert "Step 'press_go'" not in text


@pytest.mark.parametrize(
    ("ms", "expected"), [(1000, "1 second"), (300, "0.3 seconds"), (2500, "2.5 seconds")]
)
def test_wait_time_is_shown_in_friendly_units(ms: int, expected: str) -> None:
    assert expected in humanize("timeout", f"Timed out after {ms}ms waiting for '#a'")


def test_timeout_without_details_still_reads_naturally() -> None:
    text = humanize("timeout", "something odd")
    assert "the element" in text
    assert "the time limit" in text


def test_not_found_message_names_the_selector() -> None:
    text = humanize("not_found", "Step 'x': No element matches selector '#submit'")
    assert "'#submit'" in text
    assert "selector" in text


def test_not_found_without_a_quoted_selector() -> None:
    assert "the element" in humanize("not_found", "no such thing")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x.invalid/",
            "address could not be found",
        ),
        ("Page.goto: net::ERR_CONNECTION_REFUSED", "refused the connection"),
        ("Page.goto: net::ERR_INTERNET_DISCONNECTED", "no internet connection"),
        ("Timed out after 5000ms waiting for https://x.io to load", "Could not open the page"),
        ("Page.goto: net::ERR_SOMETHING_ELSE", "Could not open the page"),
    ],
)
def test_navigation_failures_get_specific_explanations(raw: str, expected: str) -> None:
    assert expected in humanize("navigation", raw)


def test_url_wait_message_shows_the_expected_address_fragment() -> None:
    text = humanize("url_wait", "Timed out after 900ms waiting for URL containing '/secure'")
    assert "'/secure'" in text
    assert "never changed" in text


def test_login_message_drops_the_repeated_prefix_and_uses_seconds() -> None:
    raw = (
        "Login failed: the logged-in page did not appear within 30000ms. "
        "Check the username and password."
    )
    assert humanize("login", raw) == (
        "The logged-in page did not appear within 30 seconds. Check the username and password."
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Login failed: Timed out after 1000ms waiting for '#u'", "Timed out after 1 second"),
        ("Login failed: Timed out after 2500ms waiting for '#u'", "Timed out after 2.5 seconds"),
        ("login went wrong", "Login went wrong"),
        ("Login failed: could not type *** into field", "Could not type *** into field"),
        ("Login failed:", ""),
    ],
)
def test_login_message_variants(raw: str, expected: str) -> None:
    assert humanize("login", raw).startswith(expected)


def test_login_is_a_declared_kind_and_never_retryable() -> None:
    assert "login" in get_args(ErrorKind)
    assert "login" not in RETRYABLE_KINDS


def test_download_message() -> None:
    assert "Nothing was downloaded" in humanize("download", "No download started within 50ms")


def test_driver_error_message_quotes_the_first_line_of_the_problem() -> None:
    text = humanize("driver", "Step 'pick': Element is not a <select>")
    assert text == "The browser reported a problem: Element is not a <select>"


def test_config_and_login_errors_pass_through_without_the_step_prefix() -> None:
    assert humanize(None, "Step 'fill': missing required field 'value'") == (
        "missing required field 'value'"
    )
    assert humanize(None, "Login failed: the logged-in page did not appear") == (
        "Login failed: the logged-in page did not appear"
    )


def test_unexpected_errors_get_a_generic_message_without_technical_detail() -> None:
    raw = "Step 'x': unexpected error (KeyError): 'secret_key'\nTraceback (most recent call last)"
    text = humanize("unexpected", raw)
    assert text == "Something unexpected went wrong. The details are saved in the run log."
    assert "KeyError" not in text
    assert "Traceback" not in text


@pytest.mark.parametrize("kind", get_args(ErrorKind))
def test_every_kind_has_a_readable_non_technical_message(kind: ErrorKind) -> None:
    text = humanize(kind, "Step 's': Timed out after 5000ms waiting for '#a'")
    assert text
    assert "Traceback" not in text
    assert "Exception" not in text
    assert "ms waiting" not in text or kind == "driver"  # only the driver fallback quotes the raw


@pytest.mark.parametrize("raw", ["", "   ", "\n", "x" * 20_000, "🚀 नमस्ते", "'''", "%s %d {0}"])
@pytest.mark.parametrize("kind", [*get_args(ErrorKind), None])
def test_humanize_never_raises_on_odd_input(kind: ErrorKind | None, raw: str) -> None:
    assert isinstance(humanize(kind, raw), str)
