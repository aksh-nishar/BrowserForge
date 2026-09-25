"""Small JSON files that must never be half-written or silently thrown away.

Used by the schedule and profile stores: `save_json` replaces the file atomically (a crash never
leaves half a file) and `load_json` keeps an unreadable file aside as `<name>.corrupt` instead
of letting the next save overwrite it.
"""

import contextlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def load_json[T](path: Path, shape: Callable[[Any], T]) -> T | None:
    """`shape(parsed JSON)` for the file at `path`; None if the file does not exist.

    `shape` picks the part that matters and may raise KeyError/TypeError/ValueError for a
    wrong structure. An unreadable or wrongly shaped file is moved to `<name>.corrupt`
    (logged) and None is returned, so the caller starts fresh without destroying the evidence.
    """
    if not path.is_file():
        return None
    try:
        return shape(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        backup = path.with_name(path.name + ".corrupt")
        logger.warning("json_file_unreadable", file=path.name, error=str(exc), kept_as=backup.name)
        with contextlib.suppress(OSError):
            os.replace(path, backup)
        return None


def save_json(path: Path, payload: Any) -> None:
    """Write `payload` as JSON to `path` atomically, creating the folder if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp:
            temp.write(text)
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise
