"""The recipes shipped in recipes/ must always load, so a demo never starts with a broken file."""

from pathlib import Path

import pytest

from browserforge.engine.recipe import STEP_TYPES, load_recipe

RECIPES_DIR = Path(__file__).parents[2] / "recipes"
EXAMPLES = sorted(RECIPES_DIR.glob("example_*.yaml"))


def test_the_example_recipes_exist() -> None:
    names = [path.name for path in EXAMPLES]
    assert "example_login_scrape.yaml" in names


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_recipe_loads_and_uses_only_known_step_types(path: Path) -> None:
    recipe = load_recipe(path)
    assert recipe.url.startswith("https://")
    assert recipe.steps
    assert {step.type for step in recipe.steps} <= STEP_TYPES


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_every_example_step_has_a_name_for_readable_logs(path: Path) -> None:
    unnamed = [step.type for step in load_recipe(path).steps if not step.name]
    assert unnamed == [] or unnamed == ["goto"], unnamed  # goto steps read fine unnamed


def test_login_scrape_example_has_the_pieces_the_plan_promises() -> None:
    recipe = load_recipe(RECIPES_DIR / "example_login_scrape.yaml")
    types = [step.type for step in recipe.steps]
    assert recipe.login is not None
    assert "paginate" in types
    assert "screenshot" in types
    paginate = next(step for step in recipe.steps if step.type == "paginate")
    assert paginate.config["max_pages"] >= 1  # never unbounded
    assert recipe.retries >= 1


def test_example_recipes_contain_only_public_demo_credentials() -> None:
    for path in EXAMPLES:
        text = path.read_text(encoding="utf-8")
        assert "herokuapp.com" in text or "scrapethissite.com" in text, path.name
        recipe = load_recipe(path)
        if recipe.login:
            assert recipe.login["username"] == "tomsmith"  # the public demo account
