import copy

from browserforge.engine.table_extract import extract_table

# ---- normal input ----


def test_header_plus_three_rows_becomes_list_of_dicts() -> None:
    raw = [["name", "age"], ["Ann", "30"], ["Bob", "25"], ["Cy", "41"]]
    assert extract_table(raw) == [
        {"name": "Ann", "age": "30"},
        {"name": "Bob", "age": "25"},
        {"name": "Cy", "age": "41"},
    ]


def test_single_data_row() -> None:
    assert extract_table([["a", "b"], ["1", "2"]]) == [{"a": "1", "b": "2"}]


def test_column_order_follows_the_header() -> None:
    row = extract_table([["z", "a", "m"], ["1", "2", "3"]])[0]
    assert list(row) == ["z", "a", "m"]


# ---- empty input ----


def test_header_only_returns_empty_list() -> None:
    assert extract_table([["name", "age"]]) == []


def test_no_rows_returns_empty_list() -> None:
    assert extract_table([]) == []


def test_no_rows_without_header_returns_empty_list() -> None:
    assert extract_table([], has_header=False) == []


# ---- ragged rows: width = widest row, short rows pad with None ----


def test_short_row_is_padded_with_none() -> None:
    raw = [["name", "age", "city"], ["Ann", "30"], ["Bob"]]
    assert extract_table(raw) == [
        {"name": "Ann", "age": "30", "city": None},
        {"name": "Bob", "age": None, "city": None},
    ]


def test_empty_data_row_becomes_all_none() -> None:
    assert extract_table([["a", "b"], []]) == [{"a": None, "b": None}]


def test_row_longer_than_header_gets_positional_key_for_extra_cells() -> None:
    raw = [["name", "age"], ["Ann", "30", "extra"], ["Bob", "25"]]
    assert extract_table(raw) == [
        {"name": "Ann", "age": "30", "col_2": "extra"},
        {"name": "Bob", "age": "25", "col_2": None},
    ]


def test_all_rows_share_the_same_keys() -> None:
    raw = [["a"], ["1", "2", "3"], ["4"], []]
    keys = {tuple(row) for row in extract_table(raw)}
    assert keys == {("a", "col_1", "col_2")}


# ---- no header ----


def test_no_header_uses_positional_keys_and_keeps_every_row() -> None:
    raw = [["Ann", "30"], ["Bob", "25"]]
    assert extract_table(raw, has_header=False) == [
        {"col_0": "Ann", "col_1": "30"},
        {"col_0": "Bob", "col_1": "25"},
    ]


def test_no_header_ragged_rows_pad_to_widest() -> None:
    assert extract_table([["a"], ["b", "c"]], has_header=False) == [
        {"col_0": "a", "col_1": None},
        {"col_0": "b", "col_1": "c"},
    ]


# ---- duplicate and blank headers ----


def test_duplicate_headers_are_numbered() -> None:
    raw = [["name", "name", "name"], ["a", "b", "c"]]
    assert extract_table(raw) == [{"name": "a", "name_2": "b", "name_3": "c"}]


def test_dedupe_does_not_collide_with_an_existing_numbered_header() -> None:
    raw = [["a", "a", "a_2"], ["1", "2", "3"]]
    assert extract_table(raw) == [{"a": "1", "a_2": "2", "a_2_2": "3"}]


def test_blank_header_cells_become_positional_keys() -> None:
    raw = [["name", "", "  "], ["Ann", "x", "y"]]
    assert extract_table(raw) == [{"name": "Ann", "col_1": "x", "col_2": "y"}]


def test_header_names_are_stripped() -> None:
    assert extract_table([["  name ", "age\n"], ["Ann", "30"]]) == [{"name": "Ann", "age": "30"}]


# ---- values ----


def test_cell_values_are_not_modified() -> None:
    raw = [["a"], ["  padded  "], [""], ["line\nbreak"]]
    assert [row["a"] for row in extract_table(raw)] == ["  padded  ", "", "line\nbreak"]


def test_empty_string_cell_is_kept_distinct_from_missing_cell() -> None:
    rows = extract_table([["a", "b"], ["", "x"], ["y"]])
    assert rows[0]["a"] == ""
    assert rows[1]["b"] is None


def test_unicode_and_long_values_pass_through() -> None:
    long_text = "x" * 10_000
    rows = extract_table([["नाम", "note"], ["रवि", long_text]])
    assert rows == [{"नाम": "रवि", "note": long_text}]


def test_input_is_not_mutated() -> None:
    raw = [["a", "a"], ["1"], []]
    snapshot = copy.deepcopy(raw)
    extract_table(raw)
    assert raw == snapshot
