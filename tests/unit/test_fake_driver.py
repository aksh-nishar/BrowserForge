"""Sanity tests for the test double itself: a buggy fake silently breaks every downstream test."""

from pathlib import Path

import pytest

from browserforge.engine.driver import (
    DriverTimeoutError,
    ElementNotFoundError,
    PageDriver,
)
from fixtures.fake_driver import FakePageDriver


def test_fake_satisfies_page_driver_protocol() -> None:
    driver: PageDriver = FakePageDriver()  # mypy verifies every method signature matches
    assert driver.current_url() == ""


def test_goto_sets_url() -> None:
    fake = FakePageDriver()
    fake.goto("https://example.com")
    assert fake.current_url() == "https://example.com"


def test_fill_click_select_check_are_recorded() -> None:
    fake = FakePageDriver()
    fake.fill("#user", "bob")
    fake.click("#go")
    fake.select_option("#country", "IN")
    fake.check("#agree")
    assert fake.fills == {"#user": "bob"}
    assert fake.clicks == ["#go"]
    assert fake.selected == {"#country": "IN"}
    assert fake.checked == ["#agree"]
    assert [name for name, _ in fake.calls] == ["fill", "click", "select_option", "check"]


def test_calls_to_filters_by_method() -> None:
    fake = FakePageDriver()
    fake.fill("#a", "1")
    fake.click("#b")
    fake.fill("#c", "2")
    assert fake.calls_to("fill") == [("#a", "1"), ("#c", "2")]


def test_upload_records_paths_as_list() -> None:
    fake = FakePageDriver()
    fake.upload("#file", ("a.txt", "b.txt"))
    assert fake.uploads == [("#file", ["a.txt", "b.txt"])]


def test_set_table_round_trip() -> None:
    fake = FakePageDriver()
    rows = [["name", "age"], ["Ann", "30"]]
    fake.set_table("#tbl", rows)
    assert fake.get_table_rows("#tbl") == rows


def test_unscripted_table_raises_not_found() -> None:
    with pytest.raises(ElementNotFoundError):
        FakePageDriver().get_table_rows("#nope")


def test_screenshot_records_and_writes_file(tmp_path: Path) -> None:
    fake = FakePageDriver()
    target = tmp_path / "shots" / "a.png"
    fake.screenshot(str(target))
    assert fake.screenshots == [str(target)]
    assert target.exists()


def test_screenshot_records_selector_and_full_page(tmp_path: Path) -> None:
    fake = FakePageDriver()
    target = str(tmp_path / "x.png")
    fake.screenshot(target, selector="#el", full_page=False)
    assert fake.calls_to("screenshot") == [(target, "#el", False)]


def test_download_queue_is_fifo_and_times_out_when_empty() -> None:
    fake = FakePageDriver()
    fake.queue_download("one.csv")
    fake.queue_download("two.csv")
    assert fake.wait_for_download() == "one.csv"
    assert fake.wait_for_download() == "two.csv"
    with pytest.raises(DriverTimeoutError):
        fake.wait_for_download()


def test_missing_selector_raises_on_actions_and_times_out_on_wait() -> None:
    fake = FakePageDriver()
    fake.set_missing("#gone")
    with pytest.raises(ElementNotFoundError, match="#gone"):
        fake.click("#gone")
    with pytest.raises(ElementNotFoundError):
        fake.fill("#gone", "x")
    with pytest.raises(DriverTimeoutError):
        fake.wait_for_selector("#gone", timeout_ms=10)
    assert fake.get_element_state("#gone") is None
    assert fake.clicks == []  # failed action must not record success


def test_fail_on_raises_n_times_then_succeeds() -> None:
    fake = FakePageDriver()
    fake.fail_on("fill", DriverTimeoutError("slow"), times=2)
    for _ in range(2):
        with pytest.raises(DriverTimeoutError):
            fake.fill("#a", "x")
    fake.fill("#a", "x")
    assert fake.fills == {"#a": "x"}
    assert len(fake.calls_to("fill")) == 3


def test_fail_on_none_fails_every_call() -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("always"), times=None)
    for _ in range(5):
        with pytest.raises(DriverTimeoutError):
            fake.click("#a")
    assert len(fake.calls_to("click")) == 5


def test_fail_on_unknown_method_is_rejected() -> None:
    with pytest.raises(ValueError, match="fil"):
        FakePageDriver().fail_on("fil", RuntimeError())


def test_element_state_scripting() -> None:
    fake = FakePageDriver()
    assert fake.get_element_state("#next") is None
    fake.set_element_state("#next", visible=True, disabled=True)
    assert fake.get_element_state("#next") == {"visible": True, "disabled": True}


def test_set_url_on_click_simulates_navigation() -> None:
    fake = FakePageDriver()
    fake.set_url_on_click("#login", "https://x.io/secure")
    fake.click("#login")
    assert fake.current_url() == "https://x.io/secure"


def test_paged_table_advances_on_next_click_and_disables_at_end() -> None:
    fake = FakePageDriver()
    pages = [[["h"], ["p1"]], [["h"], ["p2"]], [["h"], ["p3"]]]
    fake.set_pages("#tbl", "#next", pages)

    assert fake.get_table_rows("#tbl") == pages[0]
    assert fake.get_element_state("#next") == {"visible": True, "disabled": False}
    fake.click("#next")
    assert fake.get_table_rows("#tbl") == pages[1]
    fake.click("#next")
    assert fake.get_table_rows("#tbl") == pages[2]
    assert fake.get_element_state("#next") == {"visible": True, "disabled": True}
    fake.click("#next")  # clicking a disabled next stays on the last page
    assert fake.get_table_rows("#tbl") == pages[2]


def test_infinite_paged_table_never_disables() -> None:
    fake = FakePageDriver()
    fake.set_pages("#tbl", "#next", [[["h"], ["a"]], [["h"], ["b"]]], infinite=True)
    for _ in range(10):
        assert fake.get_element_state("#next") == {"visible": True, "disabled": False}
        fake.click("#next")
