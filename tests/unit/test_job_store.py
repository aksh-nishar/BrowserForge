import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

import pytest

from browserforge.engine.driver import PageDriver
from browserforge.engine.recipe import Recipe, Step
from browserforge.engine.runner import RunLog, StepLog
from browserforge.jobs import (
    COMPLETE,
    FAILED,
    PENDING,
    RUNNING,
    DriverFactory,
    JobNotFoundError,
    JobStateError,
    JobStore,
    run_job,
)
from fixtures.fake_driver import FakePageDriver

FIXED = datetime(2026, 9, 25, 12, 0, 0)


@pytest.fixture
def store() -> JobStore:
    return JobStore(clock=lambda: FIXED)


def run_log(success: bool = True) -> RunLog:
    return RunLog(job_id="x", recipe_name="r", url="https://x.io", started_at="t", success=success)


def step_log(index: int = 1, success: bool = True) -> StepLog:
    return StepLog(index=index, type="click", name=None, success=success, error=None, duration_ms=5)


# ---- create / get ----


def test_new_job_is_pending_with_id_recipe_and_timestamp(store: JobStore) -> None:
    job = store.create("payroll")
    assert job.status == PENDING
    assert job.recipe_name == "payroll"
    assert job.created_at == "2026-09-25T12:00:00"
    assert job.started_at is None
    assert job.finished_at is None
    assert len(job.job_id) == 12


def test_job_ids_are_unique(store: JobStore) -> None:
    assert len({store.create("r").job_id for _ in range(200)}) == 200


def test_get_returns_a_snapshot_not_the_live_record(store: JobStore) -> None:
    job = store.create("r")
    job.status = "hacked"
    assert store.get(job.job_id).status == PENDING


def test_exists(store: JobStore) -> None:
    job = store.create("r")
    assert store.exists(job.job_id)
    assert not store.exists("nope")


# ---- unknown ids: a clear error, never a raw KeyError ----


def test_unknown_id_raises_job_not_found_naming_the_id(store: JobStore) -> None:
    with pytest.raises(JobNotFoundError, match="abc123") as exc:
        store.get("abc123")
    assert not isinstance(exc.value, KeyError)


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.status("nope"),
        lambda s: s.mark_running("nope"),
        lambda s: s.step_started("nope", 1, 1, Step(type="click")),
        lambda s: s.step_finished("nope", step_log()),
        lambda s: s.mark_finished("nope", run_log()),
        lambda s: s.mark_failed("nope", "boom"),
    ],
)
def test_every_operation_on_an_unknown_job_raises_job_not_found(
    store: JobStore, call: Callable[[JobStore], object]
) -> None:
    with pytest.raises(JobNotFoundError, match="nope"):
        call(store)


# ---- transitions ----


def test_pending_to_running_to_complete(store: JobStore) -> None:
    job_id = store.create("r").job_id
    store.mark_running(job_id)
    running = store.get(job_id)
    assert running.status == RUNNING
    assert running.started_at == "2026-09-25T12:00:00"

    store.mark_finished(job_id, run_log(success=True))
    done = store.get(job_id)
    assert done.status == COMPLETE
    assert done.finished_at == "2026-09-25T12:00:00"
    assert done.run_log is not None


def test_run_with_a_failed_step_ends_as_failed(store: JobStore) -> None:
    job_id = store.create("r").job_id
    store.mark_running(job_id)
    store.mark_finished(job_id, run_log(success=False))
    assert store.get(job_id).status == FAILED


def test_mark_failed_records_the_error(store: JobStore) -> None:
    job_id = store.create("r").job_id
    store.mark_running(job_id)
    store.mark_failed(job_id, "browser did not start")
    job = store.get(job_id)
    assert job.status == FAILED
    assert job.error == "browser did not start"
    assert job.finished_at is not None


def test_cannot_start_a_job_twice(store: JobStore) -> None:
    job_id = store.create("r").job_id
    store.mark_running(job_id)
    with pytest.raises(JobStateError, match="running"):
        store.mark_running(job_id)


def test_cannot_finish_a_job_that_never_started(store: JobStore) -> None:
    job_id = store.create("r").job_id
    with pytest.raises(JobStateError, match="pending"):
        store.mark_finished(job_id, run_log())
    with pytest.raises(JobStateError):
        store.mark_failed(job_id, "x")


def test_a_finished_job_cannot_change_again(store: JobStore) -> None:
    job_id = store.create("r").job_id
    store.mark_running(job_id)
    store.mark_finished(job_id, run_log())
    with pytest.raises(JobStateError, match="complete"):
        store.mark_failed(job_id, "late error")
    with pytest.raises(JobStateError):
        store.mark_running(job_id)


# ---- progress ----


def test_step_progress_tracks_current_step_and_finished_steps(store: JobStore) -> None:
    job_id = store.create("r").job_id
    store.mark_running(job_id)
    store.step_started(job_id, 1, 4, Step(type="fill", name="user"))
    mid = store.get(job_id)
    assert mid.steps_total == 4
    assert mid.current_step == {"index": 1, "name": "user", "type": "fill"}

    store.step_finished(job_id, step_log(1))
    after = store.get(job_id)
    assert after.current_step is None
    assert [s.index for s in after.steps] == [1]


def test_unnamed_step_is_shown_by_its_type(store: JobStore) -> None:
    job_id = store.create("r").job_id
    store.step_started(job_id, 2, 3, Step(type="click"))
    current = store.get(job_id).current_step
    assert current is not None
    assert current["name"] == "click"


# ---- thread safety ----


def test_concurrent_updates_do_not_lose_steps(store: JobStore) -> None:
    job_id = store.create("r").job_id
    store.mark_running(job_id)

    def worker(base: int) -> None:
        for offset in range(50):
            store.step_finished(job_id, step_log(base + offset))
            store.status(job_id)  # readers run concurrently with writers

    threads = [threading.Thread(target=worker, args=(n * 100,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store.get(job_id).steps) == 400


# ---- run_job (the background task body) ----


def one_click_recipe() -> Recipe:
    return Recipe(url="https://x.io", steps=[Step(type="click", config={"selector": "#go"})])


def factory_for(fake: PageDriver) -> DriverFactory:
    return lambda: nullcontext(fake)


def test_run_job_updates_the_store_through_to_complete(store: JobStore, tmp_path: Path) -> None:
    job_id = store.create("demo").job_id
    fake = FakePageDriver()
    run_job(store, job_id, one_click_recipe(), "demo", tmp_path / job_id, factory_for(fake))

    job = store.get(job_id)
    assert job.status == COMPLETE
    assert job.steps_total == 2  # implicit start-page step + the click
    assert [s.type for s in job.steps] == ["goto", "click"]
    assert job.current_step is None
    assert fake.clicks == ["#go"]
    assert (tmp_path / job_id / "run_log.json").is_file()


def test_run_job_with_a_failing_step_ends_as_failed(store: JobStore, tmp_path: Path) -> None:
    job_id = store.create("demo").job_id
    fake = FakePageDriver()
    fake.set_missing("#go")
    run_job(store, job_id, one_click_recipe(), "demo", tmp_path / job_id, factory_for(fake))
    assert store.get(job_id).status == FAILED


def test_run_job_never_raises_when_the_browser_cannot_start(
    store: JobStore, tmp_path: Path
) -> None:
    job_id = store.create("demo").job_id

    def broken_factory() -> AbstractContextManager[PageDriver]:
        raise RuntimeError("chromium missing")

    run_job(store, job_id, one_click_recipe(), "demo", tmp_path / job_id, broken_factory)
    job = store.get(job_id)
    assert job.status == FAILED
    assert job.error is not None
    assert "chromium missing" in job.error


def test_run_job_closes_the_driver_context_even_after_a_crash(
    store: JobStore, tmp_path: Path
) -> None:
    events: list[str] = []

    class ExplodingDriver(FakePageDriver):
        def goto(self, url: str) -> None:
            raise RuntimeError("driver bug")  # an unexpected error, not a DriverError

    @contextmanager
    def factory() -> Iterator[PageDriver]:
        events.append("open")
        try:
            yield ExplodingDriver()
        finally:
            events.append("close")

    job_id = store.create("demo").job_id
    run_job(store, job_id, one_click_recipe(), "demo", tmp_path / job_id, factory)
    assert events == ["open", "close"]
    assert store.get(job_id).status == FAILED  # the runner contains the bug as a failed step


def test_run_job_for_an_unknown_job_does_not_raise(store: JobStore, tmp_path: Path) -> None:
    run_job(
        store,
        "ghost",
        one_click_recipe(),
        "demo",
        tmp_path / "ghost",
        factory_for(FakePageDriver()),
    )
