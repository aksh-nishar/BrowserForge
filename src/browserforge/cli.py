"""Command line entry point: `uv run browserforge` starts the dashboard."""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

import uvicorn

from browserforge.jobs import DriverFactory, JobStore, run_bulk_job, run_job
from browserforge.logging_config import configure_logging
from browserforge.recipe_files import find_recipe

ENV_RECIPES_DIR = "BROWSERFORGE_RECIPES_DIR"
ENV_JOBS_DIR = "BROWSERFORGE_JOBS_DIR"
ENV_DATA_DIR = "BROWSERFORGE_DATA_DIR"
ENV_JSON_LOGS = "BROWSERFORGE_JSON_LOGS"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="browserforge",
        description="Start the BrowserForge dashboard (recipes in, screenshots and Excel out).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    parser.add_argument("--recipes-dir", default="recipes", help="where recipes are stored")
    parser.add_argument("--jobs-dir", default="jobs", help="where each run's output is written")
    parser.add_argument(
        "--data-dir", default="data", help="where schedules and their data files are kept"
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="write JSON log lines (also to logs/app.log) instead of coloured console output",
    )
    parser.add_argument("--reload", action="store_true", help="restart when code changes (dev)")
    return parser


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="browserforge run",
        description=(
            "Run one recipe from the command line, without the dashboard. "
            "Exit code 0 means every step (and every data row) succeeded."
        ),
    )
    parser.add_argument(
        "recipe", help="a recipe name from --recipes-dir, or a path to a .yaml file"
    )
    parser.add_argument("--data", help="a .csv or .xlsx file: run the recipe once per row")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="PLACEHOLDER=COLUMN",
        help="use this data column for a {{placeholder}} whose name differs (repeatable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="stop before the step marked 'submit: true' and take a screenshot instead",
    )
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--recipes-dir", default="recipes", help="where recipes are stored")
    parser.add_argument("--jobs-dir", default="jobs", help="where the run's output is written")
    return parser


def _parse_map_options(items: Sequence[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items:
        placeholder, sep, column = item.partition("=")
        if not sep or not placeholder.strip() or not column.strip():
            raise ValueError(f"--map needs PLACEHOLDER=COLUMN, got '{item}'")
        mapping[placeholder.strip()] = column.strip()
    return mapping


def run_command(
    argv: Sequence[str],
    *,
    driver_factory: DriverFactory | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """`browserforge run`: run a recipe (once, or once per data row) and print a short summary."""
    from browserforge.data_input import build_rows, prepare_single
    from browserforge.engine.data_rows import DataError
    from browserforge.engine.driver import playwright_driver
    from browserforge.engine.recipe import RecipeValidationError, load_recipe
    from browserforge.engine.runner import has_submit_step

    out = out or sys.stdout
    err = err or sys.stderr
    args = build_run_parser().parse_args(argv)

    def fail(message: str) -> int:
        print(f"Error: {message}", file=err)
        return 2

    recipes_dir = Path(args.recipes_dir)
    recipe_path = Path(args.recipe)
    if not (recipe_path.suffix.lower() in (".yaml", ".yml") and recipe_path.is_file()):
        found = find_recipe(recipes_dir, args.recipe)
        if found is None:
            return fail(f"recipe '{args.recipe}' not found (looked in {recipes_dir})")
        recipe_path = found
    try:
        recipe = load_recipe(recipe_path)
        mapping = _parse_map_options(args.map) or None
    except (RecipeValidationError, ValueError) as exc:
        return fail(str(exc))
    if args.dry_run and not has_submit_step(recipe):
        return fail("--dry-run needs a step marked 'submit: true' in the recipe")

    recipe_name = recipe_path.stem
    store = JobStore()
    record = store.create(recipe_name, dry_run=args.dry_run)
    job_dir = Path(args.jobs_dir) / record.job_id
    factory = driver_factory or (lambda: playwright_driver(headless=not args.headed))
    configure_logging(level=logging.WARNING)

    try:
        if args.data:
            data_path = Path(args.data)
            try:
                raw = data_path.read_bytes()
            except OSError as exc:
                return fail(f"cannot read data file {data_path}: {exc}")
            rows = build_rows(recipe, raw=raw, filename=data_path.name, mapping=mapping)
            run_bulk_job(
                store,
                record.job_id,
                recipe,
                rows,
                recipe_name,
                job_dir,
                factory,
                dry_run=args.dry_run,
            )
        else:
            runnable, secrets = prepare_single(recipe)
            run_job(
                store,
                record.job_id,
                runnable,
                recipe_name,
                job_dir,
                factory,
                secrets=secrets,
                dry_run=args.dry_run,
            )
    except DataError as exc:
        return fail(str(exc))

    return _print_summary(store, record.job_id, job_dir, out)


def _print_summary(store: JobStore, job_id: str, job_dir: Path, out: TextIO) -> int:
    status = store.status(job_id)
    bulk = status["bulk"]
    if bulk is not None:
        print(
            f"Rows: {bulk['rows_done'] - bulk['rows_failed']} succeeded, "
            f"{bulk['rows_failed']} failed, of {bulk['rows_total']}",
            file=out,
        )
        for failed in bulk["failed_rows"]:
            print(f"  Row {failed['row']} ({failed['label']}): {failed['error']}", file=out)
    else:
        print(f"Steps: {status['succeeded']} succeeded, {status['failed']} failed", file=out)
        if status["failure"]:
            print(f"  {status['failure']['step']}: {status['failure']['error']}", file=out)
    if status["error"]:
        print(f"Error: {status['error']}", file=out)
    print(f"Results: {job_dir}", file=out)
    print("Result: " + ("success" if status["status"] == "complete" else "FAILED"), file=out)
    return 0 if status["status"] == "complete" else 1


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "run":
        raise SystemExit(run_command(arguments[1:]))
    args = build_parser().parse_args(arguments)
    # The app is built by a factory (so --reload works); it reads its settings from the
    # environment, which uvicorn's reload subprocess inherits.
    os.environ[ENV_RECIPES_DIR] = args.recipes_dir
    os.environ[ENV_JOBS_DIR] = args.jobs_dir
    os.environ[ENV_DATA_DIR] = args.data_dir
    os.environ[ENV_JSON_LOGS] = "1" if args.json_logs else "0"
    uvicorn.run(
        "browserforge.app:create_default_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,  # our own logging setup handles uvicorn's logs too
    )
