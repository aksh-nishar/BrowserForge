import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient

from browserforge.app import create_app
from browserforge.engine.driver import DriverError, DriverTimeoutError
from browserforge.engine.errors import ErrorKind
from browserforge.engine.recipe import Recipe, Step
from browserforge.engine.runner import RunLog, StepLog, run_recipe
from browserforge.jobs import JobStore, run_job
from fixtures.fake_driver import FakePageDriver


def run(recipe: Recipe, fake: FakePageDriver, job_dir: Path) -> RunLog:
    return run_recipe(recipe, fake, job_dir, sleep=lambda _s: None)


def one_step(step: Step, **kwargs: Any) -> Recipe:
    return Recipe(url="https://x.io", steps=[step], **kwargs)


def click(selector: str = "#go") -> Step:
    return Step(type="click", config={"selector": selector})


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    return tmp_path / "job"


def failed_step(log: RunLog) -> StepLog:
    return next(step for step in log.steps if not step.success)


# ---- raw error kept for debugging, human message for the dashboard ----


def test_timeout_keeps_the_raw_error_and_adds_a_plain_language_one(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 30000ms waiting for '#go'"))
    step = failed_step(run(one_step(click()), fake, job_dir))

    assert step.error_kind == "timeout"
    assert step.error is not None
    assert "Timed out after 30000ms" in step.error  # raw, for debugging
    assert step.human_error is not None
    assert "Timed out after" not in step.human_error
    assert "'#go'" in step.human_error
    assert "30 seconds" in step.human_error


def test_missing_element_gets_a_not_found_message(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.set_missing("#go")
    step = failed_step(run(one_step(click()), fake, job_dir))
    assert step.error_kind == "not_found"
    assert step.human_error == "Couldn't find '#go' on the page. Check the selector in the recipe."


def test_navigation_failure_gets_a_plain_explanation(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("goto", DriverError("Page.goto: net::ERR_CONNECTION_REFUSED at http://x/"))
    step = failed_step(run(one_step(click()), fake, job_dir))
    assert step.error_kind == "navigation"
    assert step.human_error == (
        "The website refused the connection. It may be down, or the address is wrong."
    )


def test_config_errors_are_already_plain_and_only_lose_the_step_prefix(job_dir: Path) -> None:
    fake = FakePageDriver()
    step = failed_step(run(one_step(Step(type="fill", config={"selector": "#a"})), fake, job_dir))
    assert step.error_kind is None
    assert step.error == "Step 'fill': missing required field 'value'"
    assert step.human_error == "missing required field 'value'"


def test_unknown_step_type_message(job_dir: Path) -> None:
    step = failed_step(run(one_step(Step(type="teleport")), FakePageDriver(), job_dir))
    assert step.human_error == "unknown step type 'teleport'"


def test_login_failure_is_phrased_in_plain_seconds_without_repeating_login_failed(
    job_dir: Path,
) -> None:
    login = {
        "username_selector": "#u",
        "password_selector": "#p",
        "submit_selector": "#s",
        "username": "tom",
        "password": "pw",
        "success_selector": "#dash",
    }
    fake = FakePageDriver()
    fake.set_missing("#dash")
    log = run(Recipe(url="https://x.io", login=login), fake, job_dir)
    step = failed_step(log)
    assert step.error_kind == "login"
    # raw text is unchanged, for debugging
    assert step.error == (
        "Login failed: the logged-in page did not appear within 30000ms. "
        "Check the username and password."
    )
    # the dashboard already says "Step 2 (login) failed:", so no second "Login failed:",
    # and the wait is shown in seconds
    assert step.human_error == (
        "The logged-in page did not appear within 30 seconds. Check the username and password."
    )
    assert "Login failed" not in step.human_error
    assert "ms" not in step.human_error


def test_unmapped_crash_falls_back_to_a_generic_message_without_a_stack_trace(
    job_dir: Path,
) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", KeyError("secret_internal_key"))
    step = failed_step(run(one_step(click()), fake, job_dir))

    assert step.error_kind == "unexpected"
    assert step.error is not None
    assert "KeyError" in step.error  # the raw detail is preserved in the log
    assert step.human_error == (
        "Something unexpected went wrong. The details are saved in the run log."
    )
    assert "KeyError" not in step.human_error
    assert "secret_internal_key" not in step.human_error
    assert "Traceback" not in step.human_error


def test_successful_steps_have_no_error_fields(job_dir: Path) -> None:
    log = run(one_step(click()), FakePageDriver(), job_dir)
    assert all(step.error is None for step in log.steps)
    assert all(step.human_error is None for step in log.steps)
    assert all(step.error_kind is None for step in log.steps)


def test_retry_history_keeps_raw_errors(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 100ms waiting for '#go'"), times=1)
    log = run(one_step(click(), retries=1), fake, job_dir)
    assert log.steps[-1].retry_errors == ["Step 'click': Timed out after 100ms waiting for '#go'"]


def test_excel_export_failure_has_a_friendly_message(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.set_table("#t", [["a"], ["1"]])
    (job_dir / "output.xlsx").mkdir(parents=True)
    log = run(one_step(Step(type="extract_table", config={"selector": "#t"})), fake, job_dir)
    export = log.steps[-1]
    assert export.type == "export_excel"
    assert export.human_error is not None
    assert "open in another program" in export.human_error
    assert str(job_dir) not in export.human_error  # no filesystem paths shown to the user


def test_run_log_json_has_both_the_raw_and_the_friendly_message(job_dir: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 30000ms waiting for '#go'"))
    run(one_step(click()), fake, job_dir)
    saved = json.loads((job_dir / "run_log.json").read_text(encoding="utf-8"))["steps"][-1]
    assert "Timed out after 30000ms" in saved["error"]
    assert "Timed out after" not in saved["human_error"]
    assert saved["error_kind"] == "timeout"


@pytest.mark.parametrize(
    "scenario",
    ["timeout", "not_found", "navigation", "config", "unknown_type", "crash", "download"],
)
def test_every_error_kind_the_runner_produces_is_a_declared_kind(
    job_dir: Path, scenario: str
) -> None:
    fake = FakePageDriver()
    step = click()
    if scenario == "timeout":
        fake.fail_on("click", DriverTimeoutError("Timed out after 1ms waiting for '#go'"))
    elif scenario == "not_found":
        fake.set_missing("#go")
    elif scenario == "navigation":
        fake.fail_on("goto", DriverError("net::ERR_NAME_NOT_RESOLVED"))
    elif scenario == "config":
        step = Step(type="click")
    elif scenario == "unknown_type":
        step = Step(type="warp")
    elif scenario == "crash":
        fake.fail_on("click", ValueError("x"))
    elif scenario == "download":
        step = Step(type="download", config={"selector": "#dl"})
    kind = failed_step(run(one_step(step), fake, job_dir)).error_kind
    assert kind is None or kind in get_args(ErrorKind)


# ---- what the API shows ----


def api_status(tmp_path: Path, fake: FakePageDriver, recipe_yaml: str) -> dict[str, Any]:
    (tmp_path / "recipes").mkdir()
    (tmp_path / "recipes" / "demo.yaml").write_text(recipe_yaml, encoding="utf-8")
    client = TestClient(
        create_app(
            recipes_dir=tmp_path / "recipes",
            jobs_dir=tmp_path / "jobs",
            driver_factory=lambda: nullcontext(fake),
        )
    )
    job_id = client.post("/jobs", json={"recipe_name": "demo"}).json()["job_id"]
    status: dict[str, Any] = client.get(f"/status/{job_id}").json()
    return status


def test_status_shows_the_friendly_message_and_never_the_raw_one(tmp_path: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", DriverTimeoutError("Timed out after 30000ms waiting for '#go'"))
    status = api_status(
        tmp_path, fake, "url: https://x.io\nsteps:\n  - {type: click, selector: '#go'}\n"
    )

    assert status["failure"]["error"].startswith("Couldn't find or use '#go' within 30 seconds")
    assert status["steps"][-1]["error"] == status["failure"]["error"]
    assert "Timed out after" not in json.dumps(status)
    assert "Step 'click'" not in json.dumps(status)


def test_status_for_a_crashed_step_has_no_exception_details(tmp_path: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("click", KeyError("secret_internal_key"))
    status = api_status(
        tmp_path, fake, "url: https://x.io\nsteps:\n  - {type: click, selector: '#go'}\n"
    )
    assert "secret_internal_key" not in json.dumps(status)
    assert "KeyError" not in json.dumps(status)
    assert status["failure"]["error"].startswith("Something unexpected went wrong")


# ---- jobs that could not start at all ----


def crash_message(tmp_path: Path, exc: Exception) -> str:
    store = JobStore()
    job_id = store.create("demo").job_id

    def broken() -> Any:
        raise exc

    run_job(store, job_id, Recipe(url="https://x.io"), "demo", tmp_path / job_id, broken)
    error = store.get(job_id).error
    assert error is not None
    return error


def test_missing_browser_install_gets_an_actionable_message(tmp_path: Path) -> None:
    exc = RuntimeError("BrowserType.launch: Executable doesn't exist at C:\\x\\chrome.exe")
    message = crash_message(tmp_path, exc)
    assert "uv run playwright install chromium" in message
    assert "C:\\x" not in message


def test_driver_errors_at_startup_are_translated(tmp_path: Path) -> None:
    message = crash_message(tmp_path, DriverError("Page.goto: net::ERR_INTERNET_DISCONNECTED"))
    assert message == "There is no internet connection."


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyError("k"), ValueError("secret path /x")])
def test_any_other_startup_crash_gets_a_generic_message_without_details(
    tmp_path: Path, exc: Exception
) -> None:
    message = crash_message(tmp_path, exc)
    assert message.startswith("The job could not be started because of an unexpected problem")
    assert "boom" not in message
    assert "secret path" not in message
    assert "Traceback" not in message
