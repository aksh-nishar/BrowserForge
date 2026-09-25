"""Recipe schema: YAML -> validated Recipe dataclass. No browser, no network."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

STEP_TYPES = frozenset(
    {
        "goto",
        "fill",
        "click",
        "select_option",
        "check",
        "login",
        "upload",
        "download",
        "screenshot",
        "extract_table",
        "paginate",
        "wait_for_selector",
    }
)


class RecipeValidationError(Exception):
    """Recipe file is missing, unreadable, or violates the schema."""


@dataclass(frozen=True)
class Step:
    type: str
    name: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Recipe:
    url: str
    steps: list[Step] = field(default_factory=list)
    login: dict[str, Any] | None = None


def _parse_step(index: int, raw: Any) -> Step:
    label = f"Step {index}"
    if not isinstance(raw, dict):
        raise RecipeValidationError(f"{label}: must be a mapping with a 'type' key")
    body = dict(raw)
    step_type = body.pop("type", None)
    if not isinstance(step_type, str) or not step_type:
        raise RecipeValidationError(f"{label}: missing required field 'type'")
    if step_type not in STEP_TYPES:
        valid = ", ".join(sorted(STEP_TYPES))
        raise RecipeValidationError(
            f"{label}: unknown step type '{step_type}'. Valid types: {valid}"
        )
    name = body.pop("name", None)
    if name is not None and not isinstance(name, str):
        raise RecipeValidationError(f"{label}: 'name' must be a string")
    return Step(type=step_type, name=name, config=body)


def parse_recipe(data: Any) -> Recipe:
    """Validate already-parsed YAML data and build a Recipe."""
    if not isinstance(data, dict):
        raise RecipeValidationError("Recipe must be a mapping at the top level")

    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        raise RecipeValidationError("Recipe is missing required field 'url'")

    login = data.get("login")
    if login is not None and not isinstance(login, dict):
        raise RecipeValidationError("'login' must be a mapping")

    raw_steps = data.get("steps") or []
    if not isinstance(raw_steps, list):
        raise RecipeValidationError("'steps' must be a list")

    steps = [_parse_step(i, raw) for i, raw in enumerate(raw_steps, start=1)]
    return Recipe(url=url, steps=steps, login=login)


def load_recipe(path: str | Path) -> Recipe:
    """Read a recipe YAML file. Raises RecipeValidationError on any problem."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RecipeValidationError(f"Recipe file not found: {file}") from None
    except OSError as exc:
        raise RecipeValidationError(f"Cannot read recipe file {file}: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecipeValidationError(f"Invalid YAML in {file.name}: {exc}") from exc

    return parse_recipe(data)
