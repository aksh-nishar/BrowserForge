import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from browserforge.engine.driver import DriverError, DriverTimeoutError
from browserforge.engine.recipe import Recipe, Step
from browserforge.engine.runner import RunLog, run_recipe
from fixtures.fake_driver import FakePageDriver

FIXED_NOW = datetime(2026, 9, 25, 10, 15, 30)


def click_recipe(**kwargs: Any) -> Recipe:
    return Recipe(
        url="https://x.io", steps=[Step(type="click", config={"selector": "#go"})], **kwargs
    )


def run(recipe: Recipe, fake: FakePageDriver, job_dir: Path) -> RunLog:
    return run_recipe(recipe, fake, job_dir, now=lambda: FIXED_NOW, sleep=lambda _s: None)


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    return tmp_path / "job"


# ---- a failing step triggers exactly one failure screenshot ----


def test_failed_step_takes_one_screenshot_with_a_failure_prefix(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.set_missing("#go")
    log = run(click_recipe(), fake, job_dir)

    shots = fake.calls_to("screenshot")
    assert len(shots) == 1
    path = Path(shots[0][0])
    assert path.parent == job_dir / "screenshots"
    assert path.name == "failure_02_click_20260925-101530.png"  # step 2: the click
    assert shots[0][1:] == (None, True)  # whole page, not an element
    assert log.steps[-1].failure_screenshot == str(path)
    assert path.exists()


def test_successful_steps_take_no_failure_screenshots(job_dir: Path) -> None:
    fake = FakePageDriver()
    log = run(click_recipe(), fake, job_dir)
    assert fake.calls_to("screenshot") == []
    assert all(step.failure_screenshot is None for step in log.steps)
    assert not (job_dir / "screenshots").exists()


def test_one_screenshot_per_failed_step_not_per_retry_attempt(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=None)
    log = run(click_recipe(retries=3), fake, job_dir)
    assert len(fake.calls_to("click")) == 4
    assert len(fake.calls_to("screenshot")) == 1
    assert log.steps[-1].attempts == 4


def test_a_step_that_recovers_on_retry_takes_no_failure_screenshot(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=1)
    log = run(click_recipe(retries=1), fake, job_dir)
    assert log.success is True
    assert fake.calls_to("screenshot") == []


def test_with_continue_on_error_every_failed_step_gets_its_own_screenshot(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.set_missing("#a", "#b")
    recipe = Recipe(
        url="https://x.io",
        continue_on_error=True,
        steps=[
            Step(type="click", config={"selector": "#a"}),
            Step(type="click", config={"selector": "#ok"}),
            Step(type="click", config={"selector": "#b"}),
        ],
    )
    log = run(recipe, fake, job_dir)
    names = sorted(Path(s.failure_screenshot).name for s in log.steps if s.failure_screenshot)
    assert names == [
        "failure_02_click_20260925-101530.png",
        "failure_04_click_20260925-101530.png",
    ]
    assert [s.failure_screenshot is None for s in log.steps] == [True, False, True, False]


def test_a_step_with_bad_config_also_gets_a_screenshot(job_dir: Path) -> None:
    fake = FakePageDriver()
    recipe = Recipe(url="https://x.io", steps=[Step(type="fill", config={"selector": "#a"})])
    log = run(recipe, fake, job_dir)
    assert log.steps[-1].success is False
    assert log.steps[-1].failure_screenshot is not None


def test_a_crashing_action_also_gets_a_screenshot(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", RuntimeError("bug"))
    log = run(click_recipe(), fake, job_dir)
    assert log.steps[-1].failure_screenshot is not None


def test_a_failed_start_page_is_photographed_too(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("goto", DriverTimeoutError("Timed out after 100ms waiting for x to load"))
    log = run(click_recipe(), fake, job_dir)
    assert log.steps[0].failure_screenshot is not None
    assert Path(log.steps[0].failure_screenshot).name.startswith("failure_01_open_start_page_")


# ---- the screenshot is best-effort: it never masks the real error ----


def test_when_the_failure_screenshot_also_fails_the_original_error_is_still_reported(
    job_dir: Path,
) -> None:
    fake = FakePageDriver()
    fake.set_missing("#go")
    fake.fail_on("screenshot", DriverError("browser has closed"), times=None)
    log = run(click_recipe(), fake, job_dir)

    step = log.steps[-1]
    assert step.success is False
    assert step.error is not None
    assert "#go" in step.error  # the click's error, not the screenshot's
    assert "browser has closed" not in step.error
    assert step.human_error is not None
    assert "browser has closed" not in step.human_error
    assert step.failure_screenshot is None
    assert log.success is False
    assert log.stopped_early is True  # the run still behaves as a normal failure


def test_the_secondary_screenshot_error_is_logged_not_swapped_in(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.set_missing("#go")
    fake.fail_on("screenshot", DriverError("browser has closed"), times=None)
    with capture_logs() as logs:
        run(click_recipe(), fake, job_dir)
    secondary = [e for e in logs if e["event"] == "failure_screenshot_failed"]
    assert len(secondary) == 1
    assert secondary[0]["error"] == "browser has closed"
    assert secondary[0]["log_level"] == "warning"
    assert not [e for e in logs if e["event"] == "failure_screenshot"]


def test_an_unexpected_exception_from_the_screenshot_is_swallowed_too(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.set_missing("#go")
    fake.fail_on("screenshot", RuntimeError("disk exploded"), times=None)
    log = run(click_recipe(), fake, job_dir)
    assert log.steps[-1].failure_screenshot is None
    assert "#go" in (log.steps[-1].error or "")


def test_a_screenshots_folder_that_cannot_be_created_is_swallowed(job_dir: Path) -> None:
    job_dir.mkdir()
    (job_dir / "screenshots").write_text("a file where the folder should be")
    fake = FakePageDriver()
    fake.set_missing("#go")
    log = run(click_recipe(), fake, job_dir)
    assert log.steps[-1].failure_screenshot is None
    assert log.success is False


def test_a_failing_screenshot_does_not_stop_later_steps_with_continue_on_error(
    job_dir: Path,
) -> None:
    fake = FakePageDriver()
    fake.set_missing("#a")
    fake.fail_on("screenshot", DriverError("gone"), times=None)
    recipe = Recipe(
        url="https://x.io",
        continue_on_error=True,
        steps=[
            Step(type="click", config={"selector": "#a"}),
            Step(type="check", config={"selector": "#b"}),
        ],
    )
    run(recipe, fake, job_dir)
    assert fake.checked == ["#b"]


# ---- recording ----


def test_the_screenshot_path_is_recorded_in_run_log_json(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.set_missing("#go")
    log = run(click_recipe(), fake, job_dir)
    saved = json.loads((job_dir / "run_log.json").read_text(encoding="utf-8"))
    assert saved["steps"][-1]["failure_screenshot"] == log.steps[-1].failure_screenshot
    assert saved["steps"][0]["failure_screenshot"] is None


def test_a_success_event_names_the_saved_file(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.set_missing("#go")
    with capture_logs() as logs:
        log = run(click_recipe(), fake, job_dir)
    saved = [e for e in logs if e["event"] == "failure_screenshot"]
    assert len(saved) == 1
    assert saved[0]["path"] == log.steps[-1].failure_screenshot
