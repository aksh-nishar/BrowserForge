"""Bulk runs in the job store, the status JSON, and the background job body."""

from contextlib import nullcontext
from pathlib import Path
from typing import Any

from browserforge.engine.bulk import BulkResult, RowResult
from browserforge.engine.driver import DriverError
from browserforge.engine.recipe import Recipe, Step
from browserforge.jobs import (
    COMPLETE,
    FAILED,
    JobStore,
    build_bulk_status,
    build_status,
    run_bulk_job,
    run_job,
)
from fixtures.fake_driver import FakePageDriver


def row(number: int, success: bool = True, **kwargs: Any) -> RowResult:
    return RowResult(
        row=number,
        label=kwargs.pop("label", f"person {number}"),
        success=success,
        duration_ms=10,
        folder=f"row_{number:04d}",
        failed_step=None if success else "go",
        error=None if success else "Couldn't find '#bad' on the page.",
    )


def bulk_result(rows: list[RowResult]) -> BulkResult:
    return BulkResult(
        job_id="j",
        recipe_name="demo",
        started_at="t0",
        total=len(rows),
        finished_at="t1",
        duration_ms=500,
        success=all(r.success for r in rows),
        output_file="/x/jobs/j/output.xlsx",
        rows=rows,
    )


def running_bulk_job(store: JobStore) -> str:
    job = store.create("demo")
    store.mark_running(job.job_id)
    return job.job_id


# ---- store ----


def test_create_records_the_dry_run_flag() -> None:
    store = JobStore()
    assert store.create("demo").dry_run is False
    assert store.create("demo", dry_run=True).dry_run is True


def test_a_single_run_has_no_bulk_status() -> None:
    store = JobStore()
    job = store.create("demo")
    assert store.status(job.job_id)["bulk"] is None
    assert build_bulk_status(store.get(job.job_id)) is None


def test_row_progress_is_tracked() -> None:
    store = JobStore()
    job_id = running_bulk_job(store)

    store.row_started(job_id, 1, 3, "person 1")
    mid = store.status(job_id)["bulk"]
    assert mid["rows_total"] == 3
    assert mid["rows_done"] == 0
    assert mid["current_row"] == {"index": 1, "label": "person 1"}

    store.row_finished(job_id, row(1))
    store.row_started(job_id, 2, 3, "person 2")
    store.row_finished(job_id, row(2, success=False))
    later = store.status(job_id)["bulk"]
    assert later["rows_done"] == 2
    assert later["rows_failed"] == 1
    assert later["current_row"] is None
    assert later["failed_rows"] == [
        {
            "row": 2,
            "label": "person 2",
            "step": "go",
            "error": "Couldn't find '#bad' on the page.",
        }
    ]


def test_only_the_first_failed_rows_are_listed() -> None:
    store = JobStore()
    job_id = running_bulk_job(store)
    store.row_started(job_id, 1, 80, "x")
    for number in range(1, 81):
        store.row_finished(job_id, row(number, success=False))
    bulk = store.status(job_id)["bulk"]
    assert bulk["rows_failed"] == 80
    assert len(bulk["failed_rows"]) == 50


def test_a_bulk_job_with_every_row_ok_completes() -> None:
    store = JobStore()
    job_id = running_bulk_job(store)
    store.row_started(job_id, 1, 2, "a")
    store.mark_bulk_finished(job_id, bulk_result([row(1), row(2)]))
    status = store.status(job_id)
    assert status["status"] == COMPLETE
    assert status["done"] is True
    assert status["summary"] == {
        "success": True,
        "duration_ms": 500,
        "steps_run": 0,
        "skipped": 0,
        "rows_total": 2,
        "rows_failed": 0,
        "output_file": "output.xlsx",
    }


def test_a_bulk_job_with_a_failed_row_ends_failed() -> None:
    store = JobStore()
    job_id = running_bulk_job(store)
    store.mark_bulk_finished(job_id, bulk_result([row(1), row(2, success=False)]))
    status = store.status(job_id)
    assert status["status"] == FAILED
    assert status["summary"]["rows_failed"] == 1


def test_a_bulk_job_that_could_not_run_reports_its_error() -> None:
    store = JobStore()
    job_id = running_bulk_job(store)
    store.row_started(job_id, 1, 2, "a")
    store.mark_failed(job_id, "The browser is not installed.")
    status = store.status(job_id)
    assert status["status"] == FAILED
    assert status["error"] == "The browser is not installed."
    assert status["bulk"]["current_row"] is None


def test_status_of_a_dry_run_says_so() -> None:
    store = JobStore()
    job = store.create("demo", dry_run=True)
    assert build_status(store.get(job.job_id))["dry_run"] is True


# ---- job bodies ----


def recipe() -> Recipe:
    return Recipe(
        url="https://x.io",
        steps=[
            Step(type="fill", name="name", config={"selector": "#name", "value": "{{name}}"}),
            Step(type="click", name="go", config={"selector": "#go"}),
        ],
    )


def test_run_bulk_job_runs_every_row_and_completes(tmp_path: Path) -> None:
    store = JobStore()
    job = store.create("demo")
    fake = FakePageDriver()
    run_bulk_job(
        store,
        job.job_id,
        recipe(),
        [{"name": "Ann"}, {"name": "Bob"}],
        "demo",
        tmp_path / job.job_id,
        lambda: nullcontext(fake),
        environ={},
    )
    status = store.status(job.job_id)
    assert status["status"] == COMPLETE
    assert status["bulk"]["rows_done"] == 2
    assert fake.calls_to("fill") == [("#name", "Ann"), ("#name", "Bob")]


def test_run_bulk_job_with_a_failing_row_ends_failed_with_the_reason(tmp_path: Path) -> None:
    store = JobStore()
    job = store.create("demo")
    fake = FakePageDriver()
    fake.set_missing("#go")
    run_bulk_job(
        store,
        job.job_id,
        recipe(),
        [{"name": "Ann"}],
        "demo",
        tmp_path / job.job_id,
        lambda: nullcontext(fake),
        environ={},
    )
    status = store.status(job.job_id)
    assert status["status"] == FAILED
    assert status["bulk"]["failed_rows"][0]["step"] == "go"


def test_run_bulk_job_survives_a_browser_that_will_not_start(tmp_path: Path) -> None:
    store = JobStore()
    job = store.create("demo")

    def broken() -> Any:
        raise DriverError("net::ERR_INTERNET_DISCONNECTED")

    run_bulk_job(store, job.job_id, recipe(), [{"name": "Ann"}], "demo", tmp_path / "j", broken)
    status = store.status(job.job_id)
    assert status["status"] == FAILED
    assert "no internet" in status["error"].lower()


def test_run_job_passes_secrets_and_dry_run_through(tmp_path: Path) -> None:
    store = JobStore()
    job = store.create("demo", dry_run=True)
    rec = Recipe(
        url="https://x.io",
        steps=[Step(type="click", name="send", config={"selector": "#send", "submit": True})],
    )
    fake = FakePageDriver()
    run_job(
        store,
        job.job_id,
        rec,
        "demo",
        tmp_path / job.job_id,
        lambda: nullcontext(fake),
        secrets=["x"],
        dry_run=True,
    )
    assert store.status(job.job_id)["status"] == COMPLETE
    assert fake.clicks == []
