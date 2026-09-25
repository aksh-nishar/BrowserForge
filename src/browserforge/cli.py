"""Command line entry point: `uv run browserforge` starts the dashboard."""

import argparse
import os
from collections.abc import Sequence

import uvicorn

ENV_RECIPES_DIR = "BROWSERFORGE_RECIPES_DIR"
ENV_JOBS_DIR = "BROWSERFORGE_JOBS_DIR"
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
        "--json-logs",
        action="store_true",
        help="write JSON log lines (also to logs/app.log) instead of coloured console output",
    )
    parser.add_argument("--reload", action="store_true", help="restart when code changes (dev)")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # The app is built by a factory (so --reload works); it reads its settings from the
    # environment, which uvicorn's reload subprocess inherits.
    os.environ[ENV_RECIPES_DIR] = args.recipes_dir
    os.environ[ENV_JOBS_DIR] = args.jobs_dir
    os.environ[ENV_JSON_LOGS] = "1" if args.json_logs else "0"
    uvicorn.run(
        "browserforge.app:create_default_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,  # our own logging setup handles uvicorn's logs too
    )
