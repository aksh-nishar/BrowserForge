"""The shared atomic JSON helpers behind the schedule and profile stores."""

import json
from pathlib import Path
from typing import Any

import pytest

from browserforge.jsonfile import load_json, save_json


def whole(raw: Any) -> Any:
    return raw


def items(raw: Any) -> list[Any]:
    return list(raw["items"])


def test_a_missing_file_loads_as_none(tmp_path: Path) -> None:
    assert load_json(tmp_path / "nope.json", whole) is None


def test_save_then_load_round_trips_and_creates_folders(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "er" / "x.json"
    save_json(path, {"items": [1, "two", {"three": 3}]})
    assert load_json(path, items) == [1, "two", {"three": 3}]


def test_saved_json_keeps_unicode_readable(tmp_path: Path) -> None:
    save_json(tmp_path / "x.json", {"name": "Zoë 日本"})
    assert "Zoë 日本" in (tmp_path / "x.json").read_text(encoding="utf-8")


def test_saving_leaves_no_temporary_files(tmp_path: Path) -> None:
    save_json(tmp_path / "x.json", {"a": 1})
    save_json(tmp_path / "x.json", {"a": 2})
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_a_failed_save_keeps_the_old_file_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "x.json"
    save_json(path, {"version": 1})

    def broken_replace(*_args: Any) -> None:
        raise OSError("disk exploded")

    monkeypatch.setattr("browserforge.jsonfile.os.replace", broken_replace)
    with pytest.raises(OSError, match="disk exploded"):
        save_json(path, {"version": 2})
    monkeypatch.undo()

    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]  # no stray temp file


def test_unparseable_json_is_kept_aside(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text("{ broken", encoding="utf-8")
    assert load_json(path, whole) is None
    assert not path.exists()
    assert (tmp_path / "x.json.corrupt").read_text(encoding="utf-8") == "{ broken"


def test_a_shape_function_that_raises_also_quarantines_the_file(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text('{"other": 1}', encoding="utf-8")
    assert load_json(path, items) is None
    assert (tmp_path / "x.json.corrupt").is_file()


def test_an_older_corrupt_copy_is_replaced_by_a_newer_one(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text("first", encoding="utf-8")
    load_json(path, whole)
    path.write_text("second", encoding="utf-8")
    load_json(path, whole)
    assert (tmp_path / "x.json.corrupt").read_text(encoding="utf-8") == "second"
