"""Runner: execute a Recipe step by step against a PageDriver and record what happened.

Order of execution: open the recipe URL, run the `login:` block (if any), then each step.
Every step yields an ActionResult; the runner logs it, decides whether to continue, and at
the end writes `run_log.json` (audit trail) and `output.xlsx` (if any step extracted a table).
"""

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import bound_contextvars

from browserforge.engine.actions import (
    ActionContext,
    ActionResult,
    do_check,
    do_click,
    do_download,
    do_extract_table,
    do_fill,
    do_goto,
    do_login,
    do_paginate,
    do_screenshot,
    do_select_option,
    do_upload,
    do_wait_for_selector,
    screenshot_path,
)
from browserforge.engine.driver import PageDriver
from browserforge.engine.errors import RETRYABLE_KINDS, humanize
from browserforge.engine.excel_export import build_workbook, save_workbook
from browserforge.engine.recipe import Recipe, Step

logger = structlog.get_logger(__name__)

Handler = Callable[[PageDriver, Step, ActionContext], ActionResult]


def _plain(action: Callable[[PageDriver, Step], ActionResult]) -> Handler:
    """Adapt an action that needs no ActionContext to the uniform handler signature."""
    return lambda driver, step, _ctx: action(driver, step)


HANDLERS: dict[str, Handler] = {
    "goto": _plain(do_goto),
    "fill": _plain(do_fill),
    "click": _plain(do_click),
    "select_option": _plain(do_select_option),
    "check": _plain(do_check),
    "login": _plain(do_login),
    "upload": _plain(do_upload),
    "download": do_download,
    "screenshot": do_screenshot,
    "extract_table": _plain(do_extract_table),
    "paginate": _plain(do_paginate),
    "wait_for_selector": _plain(do_wait_for_selector),
}


@dataclass
class StepLog:
    index: int
    type: str
    name: str | None
    success: bool
    error: str | None
    duration_ms: int
    data: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    retry_errors: list[str] = field(default_factory=list)  # raw error of each failed earlier try
    error_kind: str | None = None  # see engine.errors.ErrorKind
    human_error: str | None = None  # plain-language version of `error`, shown in the dashboard
    failure_screenshot: str | None = None  # taken automatically when the step failed


@dataclass
class RunLog:
    job_id: str
    recipe_name: str | None
    url: str
    started_at: str
    finished_at: str = ""
    duration_ms: int = 0
    success: bool = False
    stopped_early: bool = False
    skipped_steps: int = 0
    output_file: str | None = None
    steps: list[StepLog] = field(default_factory=list)


def _planned_steps(recipe: Recipe) -> list[Step]:
    steps = [Step(type="goto", name="open_start_page", config={"url": recipe.url})]
    if recipe.login is not None:
        steps.append(Step(type="login", name="login", config=dict(recipe.login)))
    steps.extend(recipe.steps)
    return steps


def execute_step(driver: PageDriver, step: Step, ctx: ActionContext) -> ActionResult:
    """Run one step. Never raises: unknown types and unexpected errors become failed results."""
    label = f"Step '{step.name or step.type}'"
    handler = HANDLERS.get(step.type)
    if handler is None:
        return ActionResult(success=False, error=f"{label}: unknown step type '{step.type}'")
    try:
        return handler(driver, step, ctx)
    except Exception as exc:  # a bug in an action must not abort the whole run
        logger.exception("step_crashed", step_type=step.type)
        return ActionResult(
            success=False,
            error=f"{label}: unexpected error ({type(exc).__name__}): {exc}",
            error_kind="unexpected",
        )


def _run_with_retries(
    driver: PageDriver,
    step: Step,
    ctx: ActionContext,
    retries: int,
    backoff_seconds: float,
    sleep: Callable[[float], None],
) -> tuple[ActionResult, int, list[str]]:
    """Run a step, retrying transient failures up to `retries` extra times.

    Waits backoff_seconds * 2**(n-1) before retry n. Only failures worth repeating are
    retried (see RETRYABLE_KINDS); bad config, wrong passwords and bugs fail at once.
    Returns (final result, attempts used, error text of each failed attempt before the last).
    """
    earlier_errors: list[str] = []
    attempt = 1
    while True:
        result = execute_step(driver, step, ctx)
        retryable = result.error_kind in RETRYABLE_KINDS
        if result.success or not retryable or attempt > retries:
            return result, attempt, earlier_errors
        earlier_errors.append(result.error or "")
        logger.warning(
            "step_retry",
            step_type=step.type,
            attempt=attempt,
            max_attempts=retries + 1,
            error=result.error,
        )
        sleep(backoff_seconds * 2 ** (attempt - 1))
        attempt += 1


def _loggable_data(data: dict[str, Any]) -> dict[str, Any]:
    """Step data for run_log.json: table rows are summarised as a count, not embedded."""
    return {
        ("row_count" if key == "rows" else key): (len(value) if key == "rows" else value)
        for key, value in data.items()
    }


def _failure_screenshot(driver: PageDriver, step: Step, ctx: ActionContext) -> str | None:
    """Best-effort screenshot of the page as it was when a step failed.

    Never raises and never hides the original error: if the screenshot itself fails
    (browser gone, disk full, ...) that is logged as a secondary problem and ignored.
    """
    path = screenshot_path(ctx, step, prefix="failure_")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        driver.screenshot(str(path))
    except Exception as exc:
        logger.warning("failure_screenshot_failed", step_type=step.type, error=str(exc))
        return None
    logger.info("failure_screenshot", step_type=step.type, path=str(path))
    return str(path)


def _notify(callback: Callable[..., None] | None, *args: Any) -> None:
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:  # progress reporting must never break a run
        logger.exception("progress_callback_failed")


def _write_run_log(job_dir: Path, run_log: RunLog) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(asdict(run_log), indent=2, ensure_ascii=False, default=str)
    (job_dir / "run_log.json").write_text(text, encoding="utf-8")


def run_recipe(
    recipe: Recipe,
    driver: PageDriver,
    job_dir: str | Path,
    *,
    job_id: str | None = None,
    recipe_name: str | None = None,
    now: Callable[[], datetime] = datetime.now,
    on_step_start: Callable[[int, int, Step], None] | None = None,
    on_step_end: Callable[[StepLog], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RunLog:
    """Run `recipe` on `driver`, writing run_log.json (and output.xlsx) into `job_dir`.

    `on_step_start(index, total, step)` and `on_step_end(step_log)` report live progress;
    a callback that raises is logged and ignored, it never breaks the run. `sleep` is
    injectable so retry back-off can be tested without waiting.
    """
    job_path = Path(job_dir)
    job_id = job_id or job_path.name
    started = now()
    started_clock = time.perf_counter()
    run_log = RunLog(
        job_id=job_id,
        recipe_name=recipe_name,
        url=recipe.url,
        started_at=started.isoformat(timespec="seconds"),
    )
    steps = _planned_steps(recipe)
    tables: dict[str, list[dict[str, Any]]] = {}

    with bound_contextvars(job_id=job_id, recipe=recipe_name):
        logger.info("job_start", url=recipe.url, steps=len(steps))

        for index, step in enumerate(steps, start=1):
            _notify(on_step_start, index, len(steps), step)
            step_clock = time.perf_counter()
            retries = step.retries if step.retries is not None else recipe.retries
            backoff = (
                step.backoff_seconds if step.backoff_seconds is not None else recipe.backoff_seconds
            )
            ctx = ActionContext(job_path, index, now)
            result, attempts, retry_errors = _run_with_retries(
                driver, step, ctx, retries, backoff, sleep
            )
            failure_shot = None if result.success else _failure_screenshot(driver, step, ctx)
            duration_ms = int((time.perf_counter() - step_clock) * 1000)

            step_log = StepLog(
                index=index,
                type=step.type,
                name=step.name,
                success=result.success,
                error=result.error,
                duration_ms=duration_ms,
                data=_loggable_data(result.data),
                attempts=attempts,
                retry_errors=retry_errors,
                error_kind=result.error_kind,
                human_error=humanize(result.error_kind, result.error) if result.error else None,
                failure_screenshot=failure_shot,
            )
            run_log.steps.append(step_log)
            _notify(on_step_end, step_log)
            emit = logger.info if result.success else logger.warning
            emit(
                "step_result",
                index=index,
                step_type=step.type,
                success=result.success,
                duration_ms=duration_ms,
                error=result.error,
            )

            rows = result.data.get("rows")
            if result.success and isinstance(rows, list):
                tables[step.name or f"{step.type}_{index}"] = rows

            if not result.success and not recipe.continue_on_error:
                run_log.stopped_early = True
                run_log.skipped_steps = len(steps) - index
                break

        if tables:
            steps_before_export = len(run_log.steps)
            _export_excel(run_log, tables, job_path)
            for export_step in run_log.steps[steps_before_export:]:
                _notify(on_step_end, export_step)

        run_log.success = all(step.success for step in run_log.steps)
        run_log.duration_ms = int((time.perf_counter() - started_clock) * 1000)
        run_log.finished_at = now().isoformat(timespec="seconds")
        _write_run_log(job_path, run_log)
        logger.info(
            "job_end",
            success=run_log.success,
            steps_run=len(run_log.steps),
            skipped=run_log.skipped_steps,
            duration_ms=run_log.duration_ms,
            output_file=run_log.output_file,
        )
    return run_log


def _export_excel(run_log: RunLog, tables: dict[str, list[dict[str, Any]]], job_path: Path) -> None:
    target = job_path / "output.xlsx"
    try:
        save_workbook(build_workbook(tables), target)
    except OSError as exc:
        error = f"Could not write output.xlsx: {exc}"
        logger.warning("export_failed", error=error)
        run_log.steps.append(
            StepLog(
                index=len(run_log.steps) + 1,
                type="export_excel",
                name=None,
                success=False,
                error=error,
                duration_ms=0,
                human_error=(
                    "The Excel report could not be saved. If output.xlsx is open in another "
                    "program, close it and run again."
                ),
            )
        )
        return
    run_log.output_file = str(target)
