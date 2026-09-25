import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.contextvars import bound_contextvars, get_contextvars, merge_contextvars
from structlog.testing import capture_logs

import browserforge.logging_config as logging_config
from browserforge.engine.recipe import Recipe, Step
from browserforge.engine.runner import run_recipe
from browserforge.logging_config import configure_logging
from fixtures.fake_driver import FakePageDriver


def with_context() -> Any:
    return capture_logs(processors=[merge_contextvars])


def fake_job(tmp_path: Path, name: str = "job-42") -> list[dict[str, Any]]:
    rec = Recipe(url="https://x.io", steps=[Step(type="click", config={"selector": "#go"})])
    with with_context() as logs:
        run_recipe(
            rec,
            FakePageDriver(),
            tmp_path / name,
            recipe_name="demo",
            now=lambda: datetime(2026, 9, 25, 10, 0, 0),
        )
    return list(logs)


# ---- job-scoped context (runner) ----


def test_job_events_are_emitted_with_job_id_and_recipe_bound(tmp_path: Path) -> None:
    logs = fake_job(tmp_path)
    events = [entry["event"] for entry in logs]
    assert events[0] == "job_start"
    assert events[-1] == "job_end"
    assert events.count("step_result") == 2  # implicit goto + the click
    assert all(entry["job_id"] == "job-42" for entry in logs)
    assert all(entry["recipe"] == "demo" for entry in logs)


def test_step_result_events_carry_outcome_and_timing(tmp_path: Path) -> None:
    results = [e for e in fake_job(tmp_path) if e["event"] == "step_result"]
    assert [e["step_type"] for e in results] == ["goto", "click"]
    assert all(e["success"] is True and e["duration_ms"] >= 0 for e in results)


def test_job_end_event_summarises_the_run(tmp_path: Path) -> None:
    end = fake_job(tmp_path)[-1]
    assert end["success"] is True
    assert end["steps_run"] == 2
    assert end["skipped"] == 0


def test_failed_step_is_logged_as_a_warning(tmp_path: Path) -> None:
    fake = FakePageDriver()
    fake.set_missing("#go")
    rec = Recipe(url="https://x.io", steps=[Step(type="click", config={"selector": "#go"})])
    with with_context() as logs:
        run_recipe(rec, fake, tmp_path / "j")
    failed = [e for e in logs if e["event"] == "step_result" and not e["success"]]
    assert len(failed) == 1
    assert failed[0]["log_level"] == "warning"
    assert "#go" in failed[0]["error"]


def test_context_is_unbound_after_the_job(tmp_path: Path) -> None:
    fake_job(tmp_path)
    assert "job_id" not in get_contextvars()
    assert "recipe" not in get_contextvars()


def test_context_is_unbound_even_when_the_job_blows_up(tmp_path: Path) -> None:
    blocked = tmp_path / "blocker"
    blocked.write_text("a file, so job_dir cannot be created under it")
    rec = Recipe(url="https://x.io")
    with pytest.raises(OSError):
        run_recipe(rec, FakePageDriver(), blocked / "job")
    assert "job_id" not in get_contextvars()


def test_two_consecutive_jobs_do_not_leak_context(tmp_path: Path) -> None:
    first = fake_job(tmp_path, "job-a")
    second = fake_job(tmp_path, "job-b")
    assert {e["job_id"] for e in first} == {"job-a"}
    assert {e["job_id"] for e in second} == {"job-b"}


# ---- configure_logging ----


@pytest.fixture
def clean_logging() -> Iterator[None]:
    yield
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, logging_config._OWNED, False)]:
        root.removeHandler(handler)
        handler.close()
    structlog.reset_defaults()
    root.setLevel(logging.WARNING)


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def owned_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, logging_config._OWNED, False)]


@pytest.mark.usefixtures("clean_logging")
def test_json_file_receives_structured_events_with_bound_context(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "app.log"
    configure_logging(json_logs=True, log_file=log_file)
    logger = structlog.get_logger("browserforge.test")
    with bound_contextvars(job_id="j1"):
        logger.info("hello", rows=3)
    (entry,) = read_json_lines(log_file)
    assert entry["event"] == "hello"
    assert entry["rows"] == 3
    assert entry["job_id"] == "j1"
    assert entry["level"] == "info"
    assert entry["logger"] == "browserforge.test"
    assert "timestamp" in entry


@pytest.mark.usefixtures("clean_logging")
def test_stdlib_logs_from_other_libraries_flow_into_the_same_file(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    configure_logging(json_logs=True, log_file=log_file)
    with bound_contextvars(job_id="j2"):
        logging.getLogger("uvicorn.error").warning("third party says boom")
    (entry,) = read_json_lines(log_file)
    assert entry["event"] == "third party says boom"
    assert entry["logger"] == "uvicorn.error"
    assert entry["level"] == "warning"
    assert entry["job_id"] == "j2"


@pytest.mark.usefixtures("clean_logging")
def test_exceptions_are_rendered_into_the_json_entry(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    configure_logging(json_logs=True, log_file=log_file)
    try:
        raise ValueError("kaboom")
    except ValueError:
        structlog.get_logger("t").exception("failed")
    (entry,) = read_json_lines(log_file)
    assert "ValueError: kaboom" in entry["exception"]


@pytest.mark.usefixtures("clean_logging")
def test_calling_configure_twice_does_not_duplicate_handlers(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    configure_logging(json_logs=True, log_file=log_file)
    configure_logging(json_logs=True, log_file=log_file)
    assert len(owned_handlers()) == 2  # console + file, not 4
    structlog.get_logger("t").info("once")
    assert len(read_json_lines(log_file)) == 1


@pytest.mark.usefixtures("clean_logging")
def test_without_a_log_file_only_the_console_handler_is_installed() -> None:
    configure_logging()
    handlers = owned_handlers()
    assert len(handlers) == 1
    assert not isinstance(handlers[0], logging.FileHandler)


@pytest.mark.usefixtures("clean_logging")
def test_level_filters_lower_severity_events(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    configure_logging(json_logs=True, log_file=log_file, level=logging.WARNING)
    logger = structlog.get_logger("t")
    logger.info("quiet")
    logger.warning("loud")
    assert [e["event"] for e in read_json_lines(log_file)] == ["loud"]


@pytest.mark.usefixtures("clean_logging")
def test_full_job_produces_json_lines_carrying_job_id(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    configure_logging(json_logs=True, log_file=log_file)
    rec = Recipe(url="https://x.io")
    run_recipe(rec, FakePageDriver(), tmp_path / "job-77", recipe_name="demo")
    entries = [e for e in read_json_lines(log_file) if e["logger"].startswith("browserforge")]
    assert [e["event"] for e in entries] == ["job_start", "step_result", "job_end"]
    assert {e["job_id"] for e in entries} == {"job-77"}
