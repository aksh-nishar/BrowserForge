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
        "choose",
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


MAX_RETRIES = 10
MAX_BACKOFF_SECONDS = 60.0


@dataclass(frozen=True)
class Step:
    type: str
    name: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    retries: int | None = None  # None = use the recipe's setting
    backoff_seconds: float | None = None


@dataclass(frozen=True)
class Recipe:
    url: str
    steps: list[Step] = field(default_factory=list)
    login: dict[str, Any] | None = None
    continue_on_error: bool = False
    retries: int = 0
    backoff_seconds: float = 1.0


def _parse_retry_settings(source: dict[str, Any], label: str) -> tuple[int | None, float | None]:
    """Pop and validate `retries` / `backoff_seconds` from a mapping (recipe or step)."""
    retries = source.pop("retries", None)
    if retries is not None and (
        isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= MAX_RETRIES
    ):
        raise RecipeValidationError(
            f"{label}: 'retries' must be a whole number from 0 to {MAX_RETRIES}"
        )
    backoff = source.pop("backoff_seconds", None)
    if backoff is not None and (
        isinstance(backoff, bool)
        or not isinstance(backoff, (int, float))
        or not 0 <= backoff <= MAX_BACKOFF_SECONDS
    ):
        raise RecipeValidationError(
            f"{label}: 'backoff_seconds' must be a number from 0 to {MAX_BACKOFF_SECONDS:g}"
        )
    return retries, None if backoff is None else float(backoff)


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
    retries, backoff = _parse_retry_settings(body, label)
    return Step(type=step_type, name=name, config=body, retries=retries, backoff_seconds=backoff)


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

    continue_on_error = data.get("continue_on_error", False)
    if not isinstance(continue_on_error, bool):
        raise RecipeValidationError("'continue_on_error' must be true or false")

    settings = {key: data[key] for key in ("retries", "backoff_seconds") if key in data}
    retries, backoff = _parse_retry_settings(settings, "Recipe")

    steps = [_parse_step(i, raw) for i, raw in enumerate(raw_steps, start=1)]
    return Recipe(
        url=url,
        steps=steps,
        login=login,
        continue_on_error=continue_on_error,
        retries=retries or 0,
        backoff_seconds=1.0 if backoff is None else backoff,
    )


def parse_recipe_text(text: str, source: str = "recipe") -> Recipe:
    """Parse and validate recipe YAML text. Raises RecipeValidationError on any problem."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecipeValidationError(f"Invalid YAML in {source}: {exc}") from exc
    return parse_recipe(data)


def load_recipe(path: str | Path) -> Recipe:
    """Read a recipe YAML file. Raises RecipeValidationError on any problem."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RecipeValidationError(f"Recipe file not found: {file}") from None
    except OSError as exc:
        raise RecipeValidationError(f"Cannot read recipe file {file}: {exc}") from exc
    return parse_recipe_text(text, file.name)
