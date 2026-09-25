import pytest
from structlog.testing import capture_logs

from browserforge.engine.actions import do_extract_table, do_paginate
from browserforge.engine.driver import DriverError
from browserforge.engine.pagination import has_next_page, paginate_and_collect
from browserforge.engine.recipe import Step
from fixtures.fake_driver import FakePageDriver

HEADER = ["id", "name"]


def three_pages() -> list[list[list[str]]]:
    return [
        [HEADER, ["1", "Ann"], ["2", "Bob"]],
        [HEADER, ["3", "Cy"], ["4", "Di"]],
        [HEADER, ["5", "Ed"]],
    ]


def paged_fake(
    pages: list[list[list[str]]] | None = None, infinite: bool = False
) -> FakePageDriver:
    fake = FakePageDriver()
    fake.set_pages("#tbl", "#next", pages or three_pages(), infinite=infinite)
    return fake


# ---- has_next_page (pure) ----


def test_enabled_and_visible_button_means_next_page() -> None:
    assert has_next_page({"visible": True, "disabled": False}) is True


def test_disabled_button_means_no_next_page() -> None:
    assert has_next_page({"visible": True, "disabled": True}) is False


def test_hidden_button_means_no_next_page() -> None:
    assert has_next_page({"visible": False, "disabled": False}) is False


def test_absent_button_means_no_next_page() -> None:
    assert has_next_page(None) is False


def test_flags_missing_from_state_default_to_visible_and_enabled() -> None:
    assert has_next_page({}) is True
    assert has_next_page({"disabled": True}) is False
    assert has_next_page({"visible": False}) is False


# ---- paginate_and_collect ----


def test_collects_all_three_pages_in_order_with_one_header() -> None:
    fake = paged_fake()
    result = paginate_and_collect(fake, "#tbl", "#next")
    assert result.rows == [
        HEADER,
        ["1", "Ann"],
        ["2", "Bob"],
        ["3", "Cy"],
        ["4", "Di"],
        ["5", "Ed"],
    ]
    assert result.pages == 3
    assert result.hit_cap is False


def test_stops_at_last_page_without_clicking_past_it() -> None:
    fake = paged_fake()
    paginate_and_collect(fake, "#tbl", "#next")
    assert fake.clicks == ["#next", "#next"]
    assert len(fake.calls_to("get_table_rows")) == 3


def test_single_page_never_clicks() -> None:
    fake = paged_fake([[HEADER, ["1", "Ann"]]])
    result = paginate_and_collect(fake, "#tbl", "#next")
    assert result.pages == 1
    assert result.rows == [HEADER, ["1", "Ann"]]
    assert fake.clicks == []


def test_absent_next_button_means_single_page() -> None:
    fake = FakePageDriver()
    fake.set_table("#tbl", [HEADER, ["1", "Ann"]])
    fake.set_missing("#next")
    result = paginate_and_collect(fake, "#tbl", "#next")
    assert result.pages == 1
    assert fake.clicks == []


def test_without_header_every_page_row_is_kept() -> None:
    pages = [[["1", "Ann"]], [["2", "Bob"]], [["3", "Cy"]]]
    result = paginate_and_collect(paged_fake(pages), "#tbl", "#next", has_header=False)
    assert result.rows == [["1", "Ann"], ["2", "Bob"], ["3", "Cy"]]


def test_empty_later_page_is_handled() -> None:
    pages = [[HEADER, ["1", "Ann"]], []]
    result = paginate_and_collect(paged_fake(pages), "#tbl", "#next")
    assert result.rows == [HEADER, ["1", "Ann"]]
    assert result.pages == 2


# ---- safety cap ----


def test_cap_stops_an_endless_next_button() -> None:
    fake = paged_fake([[HEADER, ["a", "1"]], [HEADER, ["b", "2"]]], infinite=True)
    result = paginate_and_collect(fake, "#tbl", "#next", max_pages=5)
    assert result.pages == 5
    assert result.hit_cap is True
    assert len(fake.calls_to("get_table_rows")) == 5
    assert len(fake.clicks) == 4  # never clicks after the last allowed page


def test_cap_of_one_reads_one_page_and_reports_truncation() -> None:
    fake = paged_fake()
    result = paginate_and_collect(fake, "#tbl", "#next", max_pages=1)
    assert result.pages == 1
    assert result.hit_cap is True
    assert fake.clicks == []


def test_cap_equal_to_page_count_is_not_truncation() -> None:
    result = paginate_and_collect(paged_fake(), "#tbl", "#next", max_pages=3)
    assert result.pages == 3
    assert result.hit_cap is False


def test_cap_smaller_than_page_count_truncates() -> None:
    result = paginate_and_collect(paged_fake(), "#tbl", "#next", max_pages=2)
    assert result.pages == 2
    assert result.hit_cap is True
    assert result.rows == [HEADER, ["1", "Ann"], ["2", "Bob"], ["3", "Cy"], ["4", "Di"]]


# ---- do_paginate action ----


def paginate_step(**config: object) -> Step:
    base: dict[str, object] = {"selector": "#tbl", "next_selector": "#next"}
    base.update(config)
    return Step(type="paginate", config=base)


def test_do_paginate_returns_rows_as_dicts_with_page_count() -> None:
    result = do_paginate(paged_fake(), paginate_step())
    assert result.success
    assert result.data["pages"] == 3
    assert result.data["truncated"] is False
    assert result.data["rows"] == [
        {"id": "1", "name": "Ann"},
        {"id": "2", "name": "Bob"},
        {"id": "3", "name": "Cy"},
        {"id": "4", "name": "Di"},
        {"id": "5", "name": "Ed"},
    ]


def test_do_paginate_honours_max_pages_and_flags_truncation() -> None:
    with capture_logs() as logs:
        result = do_paginate(paged_fake(), paginate_step(max_pages=2))
    assert result.success
    assert result.data["truncated"] is True
    assert len(result.data["rows"]) == 4
    assert [e["event"] for e in logs] == ["pagination_cap_reached"]


def test_do_paginate_default_cap_stops_an_endless_button() -> None:
    fake = paged_fake([[HEADER, ["a", "1"]], [HEADER, ["b", "2"]]], infinite=True)
    result = do_paginate(fake, paginate_step())
    assert result.success
    assert result.data["pages"] == 50
    assert result.data["truncated"] is True


def test_do_paginate_without_header() -> None:
    fake = paged_fake([[["1", "Ann"]], [["2", "Bob"]]])
    result = do_paginate(fake, paginate_step(has_header=False))
    assert result.data["rows"] == [
        {"col_0": "1", "col_1": "Ann"},
        {"col_0": "2", "col_1": "Bob"},
    ]


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        ({"selector": None}, "selector"),
        ({"selector": "  "}, "selector"),
        ({"next_selector": None}, "next_selector"),
        ({"next_selector": 5}, "next_selector"),
        ({"max_pages": 0}, "max_pages"),
        ({"max_pages": -1}, "max_pages"),
        ({"max_pages": "ten"}, "max_pages"),
        ({"max_pages": True}, "max_pages"),
        ({"has_header": "yes"}, "has_header"),
    ],
)
def test_do_paginate_bad_config_fails_without_calling_driver(
    config: dict[str, object], needle: str
) -> None:
    fake = paged_fake()
    base: dict[str, object] = {"selector": "#tbl", "next_selector": "#next"}
    base.update(config)
    step = Step(type="paginate", config={k: v for k, v in base.items() if v is not None})
    result = do_paginate(fake, step)
    assert not result.success
    assert result.error is not None
    assert needle in result.error
    assert fake.calls == []


def test_do_paginate_missing_table_is_a_failed_result() -> None:
    fake = FakePageDriver()  # no table scripted
    result = do_paginate(fake, paginate_step())
    assert not result.success
    assert result.error is not None
    assert "#tbl" in result.error
    assert result.data == {}


def test_do_paginate_click_failure_mid_run_fails_without_partial_data() -> None:
    fake = paged_fake()
    fake.fail_on("click", DriverError("next button vanished"))
    result = do_paginate(fake, paginate_step())
    assert not result.success
    assert result.error is not None
    assert "next button vanished" in result.error
    assert result.data == {}


# ---- do_extract_table action ----


def test_do_extract_table_returns_dict_rows() -> None:
    fake = FakePageDriver()
    fake.set_table("#tbl", [HEADER, ["1", "Ann"], ["2", "Bob"]])
    result = do_extract_table(fake, Step(type="extract_table", config={"selector": "#tbl"}))
    assert result.success
    assert result.data["rows"] == [{"id": "1", "name": "Ann"}, {"id": "2", "name": "Bob"}]
    assert fake.calls == [("get_table_rows", ("#tbl",))]


def test_do_extract_table_without_header() -> None:
    fake = FakePageDriver()
    fake.set_table("#tbl", [["1", "Ann"]])
    step = Step(type="extract_table", config={"selector": "#tbl", "has_header": False})
    assert do_extract_table(fake, step).data["rows"] == [{"col_0": "1", "col_1": "Ann"}]


def test_do_extract_table_missing_selector_fails_without_calling_driver() -> None:
    fake = FakePageDriver()
    result = do_extract_table(fake, Step(type="extract_table"))
    assert not result.success
    assert result.error is not None
    assert "selector" in result.error
    assert fake.calls == []


def test_do_extract_table_bad_has_header_fails() -> None:
    fake = FakePageDriver()
    step = Step(type="extract_table", config={"selector": "#tbl", "has_header": 1})
    result = do_extract_table(fake, step)
    assert not result.success
    assert fake.calls == []


def test_do_extract_table_missing_table_is_a_failed_result() -> None:
    result = do_extract_table(
        FakePageDriver(), Step(type="extract_table", config={"selector": "#nope"})
    )
    assert not result.success
    assert result.error is not None
    assert "#nope" in result.error
