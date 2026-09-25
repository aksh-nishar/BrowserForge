"""Smoke tests for the schedules card of the dashboard."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browserforge.app import create_app


@pytest.fixture
def html(tmp_path: Path) -> str:
    client = TestClient(create_app(recipes_dir=tmp_path / "r", jobs_dir=tmp_path / "j"))
    return client.get("/").text


@pytest.mark.parametrize(
    "element_id",
    [
        "schedule-card",
        "schedule-when",
        "schedule-tz",
        "schedule-data-row",
        "schedule-file",
        "schedule-btn",
        "schedule-message",
        "schedule-empty",
        "schedule-wrap",
        "schedule-body",
    ],
)
def test_page_has_every_schedule_element_the_script_relies_on(html: str, element_id: str) -> None:
    assert f'id="{element_id}"' in html


def test_the_page_explains_that_schedules_only_run_while_it_is_open(html: str) -> None:
    assert "only happens while this dashboard is running" in html


def test_the_page_shows_schedule_examples(html: str) -> None:
    assert "daily 08:00" in html
    assert "every 15 minutes" in html


def test_the_script_uses_the_schedule_endpoints(html: str) -> None:
    script = html.split("<script>", 1)[1]
    assert '"/schedules"' in script
    assert '"/schedules/" + encodeURIComponent(schedule.id)' in script
    assert "innerHTML" not in script
