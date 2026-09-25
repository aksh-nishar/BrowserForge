"""One function per step type: (driver, step) -> ActionResult.

Actions never raise for expected failures (bad config, driver timeouts, missing
elements); they return ActionResult(success=False, error=...) so the runner has a
single failure path. Unexpected exceptions are bugs and are left to propagate.
"""

from collections.abc import Callable
from dataclasses import dataclass

import structlog

from browserforge.engine.driver import DriverError, DriverTimeoutError, PageDriver
from browserforge.engine.recipe import Step

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ActionResult:
    success: bool
    error: str | None = None


def _label(step: Step) -> str:
    return f"Step '{step.name or step.type}'"


def _fail(step: Step, message: str) -> ActionResult:
    return ActionResult(success=False, error=f"{_label(step)}: {message}")


def _run(
    step: Step,
    call: Callable[[str, str], None],
    needs_value: bool = False,
) -> ActionResult:
    """Validate selector (and value), run `call(selector, value)`, map driver errors."""
    selector = step.config.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        return _fail(step, "missing required field 'selector'")

    value = ""
    if needs_value:
        raw = step.config.get("value")
        if raw is None:
            return _fail(step, "missing required field 'value'")
        value = str(raw)

    try:
        call(selector, value)
    except DriverError as exc:
        return _fail(step, str(exc))
    return ActionResult(success=True)


def do_fill(driver: PageDriver, step: Step) -> ActionResult:
    return _run(step, lambda selector, value: driver.fill(selector, value), needs_value=True)


def do_click(driver: PageDriver, step: Step) -> ActionResult:
    return _run(step, lambda selector, _value: driver.click(selector))


def do_select_option(driver: PageDriver, step: Step) -> ActionResult:
    return _run(
        step,
        lambda selector, value: driver.select_option(selector, value),
        needs_value=True,
    )


def do_check(driver: PageDriver, step: Step) -> ActionResult:
    return _run(step, lambda selector, _value: driver.check(selector))


_LOGIN_REQUIRED = (
    "username_selector",
    "password_selector",
    "submit_selector",
    "username",
    "password",
)
_DEFAULT_LOGIN_TIMEOUT_MS = 30_000


def _login_failed(reason: str, message: str) -> ActionResult:
    # Never log credentials: only the reason and the (already redacted) message.
    logger.warning("login_failed", reason=reason, error=message)
    return ActionResult(success=False, error=message)


def do_login(driver: PageDriver, step: Step) -> ActionResult:
    """Fill credentials, submit, then wait for the post-login marker.

    Config: username_selector, password_selector, submit_selector, username, password,
    plus success_selector and/or success_url_contains; optional timeout_ms.
    """
    config = step.config

    missing = [key for key in _LOGIN_REQUIRED if not str(config.get(key) or "").strip()]
    if missing:
        return ActionResult(
            success=False,
            error=f"Login step is missing required field(s): {', '.join(missing)}",
        )

    success_selector = config.get("success_selector")
    success_url = config.get("success_url_contains")
    if not success_selector and not success_url:
        return ActionResult(
            success=False,
            error=(
                "Login step needs 'success_selector' or 'success_url_contains' "
                "to know when login worked"
            ),
        )

    timeout_ms = config.get("timeout_ms", _DEFAULT_LOGIN_TIMEOUT_MS)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        return ActionResult(success=False, error="Login 'timeout_ms' must be a positive integer")

    password = str(config["password"])

    def redact(text: str) -> str:
        return text.replace(password, "***")

    try:
        driver.fill(str(config["username_selector"]), str(config["username"]))
        driver.fill(str(config["password_selector"]), password)
        driver.click(str(config["submit_selector"]))
    except DriverError as exc:
        return _login_failed("form_error", f"Login failed: {redact(str(exc))}")

    try:
        if success_selector:
            driver.wait_for_selector(str(success_selector), timeout_ms)
        if success_url:
            driver.wait_for_url_contains(str(success_url), timeout_ms)
    except DriverTimeoutError:
        return _login_failed(
            "post_login_timeout",
            f"Login failed: the logged-in page did not appear within {timeout_ms}ms. "
            "Check the username and password.",
        )
    except DriverError as exc:
        return _login_failed("post_login_error", f"Login failed: {redact(str(exc))}")

    return ActionResult(success=True)
