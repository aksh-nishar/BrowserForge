"""Finding recipe files in the recipes folder: safe names, lookup by name, listing."""

import re
from pathlib import Path

RECIPE_EXTENSIONS = (".yaml", ".yml")


def safe_recipe_name(filename: str) -> str | None:
    """Turn an uploaded file name into a safe recipe name (letters, digits, _ and -)."""
    stem = Path(filename.replace("\\", "/")).stem
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_-")[:64]
    return name or None


def find_recipe(recipes_dir: Path, name: str) -> Path | None:
    """Path of the recipe called `name`, preferring .yaml over .yml, or None."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        return None
    for extension in RECIPE_EXTENSIONS:
        candidate = recipes_dir / f"{name}{extension}"
        if candidate.is_file():
            return candidate
    return None


def list_recipe_files(recipes_dir: Path) -> list[Path]:
    """One file per recipe name, .yaml preferred over .yml, sorted by name."""
    if not recipes_dir.is_dir():
        return []
    by_name: dict[str, Path] = {}
    for extension in reversed(RECIPE_EXTENSIONS):  # .yml first so .yaml overwrites it
        for path in recipes_dir.glob(f"*{extension}"):
            by_name[path.stem] = path
    return [by_name[name] for name in sorted(by_name)]
