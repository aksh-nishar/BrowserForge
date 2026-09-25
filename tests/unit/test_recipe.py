from pathlib import Path

import pytest

from browserforge.engine.recipe import (
    Recipe,
    RecipeValidationError,
    Step,
    load_recipe,
    parse_recipe,
    parse_recipe_text,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "recipe.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_recipe(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "url: https://example.com\nsteps:\n  - type: click\n    selector: '#go'\n",
    )
    recipe = load_recipe(path)
    assert recipe == Recipe(
        url="https://example.com",
        steps=[Step(type="click", config={"selector": "#go"})],
    )
    assert recipe.login is None


def test_recipe_with_login_block() -> None:
    recipe = load_recipe(FIXTURES / "sample_recipe.yaml")
    assert recipe.login is not None
    assert recipe.login["username"] == "tomsmith"
    assert recipe.steps == [Step(type="screenshot", name="after_login")]


def test_missing_url_raises() -> None:
    with pytest.raises(RecipeValidationError, match="url"):
        parse_recipe({"steps": []})


def test_blank_url_raises() -> None:
    with pytest.raises(RecipeValidationError, match="url"):
        parse_recipe({"url": "   "})


def test_unknown_step_type_names_bad_type() -> None:
    with pytest.raises(RecipeValidationError, match="'clik'") as exc:
        parse_recipe({"url": "https://x.io", "steps": [{"type": "clik"}]})
    assert "Step 1" in str(exc.value)


def test_step_missing_type_raises() -> None:
    data = {"url": "https://x.io", "steps": [{"type": "click"}, {"selector": "#a"}]}
    with pytest.raises(RecipeValidationError, match="Step 2.*type"):
        parse_recipe(data)


def test_step_not_a_mapping_raises() -> None:
    with pytest.raises(RecipeValidationError, match="Step 1"):
        parse_recipe({"url": "https://x.io", "steps": ["click"]})


def test_empty_steps_is_valid() -> None:
    assert parse_recipe({"url": "https://x.io", "steps": []}).steps == []


def test_omitted_steps_defaults_to_empty() -> None:
    assert parse_recipe({"url": "https://x.io"}).steps == []


def test_steps_not_a_list_raises() -> None:
    with pytest.raises(RecipeValidationError, match="steps"):
        parse_recipe({"url": "https://x.io", "steps": "click"})


def test_login_not_a_mapping_raises() -> None:
    with pytest.raises(RecipeValidationError, match="login"):
        parse_recipe({"url": "https://x.io", "login": "bob"})


def test_top_level_not_a_mapping_raises(tmp_path: Path) -> None:
    with pytest.raises(RecipeValidationError, match="mapping"):
        load_recipe(write(tmp_path, "- just\n- a list\n"))


def test_empty_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RecipeValidationError):
        load_recipe(write(tmp_path, ""))


def test_malformed_yaml_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(RecipeValidationError, match="Invalid YAML"):
        load_recipe(write(tmp_path, "url: [unclosed\nsteps: {"))


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RecipeValidationError, match="not found"):
        load_recipe(tmp_path / "nope.yaml")


def test_continue_on_error_defaults_to_false_and_can_be_enabled() -> None:
    assert parse_recipe({"url": "https://x.io"}).continue_on_error is False
    assert parse_recipe({"url": "https://x.io", "continue_on_error": True}).continue_on_error


@pytest.mark.parametrize("bad", ["yes", 1, [], None])
def test_continue_on_error_must_be_a_boolean(bad: object) -> None:
    with pytest.raises(RecipeValidationError, match="continue_on_error"):
        parse_recipe({"url": "https://x.io", "continue_on_error": bad})


def test_parse_recipe_text_builds_a_recipe_from_yaml_text() -> None:
    recipe = parse_recipe_text("url: https://x.io\nsteps:\n  - type: click\n    selector: '#a'\n")
    assert recipe.url == "https://x.io"
    assert recipe.steps == [Step(type="click", config={"selector": "#a"})]


def test_parse_recipe_text_names_the_source_in_yaml_errors() -> None:
    with pytest.raises(RecipeValidationError, match=r"Invalid YAML in upload\.yaml"):
        parse_recipe_text("url: [unclosed", "upload.yaml")


def test_retry_settings_default_to_none_and_are_optional() -> None:
    recipe = parse_recipe({"url": "https://x.io", "steps": [{"type": "click", "selector": "#a"}]})
    assert (recipe.retries, recipe.backoff_seconds) == (0, 1.0)
    assert (recipe.steps[0].retries, recipe.steps[0].backoff_seconds) == (None, None)


def test_recipe_level_retry_settings_are_parsed() -> None:
    recipe = parse_recipe({"url": "https://x.io", "retries": 3, "backoff_seconds": 0.5})
    assert (recipe.retries, recipe.backoff_seconds) == (3, 0.5)


def test_step_level_retry_settings_override_and_stay_out_of_the_config() -> None:
    raw = {"type": "click", "selector": "#a", "retries": 2, "backoff_seconds": 4}
    step = parse_recipe({"url": "https://x.io", "steps": [raw]}).steps[0]
    assert (step.retries, step.backoff_seconds) == (2, 4.0)
    assert step.config == {"selector": "#a"}


@pytest.mark.parametrize("bad", [-1, 11, 1.5, "3", True, []])
def test_invalid_retries_are_rejected(bad: object) -> None:
    with pytest.raises(RecipeValidationError, match="retries"):
        parse_recipe({"url": "https://x.io", "retries": bad})


@pytest.mark.parametrize("bad", [-1, 61, "1s", True, []])
def test_invalid_backoff_is_rejected(bad: object) -> None:
    with pytest.raises(RecipeValidationError, match="backoff_seconds"):
        parse_recipe({"url": "https://x.io", "backoff_seconds": bad})


def test_invalid_step_retry_settings_name_the_step() -> None:
    raw = {"type": "click", "selector": "#a", "retries": 99}
    with pytest.raises(RecipeValidationError, match="Step 1.*retries"):
        parse_recipe({"url": "https://x.io", "steps": [raw]})


def test_zero_retries_and_zero_backoff_are_valid() -> None:
    recipe = parse_recipe({"url": "https://x.io", "retries": 0, "backoff_seconds": 0})
    assert (recipe.retries, recipe.backoff_seconds) == (0, 0.0)


def test_step_name_and_config_split() -> None:
    raw = {"type": "fill", "name": "user", "selector": "#u", "value": "bob"}
    step = parse_recipe({"url": "https://x.io", "steps": [raw]}).steps[0]
    assert step.name == "user"
    assert step.config == {"selector": "#u", "value": "bob"}
