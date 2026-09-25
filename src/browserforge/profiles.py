"""Saved profiles: one person's details, stored once and reused to fill any recipe's placeholders.

A profile is a named set of {field name: value} pairs (first_name, email, city...). When a recipe
needs `{{email}}`, the profile's value is matched by name, ignoring case and punctuation, the same
way sheet columns are matched. Secrets (passwords, PINs, ID numbers) are never stored: keys that
look like secrets are dropped on save, and secret placeholders are always left for the user.
"""

import re
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from browserforge.engine.data_rows import is_secret_name, suggest_mapping
from browserforge.jsonfile import load_json, save_json

PROFILES_FILE = "profiles.json"
MAX_PROFILES = 50
MAX_FIELDS = 100
MAX_KEY_CHARS = 64
MAX_VALUE_CHARS = 1000
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}")


class ProfileError(Exception):
    """A profile name or value is not acceptable. The message is for people."""


def check_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not _NAME.fullmatch(cleaned):
        raise ProfileError(
            "A profile name uses letters, digits, spaces, dots, dashes or underscores "
            "(up to 40 characters, starting with a letter or digit)."
        )
    return cleaned


def clean_values(values: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """(values to keep, names dropped for looking like secrets). Blank values are left out."""
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for raw_key, raw_value in values.items():
        key = str(raw_key).strip()
        value = "" if raw_value is None else str(raw_value).strip()
        if not key or not value:
            continue
        if len(key) > MAX_KEY_CHARS:
            raise ProfileError(f"The field name '{key[:20]}...' is too long.")
        if is_secret_name(key):
            dropped.append(key)
            continue
        if len(value) > MAX_VALUE_CHARS:
            raise ProfileError(f"The value for '{key}' is too long (limit {MAX_VALUE_CHARS}).")
        kept[key] = value
    if len(kept) > MAX_FIELDS:
        raise ProfileError(f"A profile can hold at most {MAX_FIELDS} fields.")
    return kept, dropped


def apply_profile(
    placeholders: Iterable[str], profile: Mapping[str, str]
) -> tuple[dict[str, str], list[str]]:
    """(values found for the placeholders, placeholders the profile could not fill).

    Names are matched loosely ('E-mail' fills 'email'); a name matching two profile fields is
    left for the user rather than guessed; secret-looking placeholders are never filled.
    """
    needed = sorted(set(placeholders))
    mapping = suggest_mapping(needed, profile.keys())
    values: dict[str, str] = {}
    missing: list[str] = []
    for placeholder in needed:
        source = mapping.get(placeholder)
        if source is None or is_secret_name(placeholder):
            missing.append(placeholder)
        else:
            values[placeholder] = profile[source]
    return values, missing


def _shape(raw: Any) -> dict[str, dict[str, str]]:
    profiles = raw["profiles"]
    if not isinstance(profiles, dict):
        raise TypeError("'profiles' must be an object")
    result: dict[str, dict[str, str]] = {}
    for name, values in profiles.items():
        if isinstance(values, dict):
            result[str(name)] = {str(k): str(v) for k, v in values.items()}
    return result


class ProfileStore:
    """`<data dir>/profiles.json`. Thread-safe; an unreadable file is kept aside as `.corrupt`."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / PROFILES_FILE
        self._lock = threading.RLock()

    def _load(self) -> dict[str, dict[str, str]]:
        return load_json(self._path, _shape) or {}

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._load(), key=str.lower)

    def get(self, name: str) -> dict[str, str] | None:
        with self._lock:
            found = self._load().get(name)
            return dict(found) if found is not None else None

    def save(self, name: str, values: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
        """Create or replace a profile. Returns (what was saved, secret-looking names dropped)."""
        clean_name = check_name(name)
        kept, dropped = clean_values(values)
        if not kept:
            raise ProfileError("There is nothing to save: fill in at least one (non-secret) value.")
        with self._lock:
            profiles = self._load()
            if clean_name not in profiles and len(profiles) >= MAX_PROFILES:
                raise ProfileError(
                    f"You can keep at most {MAX_PROFILES} profiles. Delete one first."
                )
            profiles[clean_name] = kept
            save_json(self._path, {"profiles": profiles})
        return kept, dropped

    def delete(self, name: str) -> bool:
        with self._lock:
            profiles = self._load()
            if name not in profiles:
                return False
            del profiles[name]
            save_json(self._path, {"profiles": profiles})
            return True
