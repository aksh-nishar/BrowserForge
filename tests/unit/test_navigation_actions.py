import pytest

from browserforge.engine.actions import do_goto, do_wait_for_selector
from browserforge.engine.driver import DriverTimeoutError
from browserforge.engine.recipe import Step
from fixtures.fake_driver import FakePageDriver


def test_goto_calls_driver_goto_once() -> None:
    fake = FakePageDriver()
    result = do_goto(fake, Step(type="goto", config={"url": "https://x.io/a"}))
    assert result.success
    assert fake.calls == [("goto", ("https://x.io/a",))]


@pytest.mark.parametrize("bad", [None, "", "  ", 5])
def test_goto_without_a_valid_url_fails_without_calling_driver(bad: object) -> None:
    fake = FakePageDriver()
    config = {} if bad is None else {"url": bad}
    result = do_goto(fake, Step(type="goto", config=config))
    assert not result.success
    assert result.error is not None
    assert "url" in result.error
    assert fake.calls == []


def test_goto_navigation_failure_is_a_failed_result() -> None:
    fake = FakePageDriver()
    fake.fail_on("goto", DriverTimeoutError("net::ERR_TIMED_OUT"))
    result = do_goto(fake, Step(type="goto", config={"url": "https://x.io"}))
    assert not result.success
    assert result.error is not None
    assert "ERR_TIMED_OUT" in result.error


def test_wait_for_selector_uses_default_and_custom_timeouts() -> None:
    fake = FakePageDriver()
    do_wait_for_selector(fake, Step(type="wait_for_selector", config={"selector": "#a"}))
    do_wait_for_selector(
        fake, Step(type="wait_for_selector", config={"selector": "#b", "timeout_ms": 500})
    )
    assert fake.calls == [
        ("wait_for_selector", ("#a", 30_000)),
        ("wait_for_selector", ("#b", 500)),
    ]


def test_wait_for_selector_timeout_is_a_failed_result() -> None:
    fake = FakePageDriver()
    fake.set_missing("#late")
    step = Step(type="wait_for_selector", config={"selector": "#late", "timeout_ms": 10})
    result = do_wait_for_selector(fake, step)
    assert not result.success
    assert result.error is not None
    assert "#late" in result.error


def test_wait_for_selector_without_selector_fails_without_calling_driver() -> None:
    fake = FakePageDriver()
    result = do_wait_for_selector(fake, Step(type="wait_for_selector"))
    assert not result.success
    assert fake.calls == []


@pytest.mark.parametrize("bad", [0, -1, "5s", True, 1.5])
def test_wait_for_selector_invalid_timeout_fails_without_calling_driver(bad: object) -> None:
    fake = FakePageDriver()
    step = Step(type="wait_for_selector", config={"selector": "#a", "timeout_ms": bad})
    result = do_wait_for_selector(fake, step)
    assert not result.success
    assert result.error is not None
    assert "timeout_ms" in result.error
    assert fake.calls == []
