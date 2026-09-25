"""Saved profiles: name/value rules, matching to placeholders, and the on-disk store."""

import json
from pathlib import Path

import pytest

from browserforge.profiles import (
    MAX_FIELDS,
    MAX_PROFILES,
    MAX_VALUE_CHARS,
    ProfileError,
    ProfileStore,
    apply_profile,
    check_name,
    clean_values,
)

# ---- check_name ----


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("Aksh", "Aksh"),
        ("  Home   address 2 ", "Home address 2"),
        ("a.b-c_d", "a.b-c_d"),
        ("7up", "7up"),
    ],
)
def test_good_profile_names(typed: str, expected: str) -> None:
    assert check_name(typed) == expected


@pytest.mark.parametrize(
    "typed", ["", "   ", "-start", ".hidden", "a/b", "../x", "<script>", "x" * 41, "naïve"]
)
def test_bad_profile_names_are_refused(typed: str) -> None:
    with pytest.raises(ProfileError, match="profile name"):
        check_name(typed)


# ---- clean_values ----


def test_values_are_stripped_and_turned_into_text() -> None:
    kept, dropped = clean_values({"first_name": "  Ann ", "age": 30, "born": None, " ": "x"})
    assert kept == {"first_name": "Ann", "age": "30"}
    assert dropped == []


def test_blank_values_are_left_out() -> None:
    kept, _ = clean_values({"a": "", "b": "   ", "c": "x"})
    assert kept == {"c": "x"}


def test_secret_looking_names_are_dropped_and_reported() -> None:
    kept, dropped = clean_values(
        {"email": "a@x.io", "password": "hunter22", "Aadhaar": "1234", "pin": "9"}
    )
    assert kept == {"email": "a@x.io"}
    assert sorted(dropped) == ["Aadhaar", "password", "pin"]


def test_a_secret_with_a_blank_value_is_not_even_reported() -> None:
    assert clean_values({"password": ""}) == ({}, [])


def test_a_value_over_the_limit_is_refused() -> None:
    with pytest.raises(ProfileError, match="too long"):
        clean_values({"notes": "x" * (MAX_VALUE_CHARS + 1)})
    assert clean_values({"notes": "x" * MAX_VALUE_CHARS})[0]["notes"]


def test_a_very_long_field_name_is_refused() -> None:
    with pytest.raises(ProfileError, match="too long"):
        clean_values({"n" * 200: "x"})


def test_too_many_fields_are_refused() -> None:
    many = {f"field_{i}": "x" for i in range(MAX_FIELDS + 1)}
    with pytest.raises(ProfileError, match="at most"):
        clean_values(many)
    assert len(clean_values({f"field_{i}": "x" for i in range(MAX_FIELDS)})[0]) == MAX_FIELDS


# ---- apply_profile ----

PROFILE = {"first_name": "Ann", "E-mail": "a@x.io", "city": "Pune"}


def test_exact_and_loose_names_are_filled() -> None:
    values, missing = apply_profile(["first_name", "email", "city"], PROFILE)
    assert values == {"first_name": "Ann", "email": "a@x.io", "city": "Pune"}
    assert missing == []


def test_placeholders_the_profile_lacks_are_reported_missing() -> None:
    values, missing = apply_profile(["email", "phone", "zip"], PROFILE)
    assert values == {"email": "a@x.io"}
    assert missing == ["phone", "zip"]


def test_secret_placeholders_are_never_filled_even_if_a_profile_holds_one() -> None:
    values, missing = apply_profile(["email", "password"], {**PROFILE, "password": "hunter22"})
    assert values == {"email": "a@x.io"}
    assert missing == ["password"]


def test_two_equally_good_matches_are_left_for_the_user() -> None:
    values, missing = apply_profile(["email"], {"E-mail": "a@x.io", "Email!": "b@x.io"})
    assert values == {}
    assert missing == ["email"]


def test_results_are_sorted_and_repeats_collapse() -> None:
    values, missing = apply_profile(["city", "zip", "city", "a_missing"], PROFILE)
    assert list(values) == ["city"]
    assert missing == ["a_missing", "zip"]


def test_no_placeholders_or_an_empty_profile_is_fine() -> None:
    assert apply_profile([], PROFILE) == ({}, [])
    assert apply_profile(["email"], {}) == ({}, ["email"])


# ---- ProfileStore ----


@pytest.fixture
def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(tmp_path / "data")


def test_a_new_store_is_empty_and_touches_no_files(tmp_path: Path) -> None:
    fresh = ProfileStore(tmp_path / "data")
    assert fresh.names() == []
    assert fresh.get("x") is None
    assert not (tmp_path / "data").exists()


def test_a_saved_profile_comes_back_after_a_restart(tmp_path: Path) -> None:
    ProfileStore(tmp_path).save("Aksh", {"first_name": "Aksh", "city": "Surat"})
    assert ProfileStore(tmp_path).get("Aksh") == {"first_name": "Aksh", "city": "Surat"}


def test_save_returns_what_was_kept_and_what_was_dropped(store: ProfileStore) -> None:
    kept, dropped = store.save("Ann", {"email": "a@x.io", "password": "pw123456"})
    assert kept == {"email": "a@x.io"}
    assert dropped == ["password"]
    assert store.get("Ann") == {"email": "a@x.io"}


def test_secrets_never_reach_the_file(store: ProfileStore, tmp_path: Path) -> None:
    store.save("Ann", {"email": "a@x.io", "password": "hunter22", "pin": "424242"})
    text = (tmp_path / "data" / "profiles.json").read_text(encoding="utf-8")
    assert "hunter22" not in text
    assert "424242" not in text


def test_saving_again_replaces_the_profile(store: ProfileStore) -> None:
    store.save("Ann", {"email": "old@x.io", "city": "Pune"})
    store.save("Ann", {"email": "new@x.io"})
    assert store.get("Ann") == {"email": "new@x.io"}


def test_the_name_is_tidied_when_saving(store: ProfileStore) -> None:
    store.save("  Home   address ", {"city": "Pune"})
    assert store.names() == ["Home address"]


def test_names_are_sorted_ignoring_case(store: ProfileStore) -> None:
    for name in ("banana", "Apple", "cherry"):
        store.save(name, {"x": "1"})
    assert store.names() == ["Apple", "banana", "cherry"]


def test_get_returns_a_copy(store: ProfileStore) -> None:
    store.save("Ann", {"email": "a@x.io"})
    found = store.get("Ann")
    assert found is not None
    found["email"] = "changed"
    assert store.get("Ann") == {"email": "a@x.io"}


def test_delete_removes_only_that_profile(store: ProfileStore) -> None:
    store.save("A", {"x": "1"})
    store.save("B", {"x": "2"})
    assert store.delete("A") is True
    assert store.names() == ["B"]
    assert store.delete("A") is False


def test_saving_only_secrets_or_nothing_is_an_error_and_writes_no_file(
    store: ProfileStore, tmp_path: Path
) -> None:
    with pytest.raises(ProfileError, match="nothing to save"):
        store.save("Ann", {"password": "hunter22"})
    with pytest.raises(ProfileError, match="nothing to save"):
        store.save("Ann", {})
    assert not (tmp_path / "data" / "profiles.json").exists()


def test_a_bad_name_is_refused_by_the_store_too(store: ProfileStore) -> None:
    with pytest.raises(ProfileError):
        store.save("../evil", {"x": "1"})


def test_the_number_of_profiles_is_limited_but_replacing_is_always_allowed(
    store: ProfileStore,
) -> None:
    for number in range(MAX_PROFILES):
        store.save(f"p{number}", {"x": "1"})
    with pytest.raises(ProfileError, match="at most"):
        store.save("one-too-many", {"x": "1"})
    store.save("p0", {"x": "changed"})  # replacing an existing one is fine
    assert store.get("p0") == {"x": "changed"}


def test_unicode_values_are_stored_readably(store: ProfileStore, tmp_path: Path) -> None:
    store.save("Ann", {"city": "Zürich", "name": "日本"})
    assert store.get("Ann") == {"city": "Zürich", "name": "日本"}
    assert "Zürich" in (tmp_path / "data" / "profiles.json").read_text(encoding="utf-8")


def test_a_corrupt_file_is_kept_aside_and_the_store_keeps_working(
    store: ProfileStore, tmp_path: Path
) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "profiles.json").write_text("{ nope", encoding="utf-8")
    assert store.names() == []
    assert (folder / "profiles.json.corrupt").read_text(encoding="utf-8") == "{ nope"
    store.save("Ann", {"x": "1"})
    assert store.names() == ["Ann"]


@pytest.mark.parametrize("content", ["[]", "null", '{"profiles": []}', '{"other": {}}'])
def test_a_file_of_the_wrong_shape_is_treated_as_corrupt(
    store: ProfileStore, tmp_path: Path, content: str
) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "profiles.json").write_text(content, encoding="utf-8")
    assert store.names() == []
    assert (folder / "profiles.json.corrupt").is_file()


def test_entries_that_are_not_objects_are_ignored(store: ProfileStore, tmp_path: Path) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "profiles.json").write_text(
        json.dumps({"profiles": {"good": {"x": "1"}, "bad": "not an object", "num": 5}}),
        encoding="utf-8",
    )
    assert store.names() == ["good"]
