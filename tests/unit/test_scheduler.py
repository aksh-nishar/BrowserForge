"""The APScheduler wiring and the glue that turns a firing schedule into a job."""

import threading
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from browserforge.jobs import JobStore
from browserforge.schedule import Schedule, ScheduleRunner, next_run_times
from browserforge.schedule_store import ScheduleStore
from browserforge.scheduler import SchedulerService, job_is_active, make_starter
from fixtures.fake_driver import FakePageDriver

PLAIN = "url: https://x.io\nsteps:\n  - type: click\n    selector: '#go'\n"
FORM = 'url: https://x.io\nsteps:\n  - type: fill\n    selector: "#e"\n    value: "{{email}}"\n'
SECRET = (
    'url: https://x.io\nsteps:\n  - type: fill\n    selector: "#p"\n    value: "{{env:SCHED_PW}}"\n'
)


def make(**changes: Any) -> Schedule:
    base = Schedule(id="abc12345", recipe_name="plain", when="daily 08:00", cron="0 8 * * *")
    return replace(base, **changes)


def idle_runner() -> ScheduleRunner:
    return ScheduleRunner(
        get=lambda _id: None,
        update=lambda _id, **_c: None,
        starter=lambda _s: ("j", lambda: None),
        is_active=lambda _j: False,
    )


# ---- SchedulerService ----


def test_an_enabled_schedule_becomes_a_scheduler_job() -> None:
    service = SchedulerService(idle_runner())
    service.sync(make())
    assert service.is_scheduled("abc12345")


def test_the_job_never_overlaps_itself_and_skips_missed_runs() -> None:
    scheduler = BackgroundScheduler()
    service = SchedulerService(idle_runner(), scheduler)
    service.sync(make())
    job = scheduler.get_job("abc12345")
    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.misfire_grace_time == 60
    assert list(job.args) == ["abc12345"]


def test_a_paused_schedule_is_not_scheduled() -> None:
    service = SchedulerService(idle_runner())
    service.sync(make(enabled=False))
    assert not service.is_scheduled("abc12345")


def test_syncing_again_replaces_the_job_instead_of_adding_a_second() -> None:
    scheduler = BackgroundScheduler()
    service = SchedulerService(idle_runner(), scheduler)
    service.sync(make())
    service.sync(make(cron="0 9 * * *"))
    assert len(scheduler.get_jobs()) == 1


def test_pausing_removes_the_job_and_resuming_adds_it_back() -> None:
    service = SchedulerService(idle_runner())
    service.sync(make())
    service.sync(make(enabled=False))
    assert not service.is_scheduled("abc12345")
    service.sync(make(enabled=True))
    assert service.is_scheduled("abc12345")


def test_removing_an_unknown_schedule_is_harmless() -> None:
    SchedulerService(idle_runner()).remove("never-existed")


def test_next_run_is_the_next_cron_time_and_none_when_paused() -> None:
    after = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    schedule = make(timezone="UTC")
    assert (
        SchedulerService.next_run(schedule, after)
        == next_run_times("0 8 * * *", "UTC", after, 1)[0]
    )
    assert SchedulerService.next_run(make(enabled=False), after) is None


def test_start_registers_every_schedule_and_shutdown_stops_the_scheduler() -> None:
    service = SchedulerService(idle_runner())
    service.start([make(id="aaaa1111"), make(id="bbbb2222", enabled=False), make(id="cccc3333")])
    try:
        assert service.running
        assert service.is_scheduled("aaaa1111")
        assert not service.is_scheduled("bbbb2222")
        assert service.is_scheduled("cccc3333")
    finally:
        service.shutdown()
    assert not service.running


def test_shutdown_before_start_is_harmless() -> None:
    SchedulerService(idle_runner()).shutdown()


def test_a_real_scheduler_thread_fires_the_schedule_and_runs_the_job() -> None:
    """End to end through APScheduler's own thread, with a 1-second trigger instead of cron."""
    ran = threading.Event()
    schedules = {"abc12345": make()}

    def starter(_schedule: Schedule) -> tuple[str, Any]:
        return "job1", ran.set

    def update(schedule_id: str, **changes: Any) -> Schedule:
        schedules[schedule_id] = replace(schedules[schedule_id], **changes)
        return schedules[schedule_id]

    runner = ScheduleRunner(schedules.get, update, starter, lambda _j: False)
    service = SchedulerService(runner, trigger_factory=lambda _s: IntervalTrigger(seconds=1))
    service.start(schedules.values())
    try:
        assert ran.wait(timeout=10), "the schedule never fired"
    finally:
        service.shutdown()
    assert schedules["abc12345"].last_job_id == "job1"


# ---- make_starter ----


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.recipes = tmp_path / "recipes"
        self.jobs = tmp_path / "jobs"
        self.recipes.mkdir()
        self.fake = FakePageDriver()
        self.job_store = JobStore()
        self.schedules = ScheduleStore(tmp_path / "data")
        self.start = make_starter(
            self.recipes,
            self.jobs,
            self.job_store,
            self.schedules,
            lambda: nullcontext(self.fake),
        )

    def recipe(self, name: str, text: str) -> None:
        (self.recipes / f"{name}.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.recipe("plain", PLAIN)
    return w


def test_a_plain_recipe_is_started_as_a_pending_job_and_run_on_demand(world: World) -> None:
    job_id, run = world.start(make())
    assert world.job_store.get(job_id).status == "pending"  # created, not yet run
    assert world.fake.calls == []

    run()
    assert world.job_store.get(job_id).status == "complete"
    assert world.fake.clicks == ["#go"]
    assert (world.jobs / job_id / "run_log.json").is_file()


def test_a_saved_data_sheet_makes_it_a_bulk_run(world: World) -> None:
    world.recipe("form", FORM)
    saved = world.schedules.add(
        make(recipe_name="form"), data=("people.csv", b"email\na@x.io\nb@x.io\n")
    )
    job_id, run = world.start(saved)
    run()

    status = world.job_store.status(job_id)
    assert status["status"] == "complete"
    assert status["bulk"]["rows_done"] == 2
    assert world.fake.calls_to("fill") == [("#e", "a@x.io"), ("#e", "b@x.io")]


def test_a_saved_column_mapping_is_applied(world: World) -> None:
    world.recipe("form", FORM)
    saved = world.schedules.add(
        make(recipe_name="form", mapping={"email": "mail address"}),
        data=("people.csv", b"mail address\nz@x.io\n"),
    )
    world.start(saved)[1]()
    assert world.fake.fills == {"#e": "z@x.io"}


def test_a_deleted_recipe_is_reported_and_no_job_is_created(world: World) -> None:
    with pytest.raises(LookupError, match="no longer exists"):
        world.start(make(recipe_name="gone"))
    assert not world.jobs.exists()


def test_a_recipe_that_became_invalid_is_reported(world: World) -> None:
    world.recipe("broken", "steps: []\n")
    with pytest.raises(ValueError, match="no longer valid"):
        world.start(make(recipe_name="broken"))


def test_a_recipe_that_needs_data_but_has_none_saved_is_reported(world: World) -> None:
    world.recipe("form", FORM)
    with pytest.raises(ValueError, match=r"\{\{email\}\}"):
        world.start(make(recipe_name="form"))
    assert world.fake.calls == []


def test_a_bad_saved_sheet_is_reported_before_any_job_exists(world: World) -> None:
    world.recipe("form", FORM)
    saved = world.schedules.add(make(recipe_name="form"), data=("p.csv", b"email\n\n"))
    with pytest.raises(ValueError, match="no rows"):
        world.start(saved)
    assert not world.jobs.exists()


def test_environment_values_are_read_when_the_schedule_fires(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.recipe("secret", SECRET)
    monkeypatch.setenv("SCHED_PW", "letmein99")
    world.start(make(recipe_name="secret"))[1]()
    assert world.fake.fills == {"#p": "letmein99"}


def test_a_missing_environment_value_stops_the_firing_with_a_clear_reason(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.recipe("secret", SECRET)
    monkeypatch.delenv("SCHED_PW", raising=False)
    with pytest.raises(ValueError, match="SCHED_PW"):
        world.start(make(recipe_name="secret"))


def test_the_recipe_is_read_fresh_at_every_firing(world: World) -> None:
    world.start(make())[1]()
    world.recipe("plain", PLAIN.replace("#go", "#stop"))
    world.start(make())[1]()
    assert world.fake.clicks == ["#go", "#stop"]


# ---- job_is_active ----


def test_pending_and_running_jobs_are_active_finished_and_unknown_are_not() -> None:
    store = JobStore()
    is_active = job_is_active(store)

    pending = store.create("demo").job_id
    assert is_active(pending) is True

    running = store.create("demo").job_id
    store.mark_running(running)
    assert is_active(running) is True

    failed = store.create("demo").job_id
    store.mark_running(failed)
    store.mark_failed(failed, "x")
    assert is_active(failed) is False

    assert is_active("unknown") is False
