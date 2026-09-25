"""Smoke tests for the data-entry parts of the dashboard (bulk runs, test runs)."""

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
        "data-card",
        "data-help",
        "data-mode",
        "typed-fields",
        "sheet-fields",
        "data-file",
        "mapping-box",
        "mapping-body",
        "preview-summary",
        "preview-problems",
        "env-note",
        "dry-run",
        "dry-run-row",
        "dry-run-note",
        "failed-rows-wrap",
        "failed-rows-body",
        "steps-wrap",
    ],
)
def test_page_has_every_data_element_the_script_relies_on(html: str, element_id: str) -> None:
    assert f'id="{element_id}"' in html


def test_the_data_card_starts_hidden_until_a_recipe_needs_data(html: str) -> None:
    assert 'id="data-card" aria-labelledby="h-data" hidden' in html


def test_the_data_mode_offers_typing_or_a_sheet(html: str) -> None:
    assert '<option value="typed">' in html
    assert '<option value="sheet">' in html


def test_the_sheet_input_accepts_only_csv_and_xlsx(html: str) -> None:
    assert 'accept=".csv,.xlsx"' in html


def test_the_script_calls_the_new_endpoints(html: str) -> None:
    script = html.split("<script>", 1)[1]
    assert '"/data/preview"' in script
    assert '"/jobs/bulk"' in script
    assert "dry_run" in script


def test_secret_names_get_a_masked_input(html: str) -> None:
    script = html.split("<script>", 1)[1]
    assert 'isSecretName(name) ? "password" : "text"' in script
