"""Turn what a user gave (a sheet, or typed values, plus a column mapping) into rows for a run.

Shared by the dashboard API, the scheduler and the command line, so every way of starting a
bulk run checks its data the same way, before any browser opens.
"""

import json
from collections.abc import Mapping
from typing import Any

from browserforge.engine.data_rows import (
    DataError,
    apply_mapping,
    expand_recipe,
    find_env_vars,
    find_placeholders,
    parse_rows,
    secret_values,
    suggest_mapping,
    validate_env,
    validate_rows,
)
from browserforge.engine.recipe import Recipe


def parse_mapping(text: str | None) -> dict[str, str] | None:
    """`{"placeholder": "column"}` from JSON text. Blank entries mean 'not mapped'."""
    if text is None or not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise DataError("The column mapping is not valid JSON.") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        raise DataError("The column mapping must be a {placeholder: column} object.")
    return {k: v for k, v in data.items() if isinstance(v, str) and v}


def parse_values(text: str | None) -> dict[str, str] | None:
    """One set of typed values from JSON text: {"first_name": "Aksh", ...}."""
    if text is None or not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise DataError("The typed values are not valid JSON.") from None
    if not isinstance(data, dict):
        raise DataError("The typed values must be a {name: value} object.")
    return {str(key): "" if value is None else str(value) for key, value in data.items()}


def _chosen_mapping(
    needed: set[str], headers: list[str], mapping: Mapping[str, str] | None
) -> dict[str, str]:
    """Placeholder -> column pairs to apply: the user's choices, else the automatic matches."""
    if mapping is not None:
        return {p: h for p, h in mapping.items() if p in needed and h}
    suggested = suggest_mapping(needed, headers)
    return {p: h for p, h in suggested.items() if h is not None}


def _mapped_rows(
    needed: set[str],
    rows: list[dict[str, str]],
    mapping: Mapping[str, str] | None,
) -> list[dict[str, str]]:
    if not rows:
        return rows
    chosen = _chosen_mapping(needed, list(rows[0]), mapping)
    return apply_mapping(rows, chosen)


def _require_placeholders(needed: set[str]) -> None:
    if not needed:
        raise DataError(
            "This recipe has no {{placeholders}}, so there is nothing to fill in from data. "
            "Run it normally."
        )


def prepare_single(
    recipe: Recipe, environ: Mapping[str, str] | None = None
) -> tuple[Recipe, list[str]]:
    """A recipe that runs once with no data sheet: (recipe to run, secret values to redact).

    Recipes with `{{placeholders}}` need data, so they are refused here; `{{env:NAME}}` values
    are read from the environment now, so a missing one fails before any browser starts.
    """
    needed = find_placeholders(recipe)
    if needed:
        names = ", ".join("{{" + name + "}}" for name in sorted(needed))
        raise DataError(
            f"This recipe needs values for {names}. Run it with a data file or typed values."
        )
    env_vars = find_env_vars(recipe)
    problems = validate_env(env_vars, environ)
    if problems:
        raise DataError("\n".join(problems))
    return expand_recipe(recipe, {}, environ), secret_values({}, env_vars, environ)


def build_rows(
    recipe: Recipe,
    *,
    raw: bytes | None,
    filename: str = "",
    values: Mapping[str, str] | None = None,
    mapping: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """The checked rows for a run. Raises DataError listing every problem found.

    `raw`/`filename` are an uploaded sheet; otherwise `values` is one typed set of values.
    """
    needed = find_placeholders(recipe)
    _require_placeholders(needed)
    if raw is not None:
        rows = parse_rows(raw, filename)
    elif values is not None:
        rows = [dict(values)]
    else:
        raise DataError("Add a data file, or type the values, so the recipe has data to fill in.")

    rows = _mapped_rows(needed, rows, mapping)
    problems = validate_rows(needed, rows) + validate_env(find_env_vars(recipe), environ)
    if problems:
        raise DataError("\n".join(problems))
    return rows


def preview_data(
    recipe: Recipe,
    raw: bytes,
    filename: str,
    mapping: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """What the dashboard shows before a bulk run: columns, the mapping, and any problems."""
    needed = find_placeholders(recipe)
    _require_placeholders(needed)
    rows = parse_rows(raw, filename)
    headers = list(rows[0]) if rows else []
    chosen = _chosen_mapping(needed, headers, mapping)
    mapped = apply_mapping(rows, chosen) if rows else rows
    problems = validate_rows(needed, mapped) + validate_env(find_env_vars(recipe), environ)
    return {
        "rows": len(rows),
        "headers": headers,
        "placeholders": sorted(needed),
        "mapping": {p: chosen.get(p) for p in sorted(needed)},
        "problems": problems,
    }
