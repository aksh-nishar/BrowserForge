"""Checking user-supplied data (sheet, typed values, mapping) before a bulk run."""

import pytest

from browserforge.data_input import (
    build_rows,
    parse_mapping,
    parse_values,
    prepare_single,
    preview_data,
)
from browserforge.engine.data_rows import DataError
from browserforge.engine.recipe import Recipe, parse_recipe_text

TEXT = (
    "url: https://x.io\n"
    "steps:\n"
    "  - type: fill\n"
    '    selector: "#e"\n'
    '    value: "{{email}}"\n'
    "  - type: fill\n"
    '    selector: "#n"\n'
    '    value: "{{first_name}}"\n'
)
CSV = b"E-mail,First Name,City\na@x.io,Ann,Pune\nb@x.io,Bob,Delhi\n"


def recipe(text: str = TEXT) -> Recipe:
    return parse_recipe_text(text)


# ---- parse_mapping / parse_values ----


def test_parse_mapping_reads_json_and_drops_blank_choices() -> None:
    assert parse_mapping('{"email": "E-mail", "first_name": ""}') == {"email": "E-mail"}


def test_parse_mapping_of_nothing_is_none() -> None:
    assert parse_mapping(None) is None
    assert parse_mapping("  ") is None


@pytest.mark.parametrize("bad", ["not json", "[1, 2]", '"text"', "5"])
def test_parse_mapping_rejects_bad_input(bad: str) -> None:
    with pytest.raises(DataError, match="mapping"):
        parse_mapping(bad)


def test_parse_values_turns_everything_into_text() -> None:
    assert parse_values('{"age": 30, "name": "Ann", "x": null}') == {
        "age": "30",
        "name": "Ann",
        "x": "",
    }


def test_parse_values_of_nothing_is_none() -> None:
    assert parse_values(None) is None
    assert parse_values("") is None


@pytest.mark.parametrize("bad", ["nope", "[1]"])
def test_parse_values_rejects_bad_input(bad: str) -> None:
    with pytest.raises(DataError, match="typed values"):
        parse_values(bad)


# ---- build_rows ----


def test_sheet_columns_are_matched_to_placeholders_automatically() -> None:
    rows = build_rows(recipe(), raw=CSV, filename="p.csv", environ={})
    assert [(r["email"], r["first_name"]) for r in rows] == [("a@x.io", "Ann"), ("b@x.io", "Bob")]


def test_an_explicit_mapping_wins_over_the_guess() -> None:
    raw = b"mail1,mail2,name\na@x.io,z@x.io,Ann\n"
    rows = build_rows(
        recipe(),
        raw=raw,
        filename="p.csv",
        mapping={"email": "mail2", "first_name": "name"},
        environ={},
    )
    assert rows[0]["email"] == "z@x.io"


def test_typed_values_make_a_single_row() -> None:
    rows = build_rows(
        recipe(), raw=None, values={"email": "a@x.io", "first_name": "Ann"}, environ={}
    )
    assert rows == [{"email": "a@x.io", "first_name": "Ann"}]


def test_no_data_at_all_is_a_clear_error() -> None:
    with pytest.raises(DataError, match="data file"):
        build_rows(recipe(), raw=None, environ={})


def test_a_recipe_without_placeholders_refuses_data() -> None:
    plain = recipe("url: https://x.io\n")
    with pytest.raises(DataError, match="no {{placeholders}}"):
        build_rows(plain, raw=CSV, filename="p.csv", environ={})


def test_every_problem_is_reported_together() -> None:
    raw = b"E-mail,First Name\na@x.io,\n,Bob\n"
    with pytest.raises(DataError) as info:
        build_rows(recipe(), raw=raw, filename="p.csv", environ={})
    message = str(info.value)
    assert "Row 1: no value for 'first_name'" in message
    assert "Row 2: no value for 'email'" in message


def test_a_missing_column_is_reported() -> None:
    with pytest.raises(DataError, match="no column for 'first_name'"):
        build_rows(recipe(), raw=b"E-mail\na@x.io\n", filename="p.csv", environ={})


def test_missing_environment_variables_are_reported_before_running() -> None:
    text = TEXT + '  - type: fill\n    selector: "#p"\n    value: "{{env:PW}}"\n'
    with pytest.raises(DataError, match="'PW' is not set"):
        build_rows(recipe(text), raw=CSV, filename="p.csv", environ={})


def test_a_header_only_sheet_is_rejected() -> None:
    with pytest.raises(DataError, match="no rows"):
        build_rows(recipe(), raw=b"E-mail,First Name\n", filename="p.csv", environ={})


# ---- preview_data ----


def test_preview_shows_headers_suggested_mapping_and_no_problems() -> None:
    preview = preview_data(recipe(), CSV, "p.csv", environ={})
    assert preview["rows"] == 2
    assert preview["headers"] == ["E-mail", "First Name", "City"]
    assert preview["placeholders"] == ["email", "first_name"]
    assert preview["mapping"] == {"email": "E-mail", "first_name": "First Name"}
    assert preview["problems"] == []


def test_preview_leaves_an_unmatched_placeholder_empty_and_reports_it() -> None:
    preview = preview_data(recipe(), b"E-mail,City\na@x.io,Pune\n", "p.csv", environ={})
    assert preview["mapping"] == {"email": "E-mail", "first_name": None}
    assert preview["problems"] == ["The data has no column for 'first_name'."]


def test_preview_with_a_chosen_mapping_uses_it_and_rechecks() -> None:
    preview = preview_data(
        recipe(),
        b"a,b\n1,2\n",
        "p.csv",
        mapping={"email": "a", "first_name": "b"},
        environ={},
    )
    assert preview["mapping"] == {"email": "a", "first_name": "b"}
    assert preview["problems"] == []


def test_preview_with_a_cleared_choice_is_not_refilled_by_the_guess() -> None:
    preview = preview_data(recipe(), CSV, "p.csv", mapping={"email": "E-mail"}, environ={})
    assert preview["mapping"]["first_name"] is None
    assert preview["problems"] == ["The data has no column for 'first_name'."]


def test_preview_of_a_recipe_with_no_placeholders_is_refused() -> None:
    with pytest.raises(DataError):
        preview_data(recipe("url: https://x.io\n"), CSV, "p.csv")


# ---- prepare_single ----


def test_prepare_single_expands_environment_values_and_returns_secrets() -> None:
    text = (
        'url: https://x.io\nsteps:\n  - type: fill\n    selector: "#p"\n    value: "{{env:PW}}"\n'
    )
    expanded, secrets = prepare_single(recipe(text), {"PW": "letmein99"})
    assert expanded.steps[0].config["value"] == "letmein99"
    assert secrets == ["letmein99"]


def test_prepare_single_needs_no_data_for_a_plain_recipe() -> None:
    expanded, secrets = prepare_single(recipe("url: https://x.io\n"), {})
    assert expanded.url == "https://x.io"
    assert secrets == []


def test_prepare_single_refuses_a_recipe_that_needs_data() -> None:
    with pytest.raises(DataError, match=r"\{\{email\}\}, \{\{first_name\}\}"):
        prepare_single(recipe(), {})


def test_prepare_single_names_a_missing_environment_variable() -> None:
    text = (
        'url: https://x.io\nsteps:\n  - type: fill\n    selector: "#p"\n    value: "{{env:PW}}"\n'
    )
    with pytest.raises(DataError, match="'PW' is not set"):
        prepare_single(recipe(text), {})
