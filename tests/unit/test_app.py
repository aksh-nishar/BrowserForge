from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from browserforge.app import create_app, safe_recipe_name

VALID = "url: https://x.io\nsteps:\n  - type: click\n    selector: '#go'\n"


@pytest.fixture
def recipes_dir(tmp_path: Path) -> Path:
    return tmp_path / "recipes"  # not created: the app must create it on first upload


@pytest.fixture
def client(recipes_dir: Path, tmp_path: Path) -> TestClient:
    return TestClient(create_app(recipes_dir=recipes_dir, jobs_dir=tmp_path / "jobs"))


def upload(client: TestClient, filename: str, content: str | bytes = VALID) -> Response:
    data = content.encode() if isinstance(content, str) else content
    return client.post("/recipes", files={"file": (filename, data, "application/x-yaml")})


# ---- dashboard ----


def test_root_serves_the_dashboard_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "BrowserForge" in response.text


# ---- upload: success ----


def test_valid_recipe_is_saved_and_reported(client: TestClient, recipes_dir: Path) -> None:
    response = upload(client, "demo.yaml")
    assert response.status_code == 200
    assert response.json() == {
        "name": "demo",
        "saved": True,
        "replaced": False,
        "steps": 2,  # what a run executes: the automatic start-page step + the one click
    }
    assert (recipes_dir / "demo.yaml").read_text(encoding="utf-8") == VALID


def test_uploading_the_same_name_replaces_the_recipe(client: TestClient, recipes_dir: Path) -> None:
    upload(client, "demo.yaml")
    second = upload(client, "demo.yaml", "url: https://other.io\n")
    assert second.json()["replaced"] is True
    assert (recipes_dir / "demo.yaml").read_text(encoding="utf-8") == "url: https://other.io\n"


def test_yml_extension_is_accepted_and_saved_as_yaml(client: TestClient, recipes_dir: Path) -> None:
    response = upload(client, "demo.YML")
    assert response.status_code == 200
    assert (recipes_dir / "demo.yaml").exists()


def test_unicode_content_is_preserved(client: TestClient, recipes_dir: Path) -> None:
    content = "url: https://x.io\nsteps:\n  - type: screenshot\n    name: नमस्ते\n"
    upload(client, "hindi.yaml", content)
    assert (recipes_dir / "hindi.yaml").read_text(encoding="utf-8") == content


# ---- upload: rejected, nothing saved ----


def test_invalid_yaml_syntax_returns_400_with_the_loader_message(
    client: TestClient, recipes_dir: Path
) -> None:
    response = upload(client, "bad.yaml", "url: [unclosed\nsteps: {")
    assert response.status_code == 400
    assert "Invalid YAML" in response.json()["detail"]
    assert not recipes_dir.exists()


def test_schema_error_message_from_the_loader_is_surfaced(
    client: TestClient, recipes_dir: Path
) -> None:
    response = upload(client, "bad.yaml", "steps: []\n")
    assert response.status_code == 400
    assert "'url'" in response.json()["detail"]
    assert not recipes_dir.exists()


def test_unknown_step_type_is_named_in_the_error(client: TestClient) -> None:
    response = upload(client, "bad.yaml", "url: https://x.io\nsteps:\n  - type: clik\n")
    assert response.status_code == 400
    assert "'clik'" in response.json()["detail"]


@pytest.mark.parametrize("filename", ["recipe.txt", "recipe", "recipe.yaml.exe", "recipe.json"])
def test_wrong_extension_is_rejected(client: TestClient, filename: str) -> None:
    response = upload(client, filename)
    assert response.status_code == 400
    assert ".yaml" in response.json()["detail"]


def test_empty_file_is_rejected(client: TestClient) -> None:
    assert upload(client, "empty.yaml", "").status_code == 400


def test_non_utf8_file_is_rejected(client: TestClient) -> None:
    response = upload(client, "bin.yaml", b"\xff\xfe\x00\x00 not utf8")
    assert response.status_code == 400
    assert "UTF-8" in response.json()["detail"]


def test_oversized_file_is_rejected_with_413(client: TestClient, recipes_dir: Path) -> None:
    big = "url: https://x.io\n# " + "x" * 1_000_001
    response = upload(client, "big.yaml", big)
    assert response.status_code == 413
    assert not recipes_dir.exists()


def test_missing_file_field_is_a_422(client: TestClient) -> None:
    assert client.post("/recipes").status_code == 422


# ---- file name safety ----


def test_path_traversal_in_filename_cannot_escape_the_recipes_folder(
    client: TestClient, recipes_dir: Path, tmp_path: Path
) -> None:
    response = upload(client, "../../evil name.yaml")
    assert response.status_code == 200
    assert response.json()["name"] == "evil_name"
    assert [p.name for p in recipes_dir.iterdir()] == ["evil_name.yaml"]
    assert not (tmp_path / "evil name.yaml").exists()
    assert not (tmp_path.parent / "evil name.yaml").exists()


def test_filename_with_no_usable_characters_is_rejected(client: TestClient) -> None:
    response = upload(client, "***.yaml")
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("demo.yaml", "demo"),
        ("my recipe.yaml", "my_recipe"),
        ("C:\\Users\\x\\payroll-run.yml", "payroll-run"),
        ("a/b/c.yaml", "c"),
        ("__x__.yaml", "x"),
        ("...yaml", None),
        ("", None),
        ("é.yaml", None),
    ],
)
def test_safe_recipe_name(filename: str, expected: str | None) -> None:
    assert safe_recipe_name(filename) == expected


# ---- list ----


def test_list_is_empty_when_the_folder_does_not_exist(client: TestClient) -> None:
    response = client.get("/recipes")
    assert response.status_code == 200
    assert response.json() == {"recipes": []}


def test_list_returns_saved_recipes_sorted_with_details(client: TestClient) -> None:
    upload(client, "zeta.yaml", "url: https://z.io\n")
    upload(client, "alpha.yaml")
    recipes = client.get("/recipes").json()["recipes"]
    assert [r["name"] for r in recipes] == ["alpha", "zeta"]
    assert recipes[0] == {
        "name": "alpha",
        "valid": True,
        "error": None,
        "url": "https://x.io",
        "steps": 2,
    }
    assert recipes[1]["steps"] == 1  # a recipe with no steps still opens its start page


def test_step_count_includes_the_automatic_start_page_and_login_steps(client: TestClient) -> None:
    """The dropdown's "(N steps)" must match the rows the run's step table will show."""
    with_login = (
        "url: https://x.io\n"
        "login:\n"
        "  username_selector: '#u'\n"
        "  password_selector: '#p'\n"
        "  submit_selector: '#s'\n"
        "  username: a\n"
        "  password: b\n"
        "  success_url_contains: /in\n"
        "steps:\n"
        "  - {type: click, selector: '#a'}\n"
        "  - {type: click, selector: '#b'}\n"
    )
    saved = upload(client, "with_login.yaml", with_login).json()
    listed = client.get("/recipes").json()["recipes"][0]
    assert saved["steps"] == listed["steps"] == 4  # start page + login + the 2 clicks


def test_list_reports_invalid_files_instead_of_crashing(
    client: TestClient, recipes_dir: Path
) -> None:
    upload(client, "good.yaml")
    (recipes_dir / "broken.yaml").write_text("steps: []\n", encoding="utf-8")
    recipes = {r["name"]: r for r in client.get("/recipes").json()["recipes"]}
    assert recipes["good"]["valid"] is True
    assert recipes["broken"]["valid"] is False
    assert "'url'" in recipes["broken"]["error"]


def test_list_ignores_other_files_and_shows_yml_files(
    client: TestClient, recipes_dir: Path
) -> None:
    upload(client, "a.yaml")
    (recipes_dir / "notes.txt").write_text("hi")
    (recipes_dir / "b.yml").write_text("url: https://b.io\n", encoding="utf-8")
    names = [r["name"] for r in client.get("/recipes").json()["recipes"]]
    assert names == ["a", "b"]


def test_yaml_is_preferred_over_yml_with_the_same_name(
    client: TestClient, recipes_dir: Path
) -> None:
    upload(client, "same.yaml", "url: https://yaml.io\n")
    (recipes_dir / "same.yml").write_text("url: https://yml.io\n", encoding="utf-8")
    recipes = client.get("/recipes").json()["recipes"]
    assert [(r["name"], r["url"]) for r in recipes] == [("same", "https://yaml.io")]
