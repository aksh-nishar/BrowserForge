"""Smoke tests for the dashboard HTML. Behaviour is verified in a real browser in
tests/integration/test_dashboard.py; these guard the properties that must never regress."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browserforge.app import TEMPLATES_DIR, create_app


@pytest.fixture
def html(tmp_path: Path) -> str:
    client = TestClient(create_app(recipes_dir=tmp_path / "r", jobs_dir=tmp_path / "j"))
    return client.get("/").text


@pytest.mark.parametrize(
    "element_id",
    [
        "recipe-file",
        "upload-btn",
        "recipe-select",
        "run-btn",
        "message",
        "status-card",
        "status-badge",
        "progress",
        "failure-box",
        "warnings",
        "steps-body",
        "results-card",
        "downloads",
        "gallery",
    ],
)
def test_page_has_every_element_the_script_and_tests_rely_on(html: str, element_id: str) -> None:
    assert f'id="{element_id}"' in html


def test_page_is_a_complete_mobile_friendly_document(html: str) -> None:
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert '<html lang="en">' in html
    assert 'name="viewport"' in html
    assert "<title>BrowserForge</title>" in html


def test_page_supports_dark_mode(html: str) -> None:
    assert "prefers-color-scheme: dark" in html


def test_page_never_uses_innerhtml_or_other_html_injection_sinks(html: str) -> None:
    """Recipe names, errors and scraped text are untrusted: only textContent may be used."""
    script = html.split("<script>", 1)[1]
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script, sink


def test_page_loads_nothing_from_other_sites(html: str) -> None:
    urls = re.findall(r"""(?:src|href)\s*=\s*["'](https?:)?//""", html)
    assert urls == []
    assert "@import" not in html
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "https://" not in html


def test_template_file_lives_inside_the_package() -> None:
    assert (TEMPLATES_DIR / "dashboard.html").is_file()
    assert TEMPLATES_DIR.is_relative_to(Path(__file__).parents[2] / "src" / "browserforge")


def test_script_polls_every_two_seconds_like_the_plan_says(html: str) -> None:
    assert "const POLL_MS = 2000;" in html
