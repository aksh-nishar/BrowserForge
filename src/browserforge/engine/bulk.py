"""Bulk runs: one recipe, many data rows, one browser page per row.

Each row is expanded into its own recipe (placeholders filled), run in its own subfolder
(`row_0001/` ...), and never stops the others: a failed row is recorded and the next one starts.
At the end the job folder gets `output.xlsx` (a Run_Report sheet plus any scraped tables, with a
`Row` column) and `run_log.json` (the per-row summary).
"""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import bound_contextvars

from browserforge.engine.data_rows import (
    DataError,
    expand_recipe,
    find_env_vars,
    is_secret_name,
    redact,
    secret_values,
)
from browserforge.engine.driver import PageDriver
from browserforge.engine.excel_export import build_workbook, save_workbook
from browserforge.engine.recipe import Recipe, Step
from browserforge.engine.runner import (
    DryRunError,
    RunLog,
    StepLog,
    has_submit_step,
    run_recipe,
)

logger = structlog.get_logger(__name__)

REPORT_SHEET = "Run_Report"
LABEL_COLUMNS = 3
LABEL_MAX_CHARS = 60

DriverFactory = Callable[[], AbstractContextManager[PageDriver]]


@dataclass
class RowResult:
    row: int  # 1-based position among the data rows
    label: str  # a few non-secret cell values, so a person can tell which row this was
    success: bool
    duration_ms: int
    folder: str  # the row's subfolder inside the job folder
    failed_step: str | None = None
    error: str | None = None  # plain-language reason


@dataclass
class BulkResult:
    job_id: str
    recipe_name: str | None
    started_at: str
    total: int
    finished_at: str = ""
    duration_ms: int = 0
    success: bool = False
    output_file: str | None = None
    rows: list[RowResult] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return sum(1 for row in self.rows if not row.success)


def row_label(row: Mapping[str, str]) -> str:
    """The first few non-blank, non-secret cells joined with ' / ' (kept short)."""
    values = [value for name, value in row.items() if value and not is_secret_name(name)]
    text = " / ".join(values[:LABEL_COLUMNS])
    return text if len(text) <= LABEL_MAX_CHARS else text[: LABEL_MAX_CHARS - 3] + "..."


def row_folder(number: int) -> str:
    return f"row_{number:04d}"


def _first_failure(run_log: RunLog) -> StepLog | None:
    return next((step for step in run_log.steps if not step.success), None)


def _failed_row(number: int, label: str, message: str, step: str | None = None) -> RowResult:
    return RowResult(
        row=number,
        label=label,
        success=False,
        duration_ms=0,
        folder=row_folder(number),
        failed_step=step,
        error=message,
    )


def _result_from_log(number: int, label: str, run_log: RunLog) -> RowResult:
    failure = _first_failure(run_log)
    return RowResult(
        row=number,
        label=label,
        success=run_log.success,
        duration_ms=run_log.duration_ms,
        folder=row_folder(number),
        failed_step=(failure.name or failure.type) if failure else None,
        error=(failure.human_error or failure.error) if failure else None,
    )


def _report_rows(result: BulkResult, secrets: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {
            "Row": row.row,
            "Details": row.label,
            "Status": "OK" if row.success else "Failed",
            "Failed step": row.failed_step or "",
            "Reason": redact(row.error or "", secrets),
            "Seconds": round(row.duration_ms / 1000, 1),
            "Folder": row.folder,
        }
        for row in result.rows
    ]


def _write_outputs(
    result: BulkResult,
    tables: Mapping[str, list[dict[str, Any]]],
    job_dir: Path,
    secrets: Sequence[str],
) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    sheets: dict[str, list[dict[str, Any]]] = {REPORT_SHEET: _report_rows(result, secrets)}
    sheets.update(tables)
    target = job_dir / "output.xlsx"
    try:
        save_workbook(build_workbook(sheets), target)
        result.output_file = str(target)
    except OSError as exc:
        logger.warning("export_failed", error=str(exc))
    text = json.dumps(asdict(result), indent=2, ensure_ascii=False, default=str)
    (job_dir / "run_log.json").write_text(text, encoding="utf-8")


def run_bulk(
    recipe: Recipe,
    rows: Sequence[Mapping[str, str]],
    driver_factory: DriverFactory,
    job_dir: str | Path,
    *,
    job_id: str,
    recipe_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    dry_run: bool = False,
    now: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    on_row_start: Callable[[int, int, str], None] | None = None,
    on_row_end: Callable[[RowResult], None] | None = None,
    on_step_start: Callable[[int, int, Step], None] | None = None,
) -> BulkResult:
    """Run `recipe` once per row of `rows`. Never raises for a failed row.

    A browser that cannot start at all is not a row problem: that error propagates so the
    whole job is reported as failed. A dry run needs a step marked `submit: true`
    (DryRunError otherwise, raised before any browser starts).
    """
    if dry_run and not has_submit_step(recipe):
        raise DryRunError("A test run needs one step marked 'submit: true'.")

    job_path = Path(job_dir)
    started = time.perf_counter()
    result = BulkResult(
        job_id=job_id,
        recipe_name=recipe_name,
        started_at=now().isoformat(timespec="seconds"),
        total=len(rows),
    )
    env_vars = find_env_vars(recipe)
    tables: dict[str, list[dict[str, Any]]] = {}
    all_secrets: set[str] = set()

    with bound_contextvars(job_id=job_id, recipe=recipe_name):
        logger.info("bulk_start", rows=len(rows), dry_run=dry_run)
        for number, row in enumerate(rows, start=1):
            label = row_label(row)
            secrets = secret_values(row, env_vars, environ)
            all_secrets.update(secrets)
            if on_row_start is not None:
                on_row_start(number, len(rows), label)

            try:
                expanded = expand_recipe(recipe, row, environ)
            except DataError as exc:
                row_result = _failed_row(number, label, str(exc))
            else:
                row_result = _run_one_row(
                    expanded,
                    number,
                    label,
                    driver_factory,
                    job_path / row_folder(number),
                    job_id=job_id,
                    recipe_name=recipe_name,
                    secrets=secrets,
                    dry_run=dry_run,
                    now=now,
                    sleep=sleep,
                    on_step_start=on_step_start,
                    tables=tables,
                )
            result.rows.append(row_result)
            logger.info(
                "bulk_row_result",
                row=number,
                success=row_result.success,
                error=redact(row_result.error or "", secrets) or None,
            )
            if on_row_end is not None:
                on_row_end(row_result)

        result.success = bool(result.rows) and all(r.success for r in result.rows)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        result.finished_at = now().isoformat(timespec="seconds")
        _write_outputs(result, tables, job_path, sorted(all_secrets))
        logger.info("bulk_end", success=result.success, rows=result.total, failed=result.failed)
    return result


def _run_one_row(
    recipe: Recipe,
    number: int,
    label: str,
    driver_factory: DriverFactory,
    row_dir: Path,
    *,
    job_id: str,
    recipe_name: str | None,
    secrets: Sequence[str],
    dry_run: bool,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    on_step_start: Callable[[int, int, Step], None] | None,
    tables: dict[str, list[dict[str, Any]]],
) -> RowResult:
    with ExitStack() as stack:
        driver = stack.enter_context(driver_factory())  # a browser that cannot start aborts the job
        row_tables: dict[str, list[dict[str, Any]]] = {}
        try:
            run_log = run_recipe(
                recipe,
                driver,
                row_dir,
                job_id=job_id,
                recipe_name=recipe_name,
                now=now,
                sleep=sleep,
                on_step_start=on_step_start,
                secrets=secrets,
                dry_run=dry_run,
                tables_out=row_tables,
            )
        except Exception:
            logger.exception("bulk_row_crashed", row=number)
            return _failed_row(
                number,
                label,
                "Something unexpected went wrong with this row. The details are in the server log.",
            )
    for name, table_rows in row_tables.items():
        tables.setdefault(name, []).extend({"Row": number, **item} for item in table_rows)
    return _result_from_log(number, label, run_log)
