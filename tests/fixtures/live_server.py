"""Helpers for integration tests: the real dashboard served by a real uvicorn server, plus a
few readers for the local test website."""

import json
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

from browserforge.app import create_app
from browserforge.engine.driver import playwright_driver
from browserforge.schedule import Schedule
from browserforge.scheduler import cron_trigger

JOB_TIMEOUT_MS = 90_000


@dataclass
class LiveApp:
    url: str
    recipes: Path
    jobs: Path
    data: Path


@contextmanager
def running_app(
    tmp_path: Path,
    *,
    start_scheduler: bool = False,
    trigger_factory: Callable[[Schedule], Any] = cron_trigger,
) -> Iterator[LiveApp]:
    """Serve the dashboard on a free port with a real browser behind every job."""
    recipes, jobs, data = tmp_path / "recipes", tmp_path / "jobs", tmp_path / "data"
    app = create_app(
        recipes_dir=recipes,
        jobs_dir=jobs,
        data_dir=data,
        driver_factory=lambda: playwright_driver(timeout_ms=4_000),
        start_scheduler=start_scheduler,
        trigger_factory=trigger_factory,
    )
    # log_config=None: uvicorn's default config rewires the global "uvicorn.*" loggers for the
    # whole test process, which would leak into unrelated logging tests.
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", log_config=None)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "dashboard server did not start"
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield LiveApp(f"http://127.0.0.1:{port}", recipes, jobs, data)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def write_recipe(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def submissions(site: str) -> list[dict[str, str]]:
    """What the local site's /register form has received so far."""
    with urllib.request.urlopen(f"{site}/submissions", timeout=5) as response:
        data: list[dict[str, str]] = json.loads(response.read())
    return data


REGISTER_RECIPE = """
url: {site}/register
retries: 0
steps:
  - type: fill
    name: first_name
    selector: "#first_name"
    value: "{{{{first_name}}}}"
  - type: fill
    name: email
    selector: "#email"
    value: "{{{{email}}}}"
  - type: choose
    name: plan
    selector: 'input[type=radio][name="plan"]'
    value: "{{{{plan}}}}"
  - type: screenshot
    name: before_submit
  - type: click
    name: submit
    selector: "#send"
    submit: true
  - type: wait_for_selector
    name: confirmation
    selector: "#thanks"
"""


def register_recipe(site: str) -> str:
    """A recipe with {{first_name}}, {{email}} and {{plan}} placeholders for the /register form."""
    return REGISTER_RECIPE.format(site=site)
