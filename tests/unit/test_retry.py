import json
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from browserforge.engine.driver import DriverError, DriverTimeoutError
from browserforge.engine.errors import RETRYABLE_KINDS
from browserforge.engine.recipe import Recipe, Step
from browserforge.engine.runner import RunLog, run_recipe
from fixtures.fake_driver import FakePageDriver

LOGIN = {
    "username_selector": "#u",
    "password_selector": "#p",
    "submit_selector": "#s",
    "username": "tom",
    "password": "pw",
    "success_selector": "#dash",
}


def click_recipe(**kwargs: Any) -> Recipe:
    return Recipe(
        url="https://x.io", steps=[Step(type="click", config={"selector": "#go"})], **kwargs
    )


def run(recipe: Recipe, fake: FakePageDriver, job_dir: Path) -> tuple[RunLog, list[float]]:
    sleeps: list[float] = []
    log = run_recipe(recipe, fake, job_dir, sleep=sleeps.append)
    return log, sleeps


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    return tmp_path / "job"


# ---- fail twice, then succeed ----


def test_step_that_fails_twice_then_succeeds_on_the_third_attempt(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=2)
    log, _ = run(click_recipe(retries=2), fake, job_dir)

    click = log.steps[-1]
    assert click.success is True
    assert click.attempts == 3
    assert len(click.retry_errors) == 2
    assert all("Timed out" in error for error in click.retry_errors)
    assert len(fake.calls_to("click")) == 3
    assert log.success is True


def test_retry_history_is_written_to_run_log_json(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=1)
    run(click_recipe(retries=1), fake, job_dir)
    saved = json.loads((job_dir / "run_log.json").read_text(encoding="utf-8"))
    click = saved["steps"][-1]
    assert click["attempts"] == 2
    assert len(click["retry_errors"]) == 1


def test_first_try_success_reports_one_attempt_and_no_retries(job_dir: Path) -> None:
    fake = FakePageDriver()
    log, sleeps = run(click_recipe(retries=3), fake, job_dir)
    assert log.steps[-1].attempts == 1
    assert log.steps[-1].retry_errors == []
    assert sleeps == []


# ---- always failing: bounded ----


@pytest.mark.parametrize("retries", [0, 1, 2, 5])
def test_always_failing_step_gives_up_after_exactly_retries_plus_one_calls(
    job_dir: Path, retries: int
) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=None)
    log, _ = run(click_recipe(retries=retries), fake, job_dir)

    assert len(fake.calls_to("click")) == retries + 1
    click = log.steps[-1]
    assert click.success is False
    assert click.attempts == retries + 1
    assert len(click.retry_errors) == retries
    assert log.success is False


def test_default_is_no_retries(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=None)
    log, sleeps = run(click_recipe(), fake, job_dir)
    assert len(fake.calls_to("click")) == 1
    assert log.steps[-1].attempts == 1
    assert sleeps == []


# ---- back-off ----


def test_backoff_doubles_between_attempts_and_is_not_applied_after_the_last(
    job_dir: Path,
) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=None)
    _, sleeps = run(click_recipe(retries=3, backoff_seconds=0.5), fake, job_dir)
    assert sleeps == [0.5, 1.0, 2.0]  # 3 retries -> 3 waits, none after the final failure


def test_zero_backoff_means_immediate_retries(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=None)
    _, sleeps = run(click_recipe(retries=2, backoff_seconds=0), fake, job_dir)
    assert sleeps == [0, 0]


def test_default_backoff_is_one_second(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=None)
    _, sleeps = run(click_recipe(retries=1), fake, job_dir)
    assert sleeps == [1.0]


# ---- per-step override ----


def test_step_level_settings_override_the_recipe(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=None)
    recipe = Recipe(
        url="https://x.io",
        retries=5,
        backoff_seconds=9,
        steps=[
            Step(type="click", config={"selector": "#go"}, retries=1, backoff_seconds=0.25),
        ],
    )
    _, sleeps = run(recipe, fake, job_dir)
    assert len(fake.calls_to("click")) == 2
    assert sleeps == [0.25]


def test_step_can_turn_retries_off_when_the_recipe_enables_them(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=None)
    recipe = Recipe(
        url="https://x.io",
        retries=3,
        steps=[Step(type="click", config={"selector": "#go"}, retries=0)],
    )
    run(recipe, fake, job_dir)
    assert len(fake.calls_to("click")) == 1


def test_retries_apply_to_the_implicit_start_page_step_too(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("goto", DriverTimeoutError("Timed out after 100ms waiting for x to load"), times=1)
    log, _ = run(Recipe(url="https://x.io", retries=1), fake, job_dir)
    assert log.steps[0].attempts == 2
    assert log.success is True


# ---- only transient failures are retried ----


def test_bad_step_config_is_never_retried(job_dir: Path) -> None:
    fake = FakePageDriver()
    recipe = Recipe(
        url="https://x.io", retries=3, steps=[Step(type="fill", config={"selector": "#a"})]
    )
    log, sleeps = run(recipe, fake, job_dir)  # 'value' is missing: fails the same way every time
    assert log.steps[-1].attempts == 1
    assert sleeps == []


def test_a_wrong_password_is_never_retried(job_dir: Path) -> None:
    """Retrying a rejected login could lock the account."""
    fake = FakePageDriver()
    fake.set_missing("#dash")
    log, sleeps = run(Recipe(url="https://x.io", login=LOGIN, retries=3), fake, job_dir)
    login = log.steps[1]
    assert login.success is False
    assert login.attempts == 1
    assert len(fake.calls_to("fill")) == 2  # username and password typed exactly once
    assert sleeps == []


def test_unexpected_errors_are_never_retried(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", RuntimeError("bug"), times=None)
    log, _ = run(click_recipe(retries=3), fake, job_dir)
    assert len(fake.calls_to("click")) == 1
    assert log.steps[-1].attempts == 1


@pytest.mark.parametrize(
    "error",
    [
        DriverTimeoutError("Timed out after 100ms waiting for '#go'"),
        DriverError("Element is not attached"),
        DriverError("Page.goto: net::ERR_CONNECTION_RESET"),
    ],
)
def test_transient_browser_errors_are_retried(job_dir: Path, error: DriverError) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", error, times=1)
    log, _ = run(click_recipe(retries=1), fake, job_dir)
    assert log.steps[-1].success is True
    assert log.steps[-1].attempts == 2


def test_url_wait_failures_are_not_retryable() -> None:
    assert "url_wait" not in RETRYABLE_KINDS
    assert "unexpected" not in RETRYABLE_KINDS


# ---- retries and the rest of the run ----


def test_a_step_that_recovers_lets_the_run_continue(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#a'"), times=1)
    recipe = Recipe(
        url="https://x.io",
        retries=1,
        steps=[
            Step(type="click", config={"selector": "#a"}),
            Step(type="check", config={"selector": "#b"}),
        ],
    )
    log, _ = run(recipe, fake, job_dir)
    assert log.success is True
    assert fake.checked == ["#b"]


def test_an_exhausted_step_stops_the_run_by_default(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#a'"), times=None)
    recipe = Recipe(
        url="https://x.io",
        retries=1,
        steps=[
            Step(type="click", config={"selector": "#a"}),
            Step(type="check", config={"selector": "#b"}),
        ],
    )
    log, _ = run(recipe, fake, job_dir)
    assert log.stopped_early is True
    assert fake.checked == []


def test_each_retry_is_logged_as_an_event(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=2)
    with capture_logs() as logs:
        run(click_recipe(retries=2), fake, job_dir)
    retries = [entry for entry in logs if entry["event"] == "step_retry"]
    assert [(e["attempt"], e["max_attempts"]) for e in retries] == [(1, 3), (2, 3)]
    assert all(e["step_type"] == "click" for e in retries)
