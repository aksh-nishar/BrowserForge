"""In-memory job tracking and the background job body.

JobStore is thread-safe: the job runs in a worker thread while the API reads its status.
"""

import copy
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import bound_contextvars

from browserforge.engine.bulk import BulkResult, RowResult, run_bulk
from browserforge.engine.driver import DriverError, PageDriver
from browserforge.engine.errors import classify, humanize
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
    dry_run: bool = False  # a test run: stops before the step marked `submit: true`
    rows_total: int | None = None  # set for bulk runs (one recipe run per data row)
    current_row: dict[str, Any] | None = None
    row_results: list[RowResult] = field(default_factory=list)
    bulk_result: BulkResult | None = None


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

    def create(self, recipe_name: str, *, dry_run: bool = False) -> JobRecord:
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            record = JobRecord(
                job_id=job_id,
                recipe_name=recipe_name,
                created_at=self._stamp(),
                dry_run=dry_run,
            )
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

    def row_started(self, job_id: str, index: int, total: int, label: str) -> None:
        with self._lock:
            record = self._get(job_id)
            record.rows_total = total
            record.current_row = {"index": index, "label": label}

    def row_finished(self, job_id: str, row_result: RowResult) -> None:
        with self._lock:
            record = self._get(job_id)
            record.row_results.append(row_result)
            record.current_row = None
            record.current_step = None

    def mark_bulk_finished(self, job_id: str, bulk_result: BulkResult) -> None:
        """A bulk run ended: complete if every row succeeded, otherwise failed."""
        with self._lock:
            record = self._get(job_id)
            self._require_running(record)
            record.bulk_result = bulk_result
            record.current_row = None
            record.current_step = None
            record.status = COMPLETE if bulk_result.success else FAILED
            record.finished_at = self._stamp()

    def mark_failed(self, job_id: str, error: str) -> None:
        """The job could not run at all (for example the browser failed to start)."""
        with self._lock:
            record = self._get(job_id)
            self._require_running(record)
            record.error = error
            record.current_step = None
            record.current_row = None
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
    """What the dashboard shows: the plain-language message (raw text stays in run_log.json)."""
    return step.human_error if step.human_error is not None else step.error


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
    bulk = build_bulk_status(record)
    if done and record.bulk_result is not None:
        bulk_result = record.bulk_result
        summary = {
            "success": bulk_result.success,
            "duration_ms": bulk_result.duration_ms,
            "steps_run": 0,
            "skipped": 0,
            "rows_total": bulk_result.total,
            "rows_failed": bulk_result.failed,
            "output_file": Path(bulk_result.output_file).name if bulk_result.output_file else None,
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
        "dry_run": record.dry_run,
        "bulk": bulk,
    }


MAX_FAILED_ROWS_SHOWN = 50


def build_bulk_status(record: JobRecord) -> dict[str, Any] | None:
    """Row-level progress of a bulk run (None for an ordinary single run)."""
    if record.rows_total is None:
        return None
    failed = [row for row in record.row_results if not row.success]
    return {
        "rows_total": record.rows_total,
        "rows_done": len(record.row_results),
        "rows_failed": len(failed),
        "current_row": record.current_row,
        "failed_rows": [
            {"row": row.row, "label": row.label, "step": row.failed_step, "error": row.error}
            for row in failed[:MAX_FAILED_ROWS_SHOWN]
        ],
    }


def crash_message(exc: Exception) -> str:
    """Plain-language reason a job could not run at all. Never includes a stack trace."""
    if "Executable doesn't exist" in str(exc):
        return (
            "The browser is not installed on this computer. Run: uv run playwright install chromium"
        )
    if isinstance(exc, DriverError):
        return humanize(classify(exc), str(exc))
    return (
        "The job could not be started because of an unexpected problem. "
        "The details are in the server log."
    )


def run_job(
    store: JobStore,
    job_id: str,
    recipe: Recipe,
    recipe_name: str,
    job_dir: Path,
    driver_factory: DriverFactory,
    *,
    secrets: Sequence[str] = (),
    dry_run: bool = False,
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
                    secrets=secrets,
                    dry_run=dry_run,
                )
        except Exception as exc:
            _fail_job(store, job_id, exc)
            return
        store.mark_finished(job_id, run_log)


def _fail_job(store: JobStore, job_id: str, exc: Exception) -> None:
    logger.exception("job_crashed")
    try:
        store.mark_failed(job_id, crash_message(exc))
    except (JobNotFoundError, JobStateError):
        logger.exception("job_state_update_failed")


def run_bulk_job(
    store: JobStore,
    job_id: str,
    recipe: Recipe,
    rows: Sequence[Mapping[str, str]],
    recipe_name: str,
    job_dir: Path,
    driver_factory: DriverFactory,
    *,
    environ: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> None:
    """Background task for a bulk run: one recipe run per data row. Never raises."""
    with bound_contextvars(job_id=job_id, recipe=recipe_name):
        try:
            store.mark_running(job_id)
            result = run_bulk(
                recipe,
                rows,
                driver_factory,
                job_dir,
                job_id=job_id,
                recipe_name=recipe_name,
                environ=environ,
                dry_run=dry_run,
                on_row_start=partial(store.row_started, job_id),
                on_row_end=partial(store.row_finished, job_id),
                on_step_start=partial(store.step_started, job_id),
            )
        except Exception as exc:
            _fail_job(store, job_id, exc)
            return
        store.mark_bulk_finished(job_id, result)
