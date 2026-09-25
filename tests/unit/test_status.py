import json
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from browserforge.app import create_app
from browserforge.engine.recipe import Step
from browserforge.engine.runner import RunLog, StepLog
from browserforge.jobs import JobRecord, JobStore, build_status
from fixtures.fake_driver import FakePageDriver

CREATED = "2026-09-25T12:00:00"


def record(**overrides: Any) -> JobRecord:
    base: dict[str, Any] = {"job_id": "abc123abc123", "recipe_name": "demo", "created_at": CREATED}
    base.update(overrides)
    return JobRecord(**base)


def step(index: int, success: bool = True, error: str | None = None, **kwargs: Any) -> StepLog:
    return StepLog(
        index=index,
        type=kwargs.pop("type", "click"),
        name=kwargs.pop("name", None),
        success=success,
        error=error,
        duration_ms=kwargs.pop("duration_ms", 10),
        data=kwargs.pop("data", {}),
    )


def finished_log(success: bool = True, **kwargs: Any) -> RunLog:
    return RunLog(
        job_id="abc123abc123",
        recipe_name="demo",
        url="https://x.io",
        started_at="t0",
        finished_at="t1",
        duration_ms=kwargs.pop("duration_ms", 1234),
        success=success,
        **kwargs,
    )


# ---- shape at each stage ----


def test_pending_job_shape() -> None:
    status = build_status(record())
    assert status == {
        "job_id": "abc123abc123",
        "recipe_name": "demo",
        "status": "pending",
        "done": False,
        "created_at": CREATED,
        "finished_at": None,
        "current_step": None,
        "steps_total": None,
        "steps_completed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "steps": [],
        "failure": None,
        "warnings": [],
        "summary": None,
        "error": None,
        "dry_run": False,
        "bulk": None,
    }


def test_mid_run_status_reports_current_step_and_counts() -> None:
    status = build_status(
        record(
            status="running",
            steps_total=5,
            current_step={"index": 3, "name": "download_report", "type": "download"},
            steps=[
                step(1, name="open_start_page", type="goto"),
                step(2, name="login", type="login"),
            ],
        )
    )
    assert status["status"] == "running"
    assert status["done"] is False
    assert status["current_step"] == {"index": 3, "name": "download_report", "type": "download"}
    assert status["steps_total"] == 5
    assert status["steps_completed"] == 2
    assert status["succeeded"] == 2
    assert status["failed"] == 0
    assert status["summary"] is None
    assert [s["name"] for s in status["steps"]] == ["open_start_page", "login"]


def test_steps_carry_index_type_name_result_and_duration() -> None:
    status = build_status(
        record(status="running", steps=[step(1, type="fill", name="user", duration_ms=42)])
    )
    assert status["steps"] == [
        {
            "index": 1,
            "type": "fill",
            "name": "user",
            "success": True,
            "error": None,
            "duration_ms": 42,
        }
    ]


def test_unnamed_steps_are_shown_by_type() -> None:
    status = build_status(record(status="running", steps=[step(1, type="click", name=None)]))
    assert status["steps"][0]["name"] == "click"


def test_completed_job_includes_a_final_summary() -> None:
    log = finished_log(output_file="/jobs/abc/output.xlsx")
    log.steps = [step(1), step(2)]
    status = build_status(
        record(
            status="complete",
            finished_at="2026-09-25T12:00:05",
            steps_total=2,
            steps=[step(1), step(2)],
            run_log=log,
        )
    )
    assert status["done"] is True
    assert status["finished_at"] == "2026-09-25T12:00:05"
    assert status["succeeded"] == 2
    assert status["failure"] is None
    assert status["summary"] == {
        "success": True,
        "duration_ms": 1234,
        "steps_run": 2,
        "skipped": 0,
        "output_file": "output.xlsx",
    }


def test_summary_output_file_is_none_when_no_excel_was_made() -> None:
    log = finished_log()
    status = build_status(record(status="complete", run_log=log))
    assert status["summary"]["output_file"] is None


def test_failed_job_names_the_failing_step_and_why() -> None:
    steps = [
        step(1, type="goto", name="open_start_page"),
        step(2, type="login", name="login", success=False, error="Login failed: bad password"),
    ]
    log = finished_log(success=False, stopped_early=True, skipped_steps=4)
    log.steps = steps
    status = build_status(record(status="failed", steps=steps, steps_total=6, run_log=log))
    assert status["status"] == "failed"
    assert status["done"] is True
    assert status["failure"] == {
        "step_index": 2,
        "step": "login",
        "error": "Login failed: bad password",
    }
    assert (status["succeeded"], status["failed"], status["skipped"]) == (1, 1, 4)
    assert status["summary"]["success"] is False
    assert status["summary"]["skipped"] == 4
    assert status["steps"][1]["error"] == "Login failed: bad password"


def test_failure_points_at_the_first_failed_step_when_several_failed() -> None:
    steps = [
        step(1, success=False, error="first"),
        step(2),
        step(3, success=False, error="second"),
    ]
    status = build_status(record(status="failed", steps=steps, run_log=finished_log(False)))
    assert status["failure"]["step_index"] == 1
    assert status["failed"] == 2


def test_job_that_could_not_start_reports_a_job_level_error() -> None:
    status = build_status(
        record(status="failed", error="The browser could not be started", finished_at=CREATED)
    )
    assert status["done"] is True
    assert status["error"] == "The browser could not be started"
    assert status["failure"] is None
    assert status["summary"] is None
    assert status["steps"] == []


def test_truncated_scrape_produces_a_warning() -> None:
    cut = step(
        3, type="paginate", name="Employees", data={"row_count": 50, "pages": 2, "truncated": True}
    )
    ok = step(
        4, type="paginate", name="Other", data={"row_count": 5, "pages": 1, "truncated": False}
    )
    status = build_status(record(status="running", steps=[cut, ok]))
    assert len(status["warnings"]) == 1
    assert "Employees" in status["warnings"][0]
    assert "2 pages" in status["warnings"][0]


def test_status_is_json_serialisable() -> None:
    log = finished_log(output_file="/x/output.xlsx")
    status = build_status(
        record(status="complete", steps=[step(1, data={"path": "/x", "pages": 3})], run_log=log)
    )
    assert json.loads(json.dumps(status)) == status


def test_status_is_a_copy_so_callers_cannot_corrupt_the_store() -> None:
    store = JobStore(clock=lambda: datetime(2026, 9, 25, 12, 0, 0))
    job_id = store.create("demo").job_id
    store.mark_running(job_id)
    store.step_finished(job_id, step(1))
    status = store.status(job_id)
    status["steps"].clear()
    assert len(store.status(job_id)["steps"]) == 1


# ---- through the API ----


@pytest.fixture
def api(tmp_path: Path) -> tuple[TestClient, JobStore, FakePageDriver]:
    (tmp_path / "recipes").mkdir()
    fake = FakePageDriver()
    store = JobStore()
    client = TestClient(
        create_app(
            recipes_dir=tmp_path / "recipes",
            jobs_dir=tmp_path / "jobs",
            driver_factory=lambda: nullcontext(fake),
            store=store,
        )
    )
    return client, store, fake


def test_api_reports_a_running_job_between_steps(
    api: tuple[TestClient, JobStore, FakePageDriver],
) -> None:
    client, store, _ = api
    job_id = store.create("demo").job_id
    store.mark_running(job_id)
    store.step_started(job_id, 2, 4, Step(type="click", name="press_go"))
    store.step_finished(job_id, step(1, type="goto", name="open_start_page"))

    status = client.get(f"/status/{job_id}").json()
    assert status["status"] == "running"
    assert status["steps_completed"] == 1
    assert status["steps_total"] == 4
    assert status["done"] is False


def test_api_full_run_status_lists_every_step_in_order(
    api: tuple[TestClient, JobStore, FakePageDriver],
    tmp_path: Path,
) -> None:
    client, _, _ = api
    (tmp_path / "recipes" / "demo.yaml").write_text(
        "url: https://x.io\nsteps:\n"
        "  - {type: fill, name: user, selector: '#u', value: bob}\n"
        "  - {type: click, name: go, selector: '#go'}\n"
        "  - {type: screenshot, name: shot}\n",
        encoding="utf-8",
    )
    job_id = client.post("/jobs", json={"recipe_name": "demo"}).json()["job_id"]
    status = client.get(f"/status/{job_id}").json()

    assert status["status"] == "complete"
    assert [s["name"] for s in status["steps"]] == ["open_start_page", "user", "go", "shot"]
    assert status["steps_total"] == 4
    assert (status["succeeded"], status["failed"]) == (4, 0)
    assert status["summary"]["success"] is True
    assert status["failure"] is None
    assert status["current_step"] is None


def test_api_failed_run_explains_which_step_failed(
    api: tuple[TestClient, JobStore, FakePageDriver],
    tmp_path: Path,
) -> None:
    client, _, fake = api
    fake.set_missing("#go")
    (tmp_path / "recipes" / "demo.yaml").write_text(
        "url: https://x.io\nsteps:\n"
        "  - {type: click, name: go, selector: '#go'}\n"
        "  - {type: click, name: never_runs, selector: '#later'}\n",
        encoding="utf-8",
    )
    job_id = client.post("/jobs", json={"recipe_name": "demo"}).json()["job_id"]
    status = client.get(f"/status/{job_id}").json()

    assert status["status"] == "failed"
    assert status["failure"]["step"] == "go"
    assert status["failure"]["step_index"] == 2
    assert "#go" in status["failure"]["error"]
    assert status["skipped"] == 1
    assert "never_runs" not in [s["name"] for s in status["steps"]]
