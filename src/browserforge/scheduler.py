"""Running schedules: an in-process APScheduler plus the glue that starts a job from a schedule.

The scheduler lives and dies with the server, so scheduled runs only happen while the
dashboard is running. A run that was due while the server was off is skipped, never replayed.
"""

from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

import structlog
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger

from browserforge.data_input import build_rows, prepare_single
from browserforge.engine.data_rows import DataError
from browserforge.engine.recipe import RecipeValidationError, load_recipe
from browserforge.jobs import (
    PENDING,
    RUNNING,
    DriverFactory,
    JobNotFoundError,
    JobStore,
    run_bulk_job,
    run_job,
)
from browserforge.recipe_files import find_recipe
from browserforge.schedule import (
    Schedule,
    ScheduleRunner,
    Starter,
    make_trigger,
    next_run_times,
)
from browserforge.schedule_store import ScheduleStore

logger = structlog.get_logger(__name__)

MISFIRE_GRACE_SECONDS = 60

TriggerFactory = Callable[[Schedule], BaseTrigger]


def cron_trigger(schedule: Schedule) -> BaseTrigger:
    return make_trigger(schedule.cron, schedule.timezone)


class SchedulerService:
    """Keeps APScheduler's jobs in step with the saved schedules."""

    def __init__(
        self,
        runner: ScheduleRunner,
        scheduler: BackgroundScheduler | None = None,
        trigger_factory: TriggerFactory = cron_trigger,
    ) -> None:
        self._runner = runner
        self._scheduler = scheduler or BackgroundScheduler()
        self._trigger_factory = trigger_factory

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def start(self, schedules: Iterable[Schedule]) -> None:
        for schedule in schedules:
            self.sync(schedule)
        self._scheduler.start()
        logger.info("scheduler_started", schedules=len(self._scheduler.get_jobs()))

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler_stopped")

    def sync(self, schedule: Schedule) -> None:
        """Make the scheduler match `schedule`: scheduled if enabled, otherwise not."""
        self.remove(schedule.id)
        if not schedule.enabled:
            return
        self._scheduler.add_job(
            self._runner.fire,
            trigger=self._trigger_factory(schedule),
            args=[schedule.id],
            id=schedule.id,
            max_instances=1,  # never two firings of one schedule at once
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )

    def remove(self, schedule_id: str) -> None:
        try:
            self._scheduler.remove_job(schedule_id)
        except JobLookupError:
            pass

    def is_scheduled(self, schedule_id: str) -> bool:
        return self._scheduler.get_job(schedule_id) is not None

    @staticmethod
    def next_run(schedule: Schedule, after: datetime | None = None) -> datetime | None:
        """When the schedule fires next (None for a paused schedule)."""
        if not schedule.enabled:
            return None
        moment = after or datetime.now().astimezone()
        upcoming = next_run_times(schedule.cron, schedule.timezone, moment, count=1)
        return upcoming[0] if upcoming else None


def make_starter(
    recipes_dir: Path,
    jobs_dir: Path,
    job_store: JobStore,
    schedule_store: ScheduleStore,
    driver_factory: DriverFactory,
) -> Starter:
    """The function the runner calls to turn a firing schedule into a job.

    Everything is read fresh each time (the recipe may have been edited, the environment
    may have changed), and every problem is reported as an error the runner records.
    """

    def start(schedule: Schedule) -> tuple[str, Callable[[], None]]:
        path = find_recipe(recipes_dir, schedule.recipe_name)
        if path is None:
            raise LookupError(f"The recipe '{schedule.recipe_name}' no longer exists.")
        try:
            recipe = load_recipe(path)
        except RecipeValidationError as exc:
            raise ValueError(
                f"The recipe '{schedule.recipe_name}' is no longer valid: {exc}"
            ) from exc

        # Check the data first, so a bad sheet or a missing environment variable is reported
        # as the reason this firing did not start, before any job exists.
        saved = schedule_store.read_data(schedule)
        rows: list[dict[str, str]] | None = None
        runnable = recipe
        secrets: list[str] = []
        try:
            if saved is not None:
                filename, raw = saved
                rows = build_rows(recipe, raw=raw, filename=filename, mapping=schedule.mapping)
            else:
                runnable, secrets = prepare_single(recipe)
        except DataError as exc:
            raise ValueError(str(exc)) from exc

        record = job_store.create(path.stem)
        job_dir = jobs_dir / record.job_id

        def run() -> None:
            if rows is not None:
                run_bulk_job(
                    job_store, record.job_id, recipe, rows, path.stem, job_dir, driver_factory
                )
            else:
                run_job(
                    job_store,
                    record.job_id,
                    runnable,
                    path.stem,
                    job_dir,
                    driver_factory,
                    secrets=secrets,
                )

        return record.job_id, run

    return start


def job_is_active(job_store: JobStore) -> Callable[[str], bool]:
    """`is_active(job_id)` for the runner: is that job still pending or running?"""

    def is_active(job_id: str) -> bool:
        try:
            return job_store.get(job_id).status in (PENDING, RUNNING)
        except JobNotFoundError:
            return False

    return is_active
