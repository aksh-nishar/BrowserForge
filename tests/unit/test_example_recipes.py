"""The recipes shipped in recipes/ must always load, so a demo never starts with a broken file."""

from pathlib import Path

import pytest

from browserforge.engine.recipe import STEP_TYPES, load_recipe

RECIPES_DIR = Path(__file__).parents[2] / "recipes"
EXAMPLES = sorted(RECIPES_DIR.glob("example_*.yaml"))


def test_the_example_recipes_exist() -> None:
    names = [path.name for path in EXAMPLES]
    assert "example_login_scrape.yaml" in names
    assert "example_form_fill.yaml" in names


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_recipe_loads_and_uses_only_known_step_types(path: Path) -> None:
    recipe = load_recipe(path)
    assert recipe.url.startswith("https://")
    assert recipe.steps
    assert {step.type for step in recipe.steps} <= STEP_TYPES


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_every_example_step_has_a_name_for_readable_logs(path: Path) -> None:
    unnamed = [step.type for step in load_recipe(path).steps if not step.name]
    assert set(unnamed) <= {"goto"}, unnamed  # goto steps read fine unnamed


def test_login_scrape_example_has_the_pieces_the_plan_promises() -> None:
    recipe = load_recipe(RECIPES_DIR / "example_login_scrape.yaml")
    types = [step.type for step in recipe.steps]
    assert recipe.login is not None
    assert "paginate" in types
    assert "screenshot" in types
    paginate = next(step for step in recipe.steps if step.type == "paginate")
    assert paginate.config["max_pages"] >= 1  # never unbounded
    assert recipe.retries >= 1


def test_form_fill_example_has_the_pieces_the_plan_promises() -> None:
    types = [step.type for step in load_recipe(RECIPES_DIR / "example_form_fill.yaml").steps]
    for needed in ("fill", "select_option", "check", "upload", "click", "screenshot"):
        assert needed in types, needed


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_files_an_example_uploads_exist_relative_to_the_project_root(path: Path) -> None:
    project_root = RECIPES_DIR.parent
    for step in load_recipe(path).steps:
        if step.type == "upload":
            files = step.config["files"]
            for file in [files] if isinstance(files, str) else files:
                assert (project_root / file).is_file(), file


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_recipe_steps_have_valid_selectors_or_urls_they_need(path: Path) -> None:
    """Catches typos that only show up mid-demo: every step must carry what its action needs."""
    needs = {
        "goto": "url",
        "fill": "selector",
        "click": "selector",
        "select_option": "selector",
        "check": "selector",
        "upload": "selector",
        "download": "selector",
        "extract_table": "selector",
        "paginate": "selector",
        "wait_for_selector": "selector",
    }
    for step in load_recipe(path).steps:
        if step.type in needs:
            assert step.config.get(needs[step.type]), (path.name, step.type, step.name)


def test_example_recipes_contain_only_public_demo_credentials() -> None:
    for path in EXAMPLES:
        text = path.read_text(encoding="utf-8")
        assert "herokuapp.com" in text or "scrapethissite.com" in text, path.name
        recipe = load_recipe(path)
        if recipe.login:
            assert recipe.login["username"] == "tomsmith"  # the public demo account
