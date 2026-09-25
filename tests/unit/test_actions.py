from collections.abc import Callable

import pytest

from browserforge.engine.actions import (
    ActionResult,
    do_check,
    do_click,
    do_fill,
    do_select_option,
)
from browserforge.engine.driver import DriverTimeoutError, PageDriver
from browserforge.engine.recipe import Step
from fixtures.fake_driver import FakePageDriver

Action = Callable[[PageDriver, Step], ActionResult]

# (action, step type, needs a value)
ACTIONS: list[tuple[Action, str, bool]] = [
    (do_fill, "fill", True),
    (do_click, "click", False),
    (do_select_option, "select_option", True),
    (do_check, "check", False),
]


def make_step(step_type: str, **config: object) -> Step:
    return Step(type=step_type, config=dict(config))


# ---- happy paths: right driver call, right arguments, exactly once ----


def test_do_fill_calls_driver_fill_once_with_args() -> None:
    fake = FakePageDriver()
    result = do_fill(fake, make_step("fill", selector="#user", value="bob"))
    assert result == ActionResult(success=True)
    assert fake.calls == [("fill", ("#user", "bob"))]


def test_do_click_calls_driver_click() -> None:
    fake = FakePageDriver()
    result = do_click(fake, make_step("click", selector="#go"))
    assert result.success
    assert fake.calls == [("click", ("#go",))]


def test_do_select_option_calls_driver_with_value() -> None:
    fake = FakePageDriver()
    result = do_select_option(fake, make_step("select_option", selector="#country", value="IN"))
    assert result.success
    assert fake.calls == [("select_option", ("#country", "IN"))]


def test_do_check_calls_driver_check() -> None:
    fake = FakePageDriver()
    result = do_check(fake, make_step("check", selector="#agree"))
    assert result.success
    assert fake.calls == [("check", ("#agree",))]


def test_fill_coerces_non_string_value_to_str() -> None:
    fake = FakePageDriver()
    do_fill(fake, make_step("fill", selector="#age", value=42))
    assert fake.fills == {"#age": "42"}


def test_fill_allows_empty_string_value_to_clear_a_field() -> None:
    fake = FakePageDriver()
    result = do_fill(fake, make_step("fill", selector="#note", value=""))
    assert result.success
    assert fake.fills == {"#note": ""}


# ---- bad config: clear error, driver never touched ----


@pytest.mark.parametrize(("action", "step_type", "needs_value"), ACTIONS)
def test_missing_selector_returns_error_without_calling_driver(
    action: Action, step_type: str, needs_value: bool
) -> None:
    fake = FakePageDriver()
    config: dict[str, object] = {"value": "x"} if needs_value else {}
    result = action(fake, Step(type=step_type, config=config))
    assert not result.success
    assert result.error is not None
    assert "selector" in result.error
    assert step_type in result.error
    assert fake.calls == []


@pytest.mark.parametrize(("action", "step_type", "needs_value"), ACTIONS)
def test_blank_selector_is_treated_as_missing(
    action: Action, step_type: str, needs_value: bool
) -> None:
    fake = FakePageDriver()
    result = action(fake, make_step(step_type, selector="   ", value="x"))
    assert not result.success
    assert fake.calls == []


@pytest.mark.parametrize(
    ("action", "step_type"), [(do_fill, "fill"), (do_select_option, "select_option")]
)
def test_missing_value_returns_error_without_calling_driver(action: Action, step_type: str) -> None:
    fake = FakePageDriver()
    result = action(fake, make_step(step_type, selector="#a"))
    assert not result.success
    assert result.error is not None
    assert "value" in result.error
    assert fake.calls == []


def test_error_uses_step_name_when_given() -> None:
    step = Step(type="click", name="submit_form", config={})
    result = do_click(FakePageDriver(), step)
    assert result.error is not None
    assert "submit_form" in result.error


# ---- driver failures: returned as results, never raised ----


@pytest.mark.parametrize(("action", "step_type", "needs_value"), ACTIONS)
def test_driver_timeout_becomes_failed_result(
    action: Action, step_type: str, needs_value: bool
) -> None:
    fake = FakePageDriver()
    method = step_type
    fake.fail_on(method, DriverTimeoutError("Timed out after 30000ms"))
    result = action(fake, make_step(step_type, selector="#a", value="x"))
    assert not result.success
    assert result.error is not None
    assert "Timed out after 30000ms" in result.error


@pytest.mark.parametrize(("action", "step_type", "needs_value"), ACTIONS)
def test_selector_not_found_becomes_failed_result(
    action: Action, step_type: str, needs_value: bool
) -> None:
    fake = FakePageDriver()
    fake.set_missing("#gone")
    result = action(fake, make_step(step_type, selector="#gone", value="x"))
    assert not result.success
    assert result.error is not None
    assert "#gone" in result.error


def test_unexpected_exception_is_not_swallowed() -> None:
    fake = FakePageDriver()
    fake.fail_on("click", RuntimeError("bug in driver"))
    with pytest.raises(RuntimeError, match="bug in driver"):
        do_click(fake, make_step("click", selector="#a"))
