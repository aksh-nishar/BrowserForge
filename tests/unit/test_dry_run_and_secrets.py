"""Runner options added for bulk and form runs: dry_run, secrets redaction, tables_out."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from browserforge.engine.driver import DriverError
from browserforge.engine.recipe import Recipe, Step
from browserforge.engine.runner import (
    DryRunError,
    has_submit_step,
    run_recipe,
    total_steps,
)
from fixtures.fake_driver import FakePageDriver

URL = "https://portal.example.com"
NOW = datetime(2026, 9, 25, 10, 15, 30)


def step(step_type: str, name: str | None = None, **config: Any) -> Step:
    return Step(type=step_type, name=name, config=config)


def form_recipe() -> Recipe:
    return Recipe(
        url=URL,
        steps=[
            step("fill", "name", selector="#name", value="Ann"),
            step("click", "submit", selector="#send", submit=True),
            step("wait_for_selector", "confirmation", selector="#thanks"),
            step("screenshot", "after"),
        ],
    )


# ---- dry run ----


def test_has_submit_step_looks_for_the_marker() -> None:
    assert has_submit_step(form_recipe()) is True
    assert has_submit_step(Recipe(url=URL, steps=[step("click", selector="#a")])) is False
    # only the boolean True counts, not a truthy string
    assert (
        has_submit_step(Recipe(url=URL, steps=[step("click", selector="#a", submit="yes")]))
        is False
    )


def test_dry_run_stops_before_the_submit_step_and_takes_a_screenshot(tmp_path: Path) -> None:
    fake = FakePageDriver()
    log = run_recipe(form_recipe(), fake, tmp_path / "j", dry_run=True, now=lambda: NOW)

    assert [(s.type, s.name) for s in log.steps] == [
        ("goto", "open_start_page"),
        ("fill", "name"),
        ("screenshot", "dry_run_before_submit"),
    ]
    assert log.success is True
    assert fake.clicks == []  # the form was never submitted
    assert fake.fills == {"#name": "Ann"}
    assert len(fake.screenshots) == 1
    assert "dry_run_before_submit" in fake.screenshots[0]


def test_dry_run_drops_every_step_after_the_submit_marker(tmp_path: Path) -> None:
    fake = FakePageDriver()
    run_recipe(form_recipe(), fake, tmp_path / "j", dry_run=True, now=lambda: NOW)
    assert fake.calls_to("wait_for_selector") == []


def test_a_normal_run_still_submits(tmp_path: Path) -> None:
    fake = FakePageDriver()
    log = run_recipe(form_recipe(), fake, tmp_path / "j", now=lambda: NOW)
    assert fake.clicks == ["#send"]
    assert [s.name for s in log.steps][-1] == "after"


def test_dry_run_refuses_a_recipe_with_no_submit_step_before_touching_the_browser(
    tmp_path: Path,
) -> None:
    fake = FakePageDriver()
    plain = Recipe(url=URL, steps=[step("click", selector="#buy")])
    with pytest.raises(DryRunError, match="submit: true"):
        run_recipe(plain, fake, tmp_path / "j", dry_run=True, now=lambda: NOW)
    assert fake.calls == []


def test_total_steps_in_a_dry_run_counts_only_what_runs() -> None:
    assert total_steps(form_recipe()) == 5
    assert total_steps(form_recipe(), dry_run=True) == 3


def test_a_login_before_the_submit_still_runs_in_a_dry_run(tmp_path: Path) -> None:
    login = {
        "username_selector": "#u",
        "password_selector": "#p",
        "submit_selector": "#go",
        "username": "a",
        "password": "b",
        "success_url_contains": "/in",
    }
    rec = Recipe(url=URL, login=login, steps=form_recipe().steps)
    fake = FakePageDriver()
    fake.set_url_on_click("#go", URL + "/in")
    log = run_recipe(rec, fake, tmp_path / "j", dry_run=True, now=lambda: NOW)
    assert [s.type for s in log.steps] == ["goto", "login", "fill", "screenshot"]
    assert "#send" not in fake.clicks


# ---- secrets ----


def failing_fill_recipe() -> Recipe:
    return Recipe(url=URL, steps=[step("fill", "pw", selector="#pw", value="hunter22")])


def test_secrets_are_replaced_in_the_run_log_and_step_errors(tmp_path: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("fill", DriverError("could not type hunter22 into the box"), times=None)
    job_dir = tmp_path / "j"
    log = run_recipe(failing_fill_recipe(), fake, job_dir, secrets=["hunter22"], now=lambda: NOW)

    failed = log.steps[-1]
    assert failed.success is False
    assert "hunter22" not in (failed.error or "")
    assert "hunter22" not in (failed.human_error or "")
    assert "***" in (failed.error or "")
    assert "hunter22" not in (job_dir / "run_log.json").read_text(encoding="utf-8")


def test_without_secrets_errors_are_left_as_they_are(tmp_path: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("fill", DriverError("could not type hunter22"), times=None)
    log = run_recipe(failing_fill_recipe(), fake, tmp_path / "j", now=lambda: NOW)
    assert "hunter22" in (log.steps[-1].error or "")


def test_secrets_are_also_hidden_from_retry_errors(tmp_path: Path) -> None:
    fake = FakePageDriver()
    fake.fail_on("fill", DriverError("bad hunter22"), times=2)
    rec = Recipe(url=URL, steps=failing_fill_recipe().steps, retries=2, backoff_seconds=0)
    log = run_recipe(
        rec, fake, tmp_path / "j", secrets=["hunter22"], sleep=lambda _s: None, now=lambda: NOW
    )
    assert log.steps[-1].success is True
    assert log.steps[-1].retry_errors == ["Step 'pw': bad ***", "Step 'pw': bad ***"]


# ---- tables_out ----


def test_tables_out_receives_the_scraped_tables(tmp_path: Path) -> None:
    fake = FakePageDriver()
    fake.set_table("#t", [["id", "name"], ["1", "Ann"]])
    rec = Recipe(url=URL, steps=[step("extract_table", "People", selector="#t")])
    collected: dict[str, list[dict[str, Any]]] = {}
    run_recipe(rec, fake, tmp_path / "j", tables_out=collected, now=lambda: NOW)
    assert collected == {"People": [{"id": "1", "name": "Ann"}]}


def test_tables_out_is_optional_and_run_log_still_lists_only_counts(tmp_path: Path) -> None:
    fake = FakePageDriver()
    fake.set_table("#t", [["id"], ["1"]])
    rec = Recipe(url=URL, steps=[step("extract_table", "People", selector="#t")])
    run_recipe(rec, fake, tmp_path / "j", now=lambda: NOW)
    data = json.loads((tmp_path / "j" / "run_log.json").read_text(encoding="utf-8"))
    assert data["steps"][1]["data"] == {"row_count": 1}
