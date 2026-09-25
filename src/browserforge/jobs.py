"""In-memory job tracking and the background job body.

JobStore is thread-safe: the job runs in a worker thread while the API reads its status.
"""

import copy
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import bound_contextvars

from browserforge.engine.driver import PageDriver
from browserforge.engine.recipe import Recipe, Step
from browserforge.engine.runner import RunLog, StepLog, run_recipe

logger = structlog.get_logger(__name__)

PENDING = "pending"
RUNNING = "running"
COMPLETE = "complete"
FAILED = "failed"
FINISHED_STATUSES = frozenset({COMPLETE, FAILED})

DriverFactory = Callable[[], AbstractContextManager[PageDriver]]


class JobNotFoundError(Exception):
    """No job with that id exists (maps to a 404 in the API)."""


class JobStateError(Exception):
    """A status change that is not allowed from the job's current state."""


@dataclass
class JobRecord:
    job_id: str
    recipe_name: str
    created_at: str
    status: str = PENDING
    started_at: str | None = None
    finished_at: str | None = None
    steps_total: int | None = None
    current_step: dict[str, Any] | None = None
    steps: list[StepLog] = field(default_factory=list)
    run_log: RunLog | None = None
    error: str | None = None


class JobStore:
    def __init__(self, clock: Callable[[], datetime] = datetime.now) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def _stamp(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _get(self, job_id: str) -> JobRecord:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise JobNotFoundError(f"Job '{job_id}' not found") from None

    def create(self, recipe_name: str) -> JobRecord:
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            record = JobRecord(job_id=job_id, recipe_name=recipe_name, created_at=self._stamp())
            self._jobs[job_id] = record
            return copy.deepcopy(record)

    def get(self, job_id: str) -> JobRecord:
        """A snapshot copy of the job (safe to read while the job keeps running)."""
        with self._lock:
            return copy.deepcopy(self._get(job_id))

    def exists(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._jobs

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return build_status(self._get(job_id))

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            record = self._get(job_id)
            if record.status != PENDING:
                raise JobStateError(f"Job '{job_id}' is {record.status}, cannot start it")
            record.status = RUNNING
            record.started_at = self._stamp()

    def step_started(self, job_id: str, index: int, total: int, step: Step) -> None:
        with self._lock:
            record = self._get(job_id)
            record.steps_total = total
            record.current_step = {
                "index": index,
                "name": step.name or step.type,
                "type": step.type,
            }

    def step_finished(self, job_id: str, step_log: StepLog) -> None:
        with self._lock:
            record = self._get(job_id)
            record.steps.append(step_log)
            record.current_step = None

    def mark_finished(self, job_id: str, run_log: RunLog) -> None:
        """The run ended: complete if every step succeeded, otherwise failed."""
        with self._lock:
            record = self._get(job_id)
            self._require_running(record)
            record.run_log = run_log
            record.current_step = None
            record.status = COMPLETE if run_log.success else FAILED
            record.finished_at = self._stamp()

    def mark_failed(self, job_id: str, error: str) -> None:
        """The job could not run at all (for example the browser failed to start)."""
        with self._lock:
            record = self._get(job_id)
            self._require_running(record)
            record.error = error
            record.current_step = None
            record.status = FAILED
            record.finished_at = self._stamp()

    @staticmethod
    def _require_running(record: JobRecord) -> None:
        if record.status != RUNNING:
            raise JobStateError(
                f"Job '{record.job_id}' is {record.status}, expected it to be running"
            )


def _step_name(step: StepLog) -> str:
    return step.name or step.type


def _step_message(step: StepLog) -> str | None:
    return step.error


def build_status(record: JobRecord) -> dict[str, Any]:
    """The JSON the dashboard polls. Pure: a JobRecord in, plain data out."""
    steps = [
        {
            "index": step.index,
            "type": step.type,
            "name": _step_name(step),
            "success": step.success,
            "error": _step_message(step),
            "duration_ms": step.duration_ms,
        }
        for step in record.steps
    ]
    failed_steps = [step for step in record.steps if not step.success]
    done = record.status in FINISHED_STATUSES

    failure = None
    if failed_steps:
        first = failed_steps[0]
        failure = {
            "step_index": first.index,
            "step": _step_name(first),
            "error": _step_message(first),
        }

    warnings = [
        f"'{_step_name(step)}' was cut off after {step.data.get('pages')} pages "
        "(the page limit was reached), so the data may be incomplete."
        for step in record.steps
        if step.data.get("truncated")
    ]

    summary = None
    if done and record.run_log is not None:
        run_log = record.run_log
        summary = {
            "success": run_log.success,
            "duration_ms": run_log.duration_ms,
            "steps_run": len(run_log.steps),
            "skipped": run_log.skipped_steps,
            "output_file": Path(run_log.output_file).name if run_log.output_file else None,
        }

    return {
        "job_id": record.job_id,
        "recipe_name": record.recipe_name,
        "status": record.status,
        "done": done,
        "created_at": record.created_at,
        "finished_at": record.finished_at,
        "current_step": record.current_step,
        "steps_total": record.steps_total,
        "steps_completed": len(record.steps),
        "succeeded": len(record.steps) - len(failed_steps),
        "failed": len(failed_steps),
        "skipped": record.run_log.skipped_steps if record.run_log else 0,
        "steps": steps,
        "failure": failure,
        "warnings": warnings,
        "summary": summary,
        "error": record.error,
    }


def _crash_message(exc: Exception) -> str:
    return f"The job could not be run ({type(exc).__name__}): {exc}"


def run_job(
    store: JobStore,
    job_id: str,
    recipe: Recipe,
    recipe_name: str,
    job_dir: Path,
    driver_factory: DriverFactory,
) -> None:
    """Body of the background task. Runs the recipe, keeping the store up to date. Never raises."""
    with bound_contextvars(job_id=job_id, recipe=recipe_name):
        try:
            store.mark_running(job_id)
            with driver_factory() as driver:
                run_log = run_recipe(
                    recipe,
                    driver,
                    job_dir,
                    job_id=job_id,
                    recipe_name=recipe_name,
                    on_step_start=partial(store.step_started, job_id),
                    on_step_end=partial(store.step_finished, job_id),
                )
        except Exception as exc:
            logger.exception("job_crashed")
            try:
                store.mark_failed(job_id, _crash_message(exc))
            except (JobNotFoundError, JobStateError):
                logger.exception("job_state_update_failed")
            return
        store.mark_finished(job_id, run_log)
