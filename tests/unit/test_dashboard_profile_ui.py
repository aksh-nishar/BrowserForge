"""Smoke tests for the saved-profile controls of the dashboard."""

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
        "profile-box",
        "profile-select",
        "profile-load-btn",
        "profile-delete-btn",
        "profile-name",
        "profile-save-btn",
        "profile-message",
    ],
)
def test_page_has_every_profile_element_the_script_relies_on(html: str, element_id: str) -> None:
    assert f'id="{element_id}"' in html


def test_the_profile_box_starts_hidden_until_a_recipe_needs_typed_values(html: str) -> None:
    assert 'id="profile-box" hidden' in html


def test_the_script_uses_the_profile_endpoints_with_encoded_names(html: str) -> None:
    script = html.split("<script>", 1)[1]
    assert '"/profiles"' in script
    assert '"/profiles/" + encodeURIComponent(name)' in script
    assert '"/apply"' in script


def test_the_page_tells_people_which_fields_are_not_saved(html: str) -> None:
    script = html.split("<script>", 1)[1]
    assert "look like secrets" in script


def test_profile_text_is_only_ever_inserted_as_text(html: str) -> None:
    script = html.split("<script>", 1)[1]
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script, sink
