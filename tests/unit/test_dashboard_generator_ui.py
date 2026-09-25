"""Smoke tests for the 'create a recipe from a web form' card of the dashboard."""

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
        "create-card",
        "scan-url",
        "scan-btn",
        "scan-message",
        "scan-result",
        "scan-notes",
        "scan-fields-body",
        "scan-skipped",
        "submit-select",
        "generated-name",
        "save-generated-btn",
    ],
)
def test_page_has_every_generator_element_the_script_relies_on(html: str, element_id: str) -> None:
    assert f'id="{element_id}"' in html


def test_the_scan_result_starts_hidden(html: str) -> None:
    assert 'id="scan-result" hidden' in html


def test_the_script_uses_the_generator_endpoints(html: str) -> None:
    script = html.split("<script>", 1)[1]
    assert '"/scan"' in script
    assert '"/recipes/generate"' in script


def test_generated_recipes_are_offered_a_test_run_first(html: str) -> None:
    script = html.split("<script>", 1)[1]
    assert '$("dry-run").checked = saved.has_submit' in script


def test_scanned_page_text_is_only_ever_inserted_as_text(html: str) -> None:
    """Field labels come from the scanned page, which is untrusted."""
    script = html.split("<script>", 1)[1]
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script, sink
