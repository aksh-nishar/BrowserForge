"""Saving schedules to disk: persistence, corruption handling and data sheets."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from browserforge.schedule import Schedule
from browserforge.schedule_store import ScheduleStore


def make(schedule_id: str = "abc12345", **changes: object) -> Schedule:
    base = Schedule(id=schedule_id, recipe_name="demo", when="daily 08:00", cron="0 8 * * *")
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "data")


# ---- basics ----


def test_a_new_store_is_empty_and_touches_no_files(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "data")
    assert store.all() == []
    assert store.get("x") is None
    assert not (tmp_path / "data").exists()


def test_added_schedules_come_back_after_a_restart(tmp_path: Path) -> None:
    ScheduleStore(tmp_path).add(make("aaaa1111"))
    ScheduleStore(tmp_path).add(make("bbbb2222", recipe_name="other"))

    fresh = ScheduleStore(tmp_path)  # a new object, like after a server restart
    assert [s.id for s in fresh.all()] == ["aaaa1111", "bbbb2222"]
    assert fresh.get("bbbb2222") is not None
    assert fresh.get("bbbb2222").recipe_name == "other"  # type: ignore[union-attr]


def test_the_same_id_cannot_be_added_twice(store: ScheduleStore) -> None:
    store.add(make())
    with pytest.raises(ValueError, match="already exists"):
        store.add(make())
    assert len(store.all()) == 1


def test_update_changes_fields_and_persists_them(store: ScheduleStore) -> None:
    store.add(make())
    updated = store.update("abc12345", enabled=False, last_job_id="j1")
    assert updated is not None
    assert updated.enabled is False
    fresh = store.get("abc12345")
    assert fresh is not None
    assert (fresh.enabled, fresh.last_job_id) == (False, "j1")


def test_updating_an_unknown_id_returns_none(store: ScheduleStore) -> None:
    store.add(make())
    assert store.update("nope", enabled=False) is None
    assert store.get("abc12345").enabled is True  # type: ignore[union-attr]


def test_updating_an_unknown_field_is_an_error(store: ScheduleStore) -> None:
    store.add(make())
    with pytest.raises(TypeError):
        store.update("abc12345", not_a_field=1)


def test_remove_deletes_only_that_schedule(store: ScheduleStore) -> None:
    store.add(make("aaaa1111"))
    store.add(make("bbbb2222"))
    assert store.remove("aaaa1111") is True
    assert [s.id for s in store.all()] == ["bbbb2222"]
    assert store.remove("aaaa1111") is False


def test_saving_leaves_no_temporary_files_behind(store: ScheduleStore, tmp_path: Path) -> None:
    store.add(make())
    store.update("abc12345", enabled=False)
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == ["schedules.json"]


def test_the_file_is_plain_readable_json(store: ScheduleStore, tmp_path: Path) -> None:
    store.add(make(mapping={"email": "E-mail"}))
    saved = json.loads((tmp_path / "data" / "schedules.json").read_text(encoding="utf-8"))
    assert saved["schedules"][0]["recipe_name"] == "demo"
    assert saved["schedules"][0]["mapping"] == {"email": "E-mail"}


# ---- damaged files ----


def test_a_corrupt_file_is_kept_aside_and_treated_as_empty(
    store: ScheduleStore, tmp_path: Path
) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "schedules.json").write_text("{ this is not json", encoding="utf-8")

    assert store.all() == []
    assert (folder / "schedules.json.corrupt").read_text(encoding="utf-8") == "{ this is not json"
    store.add(make())  # the store keeps working
    assert [s.id for s in store.all()] == ["abc12345"]
    assert (folder / "schedules.json.corrupt").is_file()  # and the evidence is still there


@pytest.mark.parametrize("content", ["[]", '{"schedules": 5}', "null", '{"other": []}'])
def test_a_file_of_the_wrong_shape_is_treated_as_corrupt(
    store: ScheduleStore, tmp_path: Path, content: str
) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "schedules.json").write_text(content, encoding="utf-8")
    assert store.all() == []
    assert (folder / "schedules.json.corrupt").is_file()


def test_one_bad_entry_does_not_hide_the_good_ones(store: ScheduleStore, tmp_path: Path) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    good = make().to_dict()
    (folder / "schedules.json").write_text(
        json.dumps({"schedules": ["not a dict", {"id": "missing-required-fields"}, good]}),
        encoding="utf-8",
    )
    assert [s.id for s in store.all()] == ["abc12345"]


# ---- data sheets ----


def test_a_data_sheet_is_saved_under_a_fixed_name(store: ScheduleStore, tmp_path: Path) -> None:
    saved = store.add(make(), data=("../../evil name.CSV", b"email\na@x.io\n"))
    assert saved.data_file == "data.csv"  # nothing from the upload becomes a path
    assert (tmp_path / "data" / "schedule_data" / "abc12345" / "data.csv").is_file()
    assert store.read_data(saved) == ("data.csv", b"email\na@x.io\n")


def test_an_xlsx_sheet_keeps_its_type(store: ScheduleStore) -> None:
    saved = store.add(make(), data=("people.xlsx", b"PK"))
    assert saved.data_file == "data.xlsx"


def test_other_file_types_are_refused(store: ScheduleStore) -> None:
    with pytest.raises(ValueError, match=r"\.csv or \.xlsx"):
        store.add(make(), data=("people.txt", b"x"))
    assert store.all() == []  # nothing half-saved


def test_a_schedule_without_a_sheet_reads_none(store: ScheduleStore) -> None:
    assert store.read_data(store.add(make())) is None


def test_removing_a_schedule_deletes_its_sheet(store: ScheduleStore, tmp_path: Path) -> None:
    store.add(make(), data=("p.csv", b"a\n1\n"))
    store.remove("abc12345")
    assert not (tmp_path / "data" / "schedule_data" / "abc12345").exists()


def test_a_missing_sheet_file_reads_none(store: ScheduleStore, tmp_path: Path) -> None:
    saved = store.add(make(), data=("p.csv", b"a\n1\n"))
    (tmp_path / "data" / "schedule_data" / "abc12345" / "data.csv").unlink()
    assert store.read_data(saved) is None


def test_a_tampered_data_file_name_is_never_read(store: ScheduleStore) -> None:
    store.add(make())
    hostile = make(data_file="../../schedules.json")
    assert store.read_data(hostile) is None
