from typing import Any

import pytest
from structlog.testing import capture_logs

from browserforge.engine.actions import do_login
from browserforge.engine.driver import DriverError, DriverTimeoutError
from browserforge.engine.recipe import Step
from fixtures.fake_driver import FakePageDriver

PASSWORD = "S3cret!pw"


def login_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "username_selector": "#user",
        "password_selector": "#pass",
        "submit_selector": "#submit",
        "username": "tomsmith",
        "password": PASSWORD,
        "success_selector": "#dashboard",
    }
    config.update(overrides)
    return config


def login_step(**overrides: Any) -> Step:
    config = login_config(**overrides)
    return Step(type="login", config={k: v for k, v in config.items() if v is not None})


# ---- success ----


def test_call_sequence_fill_user_fill_pass_click_submit_then_wait() -> None:
    fake = FakePageDriver()
    result = do_login(fake, login_step(timeout_ms=5000))
    assert result.success
    assert result.error is None
    assert fake.calls == [
        ("fill", ("#user", "tomsmith")),
        ("fill", ("#pass", PASSWORD)),
        ("click", ("#submit",)),
        ("wait_for_selector", ("#dashboard", 5000)),
    ]


def test_default_timeout_is_used_when_not_configured() -> None:
    fake = FakePageDriver()
    do_login(fake, login_step())
    assert fake.calls_to("wait_for_selector") == [("#dashboard", 30_000)]


def test_url_marker_waits_for_url_fragment() -> None:
    fake = FakePageDriver()
    fake.set_url_on_click("#submit", "https://x.io/secure")
    result = do_login(fake, login_step(success_selector=None, success_url_contains="/secure"))
    assert result.success
    assert fake.calls[-1] == ("wait_for_url_contains", ("/secure", 30_000))


def test_both_markers_are_both_waited_for() -> None:
    fake = FakePageDriver()
    fake.set_url_on_click("#submit", "https://x.io/secure")
    result = do_login(fake, login_step(success_url_contains="/secure"))
    assert result.success
    assert [name for name, _ in fake.calls[-2:]] == ["wait_for_selector", "wait_for_url_contains"]


def test_non_string_password_from_yaml_is_coerced() -> None:
    fake = FakePageDriver()
    do_login(fake, login_step(password=123456))
    assert fake.fills["#pass"] == "123456"


# ---- config validation: fail fast, driver untouched ----


@pytest.mark.parametrize(
    "missing_key",
    ["username_selector", "password_selector", "submit_selector", "username", "password"],
)
def test_missing_required_field_names_it_and_skips_driver(missing_key: str) -> None:
    fake = FakePageDriver()
    result = do_login(fake, login_step(**{missing_key: None}))
    assert not result.success
    assert result.error is not None
    assert missing_key in result.error
    assert fake.calls == []


def test_blank_credentials_count_as_missing() -> None:
    fake = FakePageDriver()
    result = do_login(fake, login_step(username="  ", password=""))
    assert not result.success
    assert result.error is not None
    assert "username" in result.error
    assert "password" in result.error
    assert fake.calls == []


def test_missing_success_marker_is_a_config_error() -> None:
    fake = FakePageDriver()
    result = do_login(fake, login_step(success_selector=None))
    assert not result.success
    assert result.error is not None
    assert "success_selector" in result.error
    assert fake.calls == []


@pytest.mark.parametrize("bad", [0, -5, "soon", True, 1.5])
def test_invalid_timeout_is_a_config_error(bad: object) -> None:
    fake = FakePageDriver()
    result = do_login(fake, login_step(timeout_ms=bad))
    assert not result.success
    assert result.error is not None
    assert "timeout_ms" in result.error
    assert fake.calls == []


# ---- failures: specific login message, never a raw timeout ----


def test_post_login_timeout_is_reported_as_login_failure() -> None:
    fake = FakePageDriver()
    fake.set_missing("#dashboard")  # wrong password: dashboard never appears
    result = do_login(fake, login_step(timeout_ms=1234))
    assert not result.success
    assert result.error is not None
    assert result.error.startswith("Login failed")
    assert "1234ms" in result.error
    assert "username and password" in result.error


def test_url_marker_timeout_is_reported_as_login_failure() -> None:
    fake = FakePageDriver()  # url never changes to /secure
    result = do_login(fake, login_step(success_selector=None, success_url_contains="/secure"))
    assert not result.success
    assert result.error is not None
    assert result.error.startswith("Login failed")


def test_missing_form_field_is_reported_as_login_failure_with_selector() -> None:
    fake = FakePageDriver()
    fake.set_missing("#user")
    result = do_login(fake, login_step())
    assert not result.success
    assert result.error is not None
    assert result.error.startswith("Login failed")
    assert "#user" in result.error
    assert fake.clicks == []  # stopped before submitting


def test_timeout_while_filling_is_not_blamed_on_credentials() -> None:
    fake = FakePageDriver()
    fake.fail_on("fill", DriverTimeoutError("Timed out waiting for '#user'"))
    result = do_login(fake, login_step())
    assert result.error is not None
    assert "Timed out waiting for '#user'" in result.error
    assert "username and password" not in result.error


# ---- secrets never leak ----


def test_password_is_redacted_from_driver_error_messages() -> None:
    fake = FakePageDriver()
    # the first fill (username) raises with the password embedded in the message
    fake.fail_on("fill", DriverError(f"could not type {PASSWORD} into field"))
    result = do_login(fake, login_step())
    assert result.error is not None
    assert PASSWORD not in result.error
    assert "***" in result.error


def test_login_failure_emits_event_without_credentials() -> None:
    fake = FakePageDriver()
    fake.set_missing("#dashboard")
    with capture_logs() as logs:
        do_login(fake, login_step())
    events = [entry for entry in logs if entry["event"] == "login_failed"]
    assert len(events) == 1
    assert events[0]["reason"] == "post_login_timeout"
    assert PASSWORD not in repr(logs)
    assert "tomsmith" not in repr(logs)


def test_successful_login_emits_no_failure_event() -> None:
    with capture_logs() as logs:
        do_login(FakePageDriver(), login_step())
    assert not [entry for entry in logs if entry["event"] == "login_failed"]
