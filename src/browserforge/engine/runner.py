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
)
from browserforge.engine.driver import PageDriver
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
        )


def _loggable_data(data: dict[str, Any]) -> dict[str, Any]:
    """Step data for run_log.json: table rows are summarised as a count, not embedded."""
    return {
        ("row_count" if key == "rows" else key): (len(value) if key == "rows" else value)
        for key, value in data.items()
    }


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
) -> RunLog:
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
            step_clock = time.perf_counter()
            result = execute_step(driver, step, ActionContext(job_path, index, now))
            duration_ms = int((time.perf_counter() - step_clock) * 1000)

            run_log.steps.append(
                StepLog(
                    index=index,
                    type=step.type,
                    name=step.name,
                    success=result.success,
                    error=result.error,
                    duration_ms=duration_ms,
                    data=_loggable_data(result.data),
                )
            )
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
            _export_excel(run_log, tables, job_path)

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
            )
        )
        return
    run_log.output_file = str(target)
