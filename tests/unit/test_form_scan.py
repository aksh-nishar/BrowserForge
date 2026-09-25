"""Form scan -> recipe: selectors, field planning and recipe building (pure, no browser)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from browserforge.engine.data_rows import expand_recipe, find_placeholders
from browserforge.engine.form_scan import (
    MAX_KEY_LENGTH,
    NOTES,
    PlanError,
    PlannedField,
    build_selector,
    check_url,
    fields_to_recipe,
    normalize_address,
    normalize_label,
    plan_form,
    recipe_to_yaml,
)
from browserforge.engine.recipe import parse_recipe_text
from browserforge.engine.runner import has_submit_step, run_recipe
from fixtures.fake_driver import FakePageDriver, raw_field

URL = "https://forms.example.com/register"


def scan(*fields: Any, notes: list[str] | None = None) -> Any:
    return {"fields": list(fields), "notes": notes or []}


# ---- normalize_label ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("First name", "first_name"),
        ("E-mail Address *", "e_mail_address"),
        ("  Phone  number: ", "phone_number"),
        ("123 Street", "field_123_street"),
        ("Téléphone", "telephone"),
        ("___a___", "a"),
        ("Date of Birth (DD/MM/YYYY)", "date_of_birth_dd_mm_yyyy"),
        ("", ""),
        ("   ", ""),
        ("日本語", ""),
    ],
)
def test_normalize_label(text: str, expected: str) -> None:
    assert normalize_label(text) == expected


def test_normalize_label_shortens_long_labels_without_a_trailing_underscore() -> None:
    result = normalize_label("word " * 30)
    assert len(result) <= MAX_KEY_LENGTH
    assert not result.endswith("_")


# ---- build_selector ----


def test_a_unique_id_is_preferred() -> None:
    assert build_selector(raw_field(id="email", name="email_field")) == "#email"


def test_an_id_that_is_not_a_plain_identifier_uses_an_attribute_selector() -> None:
    assert build_selector(raw_field(id="user.name")) == '[id="user.name"]'
    assert build_selector(raw_field(id="1st")) == '[id="1st"]'


def test_a_shared_id_falls_back_to_the_name() -> None:
    field = raw_field(id="dup", id_count=2, name="email")
    assert build_selector(field) == 'input[name="email"]'


def test_a_unique_name_is_used_with_the_tag() -> None:
    assert build_selector(raw_field(tag="select", name="country")) == 'select[name="country"]'
    assert build_selector(raw_field(tag="textarea", name="notes")) == 'textarea[name="notes"]'


def test_a_name_shared_with_another_element_is_not_used() -> None:
    assert build_selector(raw_field(name="email", name_count=2)) is None


def test_no_id_or_name_means_no_selector() -> None:
    assert build_selector(raw_field(label="Something")) is None


def test_names_with_quotes_are_escaped() -> None:
    assert build_selector(raw_field(name='a"b')) == 'input[name="a\\"b"]'


def test_a_selector_that_would_contain_a_placeholder_is_refused() -> None:
    assert build_selector(raw_field(id="{{x}}")) is None
    assert build_selector(raw_field(name="a{{env:HOME}}")) is None


def test_an_absurdly_long_selector_is_refused() -> None:
    assert build_selector(raw_field(name="n" * 600)) is None


# ---- plan_form: field kinds ----


def test_text_email_and_date_fields_become_fill_steps_named_after_their_labels() -> None:
    plan = plan_form(
        scan(
            raw_field(id="fn", label="First name", required=True),
            raw_field(id="em", type="email", label="E-mail address"),
            raw_field(id="dob", type="date", label="Date of birth"),
        )
    )
    assert [(f.key, f.action, f.selector, f.kind) for f in plan.fields] == [
        ("first_name", "fill", "#fn", "text"),
        ("e_mail_address", "fill", "#em", "email"),
        ("date_of_birth", "fill", "#dob", "date"),
    ]
    assert plan.fields[0].required is True
    assert plan.fields[1].required is False
    assert plan.skipped == []


def test_a_textarea_is_a_fill_step() -> None:
    plan = plan_form(scan(raw_field(tag="textarea", type="textarea", name="notes", label="Notes")))
    assert (plan.fields[0].action, plan.fields[0].kind) == ("fill", "textarea")
    assert plan.fields[0].selector == 'textarea[name="notes"]'


def test_a_dropdown_lists_its_options_without_the_empty_choice() -> None:
    options = [
        {"value": "", "text": "-- choose --"},
        {"value": "IN", "text": "India"},
        {"value": "US", "text": "USA"},
    ]
    plan = plan_form(
        scan(raw_field(tag="select", type="select", id="c", label="Country", options=options))
    )
    (field,) = plan.fields
    assert (field.action, field.kind, field.options) == ("select_option", "select", ("IN", "US"))


def test_a_dropdown_with_no_real_options_is_skipped_with_a_reason() -> None:
    options = [{"value": "", "text": "-- choose --"}]
    plan = plan_form(
        scan(raw_field(tag="select", type="select", id="c", label="Country", options=options))
    )
    assert plan.fields == []
    assert "no options" in plan.skipped[0]


def test_a_checkbox_is_a_check_step_with_no_value_placeholder() -> None:
    plan = plan_form(scan(raw_field(id="agree", type="checkbox", label="I agree")))
    (field,) = plan.fields
    assert (field.action, field.key, field.selector, field.kind) == (
        "check",
        "",
        "#agree",
        "checkbox",
    )
    assert field.label == "I agree"


def test_a_file_input_is_an_upload_step() -> None:
    plan = plan_form(scan(raw_field(id="cv", type="file", label="Upload your CV")))
    (field,) = plan.fields
    assert (field.action, field.key, field.kind) == ("upload", "upload_your_cv", "file")


def test_radio_buttons_of_one_name_become_a_single_choose_step() -> None:
    plan = plan_form(
        scan(
            raw_field(
                type="radio",
                name="plan",
                value="basic",
                label="Basic",
                name_count=2,
                group_label="Plan",
            ),
            raw_field(
                type="radio",
                name="plan",
                value="pro",
                label="Pro",
                name_count=2,
                group_label="Plan",
            ),
        )
    )
    (field,) = plan.fields
    assert field.action == "choose"
    assert field.selector == 'input[type=radio][name="plan"]'
    assert field.options == ("basic", "pro")
    assert field.key == "plan"
    assert field.label == "Plan"


def test_a_radio_group_without_a_legend_is_named_after_its_name_attribute() -> None:
    plan = plan_form(
        scan(
            raw_field(type="radio", name="delivery_speed", value="a", name_count=2),
            raw_field(type="radio", name="delivery_speed", value="b", name_count=2),
        )
    )
    assert plan.fields[0].label == "delivery speed"
    assert plan.fields[0].key == "delivery_speed"


def test_radio_edge_cases_are_skipped_with_reasons() -> None:
    plan = plan_form(
        scan(
            raw_field(type="radio", label="Nameless"),
            raw_field(type="radio", name="empty", value="", name_count=1),
        )
    )
    assert plan.fields == []
    assert len(plan.skipped) == 2
    assert "cannot be grouped" in plan.skipped[0]
    assert "no values" in plan.skipped[1]


def test_a_hidden_radio_does_not_count_as_an_option() -> None:
    plan = plan_form(
        scan(
            raw_field(type="radio", name="g", value="a", name_count=2),
            raw_field(type="radio", name="g", value="secret", name_count=2, visible=False),
        )
    )
    assert plan.fields[0].options == ("a",)


def test_checkboxes_sharing_a_name_are_told_apart_by_their_value() -> None:
    plan = plan_form(
        scan(
            raw_field(type="checkbox", name="topic", value="news", label="News", name_count=2),
            raw_field(type="checkbox", name="topic", value="sport", label="Sport", name_count=2),
        )
    )
    assert [f.selector for f in plan.fields] == [
        'input[type=checkbox][name="topic"][value="news"]',
        'input[type=checkbox][name="topic"][value="sport"]',
    ]


def test_two_controls_that_share_a_selector_keep_only_the_first() -> None:
    plan = plan_form(
        scan(
            raw_field(type="checkbox", name="t", value="x", label="One", name_count=2),
            raw_field(type="checkbox", name="t", value="x", label="Two", name_count=2),
        )
    )
    assert len(plan.fields) == 1
    assert "already uses the same selector" in plan.skipped[0]


# ---- plan_form: what is left out ----


def test_hidden_invisible_and_disabled_controls_are_ignored_silently() -> None:
    plan = plan_form(
        scan(
            raw_field(id="a", type="hidden", label="A", visible=False),
            raw_field(id="b", label="B", visible=False),
            raw_field(id="c", label="C", disabled=True),
            raw_field(id="d", type="reset", label="D"),
            raw_field(id="e", label="E"),
        )
    )
    assert [f.selector for f in plan.fields] == ["#e"]
    assert plan.skipped == []


def test_a_control_with_no_id_or_name_is_skipped_and_explained() -> None:
    plan = plan_form(scan(raw_field(label="Mystery box")))
    assert plan.fields == []
    assert plan.skipped == ["'Mystery box': it has no unique id or name to find it by."]


def test_a_control_whose_id_and_name_are_both_shared_is_skipped() -> None:
    plan = plan_form(scan(raw_field(id="x", id_count=3, name="y", name_count=3, label="Twin")))
    assert plan.fields == []
    assert "Twin" in plan.skipped[0]


def test_unsupported_input_types_are_reported() -> None:
    plan = plan_form(scan(raw_field(id="r", type="range", label="Volume")))
    assert plan.fields == []
    assert plan.skipped == ["'Volume': fields of type 'range' are not supported."]


# ---- plan_form: keys ----


def test_the_key_falls_back_from_label_to_placeholder_to_name_to_id() -> None:
    plan = plan_form(
        scan(
            raw_field(id="a", label="From label", placeholder="ignored"),
            raw_field(id="b", placeholder="From placeholder", name="ignored"),
            raw_field(id="c", name="from_name"),
            raw_field(id="from-id"),
        )
    )
    assert [f.key for f in plan.fields] == [
        "from_label",
        "from_placeholder",
        "from_name",
        "from_id",
    ]


def test_a_control_with_nothing_to_name_it_by_gets_a_numbered_key() -> None:
    plan = plan_form(scan(raw_field(id="日本")))
    assert plan.fields[0].key == "field_1"


def test_repeated_labels_get_numbered_keys() -> None:
    plan = plan_form(
        scan(
            raw_field(id="a", label="Name"),
            raw_field(id="b", label="Name"),
            raw_field(id="c", label="Name"),
        )
    )
    assert [f.key for f in plan.fields] == ["name", "name_2", "name_3"]


def test_password_fields_and_secret_sounding_names_are_marked_secret() -> None:
    plan = plan_form(
        scan(
            raw_field(id="p", type="password", label="Secret word"),
            raw_field(id="q", label="Your PIN"),
            raw_field(id="r", label="Nickname"),
        )
    )
    assert [f.secret for f in plan.fields] == [True, True, False]


def test_the_page_order_of_fields_is_kept() -> None:
    plan = plan_form(scan(raw_field(id="z", label="Zed"), raw_field(id="a", label="Ann")))
    assert [f.key for f in plan.fields] == ["zed", "ann"]


# ---- plan_form: submit buttons ----


def test_submit_buttons_come_first_then_send_like_buttons_then_the_rest() -> None:
    plan = plan_form(
        scan(
            raw_field(tag="button", type="button", id="cancel", label="Cancel"),
            raw_field(tag="button", type="button", id="go", label="Send it"),
            raw_field(tag="input", type="submit", id="real", label="Register"),
        )
    )
    assert [c.selector for c in plan.submit_candidates] == ["#real", "#go", "#cancel"]
    assert plan.submit_candidates[0].label == "Register"
    assert plan.fields == []  # buttons are never form fields


def test_a_button_without_an_id_is_found_by_its_text() -> None:
    plan = plan_form(scan(raw_field(tag="button", type="button", label="Send")))
    assert plan.submit_candidates[0].selector == 'button:text-is("Send")'


def test_an_unnamed_submit_input_is_found_by_its_value() -> None:
    plan = plan_form(scan(raw_field(tag="input", type="submit", label="Go", value="Go")))
    assert plan.submit_candidates[0].selector == 'input[type=submit][value="Go"]'


def test_duplicate_buttons_are_listed_once() -> None:
    button = raw_field(tag="button", type="button", label="Send")
    plan = plan_form(scan(button, button))
    assert len(plan.submit_candidates) == 1


def test_a_page_with_no_usable_button_has_no_candidates() -> None:
    plan = plan_form(scan(raw_field(id="a", label="A")))
    assert plan.submit_candidates == []


def test_button_text_that_looks_like_a_placeholder_is_not_used() -> None:
    plan = plan_form(scan(raw_field(tag="button", type="button", label="{{env:HOME}}")))
    assert plan.submit_candidates == []


# ---- plan_form: notes and serialisation ----


def test_page_notes_are_turned_into_warnings_and_unknown_ones_ignored() -> None:
    plan = plan_form(scan(notes=["captcha", "iframe", "something-new"]))
    assert plan.notes == [NOTES["captcha"], NOTES["iframe"]]


def test_an_empty_page_gives_an_empty_plan() -> None:
    plan = plan_form(scan())
    assert (plan.fields, plan.submit_candidates, plan.skipped, plan.notes) == ([], [], [], [])


def test_the_plan_can_be_turned_into_plain_json_data() -> None:
    plan = plan_form(
        scan(
            raw_field(
                id="c",
                tag="select",
                type="select",
                label="Country",
                options=[{"value": "IN", "text": "India"}],
            ),
            raw_field(tag="button", type="submit", id="go", label="Go"),
        )
    )
    data = plan.to_dict()
    assert data["fields"][0]["options"] == ["IN"]
    assert data["submit_candidates"] == [{"selector": "#go", "label": "Go"}]
    assert set(data) == {"fields", "submit_candidates", "skipped", "notes"}


# ---- check_url ----


@pytest.mark.parametrize("url", [URL, "http://localhost:8000/form", "  https://x.io/a?b=1  "])
def test_web_addresses_are_accepted(url: str) -> None:
    assert check_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "x.io",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://x.io",
        "https://",
        "http://a b",
    ],
)
def test_other_addresses_are_refused(url: str) -> None:
    with pytest.raises(PlanError):
        check_url(url)


def test_an_over_long_address_is_refused() -> None:
    with pytest.raises(PlanError, match="too long"):
        check_url("https://x.io/" + "a" * 2100)


# ---- normalize_address ----


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("example.com/register", "https://example.com/register"),
        ("  forms.example.com  ", "https://forms.example.com"),
        ("localhost:8000/form", "https://localhost:8000/form"),
        ("example.com?page=2", "https://example.com?page=2"),
        ("http://example.com/a", "http://example.com/a"),
        ("https://example.com", "https://example.com"),
    ],
)
def test_a_bare_address_gets_https_and_a_full_one_is_left_alone(typed: str, expected: str) -> None:
    assert normalize_address(typed) == expected


@pytest.mark.parametrize(
    "typed",
    ["javascript:alert(1)", "file:///etc/passwd", "not a url", "", "a b.com", "mailto:x@y.io"],
)
def test_addresses_that_are_not_bare_are_left_for_check_url_to_refuse(typed: str) -> None:
    normalized = normalize_address(typed)
    assert normalized == typed.strip()
    with pytest.raises(PlanError):
        check_url(normalized)


# ---- fields_to_recipe ----


def field(action: str = "fill", key: str = "name", selector: str = "#n", **kw: Any) -> PlannedField:
    return PlannedField(key=key, label=kw.pop("label", key), action=action, selector=selector, **kw)


def full_form() -> list[PlannedField]:
    return [
        field("fill", "first_name", "#fn"),
        field("select_option", "country", "#c"),
        field("choose", "plan", 'input[type=radio][name="plan"]'),
        field("upload", "cv", "#cv"),
        field("check", "", "#agree", label="I agree"),
    ]


def test_every_field_becomes_a_step_with_a_placeholder_value() -> None:
    recipe = fields_to_recipe(URL, full_form(), "#send")
    assert recipe["url"] == URL
    assert recipe["steps"] == [
        {"type": "fill", "name": "first_name", "selector": "#fn", "value": "{{first_name}}"},
        {"type": "select_option", "name": "country", "selector": "#c", "value": "{{country}}"},
        {
            "type": "choose",
            "name": "plan",
            "selector": 'input[type=radio][name="plan"]',
            "value": "{{plan}}",
        },
        {"type": "upload", "name": "cv", "selector": "#cv", "files": "{{cv}}"},
        {"type": "check", "name": "tick_i_agree", "selector": "#agree"},
        {"type": "screenshot", "name": "before_submit"},
        {"type": "click", "name": "submit", "selector": "#send", "submit": True},
        {"type": "screenshot", "name": "after_submit"},
    ]


def test_without_a_submit_button_the_recipe_ends_with_a_screenshot() -> None:
    recipe = fields_to_recipe(URL, [field()], None)
    assert [s["type"] for s in recipe["steps"]] == ["fill", "screenshot"]
    assert recipe["steps"][-1]["name"] == "before_submit"


def test_the_start_address_is_cleaned() -> None:
    assert fields_to_recipe("  https://x.io/a  ", [field()], None)["url"] == "https://x.io/a"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ([], "at least one field"),
        ([field(action="explode")], "unknown action"),
        ([field(selector="  ")], "no selector"),
        ([field(selector="{{env:HOME}}")], "reserved"),
        ([field(selector="a" * 501)], "too long"),
        ([field(key="")], "needs a name"),
        ([field(key="1abc")], "needs a name"),
        ([field(key="two words")], "needs a name"),
        ([field(key="bad-dash")], "needs a name"),
        ([field(key="a", selector="#1"), field(key="a", selector="#2")], "used for two fields"),
    ],
)
def test_bad_field_lists_are_refused_with_a_reason(
    fields: list[PlannedField], message: str
) -> None:
    with pytest.raises(PlanError, match=message):
        fields_to_recipe(URL, fields, "#send")


@pytest.mark.parametrize("bad_url", ["", "file:///etc/passwd", "javascript:1"])
def test_a_bad_start_address_is_refused(bad_url: str) -> None:
    with pytest.raises(PlanError):
        fields_to_recipe(bad_url, [field()], None)


def test_a_bad_submit_selector_is_refused() -> None:
    with pytest.raises(PlanError, match="submit button"):
        fields_to_recipe(URL, [field()], "{{x}}")


def test_two_checkboxes_may_share_the_empty_key() -> None:
    recipe = fields_to_recipe(
        URL,
        [field("check", "", "#a", label="A"), field("check", "", "#b", label="B")],
        None,
    )
    assert [s["name"] for s in recipe["steps"][:2]] == ["tick_a", "tick_b"]


# ---- recipe_to_yaml ----


def test_the_yaml_is_a_valid_recipe_with_the_expected_placeholders() -> None:
    text = recipe_to_yaml(fields_to_recipe(URL, full_form(), "#send"))
    recipe = parse_recipe_text(text)
    assert find_placeholders(recipe) == {"first_name", "country", "plan", "cv"}
    assert has_submit_step(recipe) is True


def test_the_yaml_starts_with_a_readable_header() -> None:
    text = recipe_to_yaml(fields_to_recipe(URL, [field()], None))
    assert text.startswith(f"# Generated by BrowserForge from {URL}\n")
    assert "{{name}}" in text.replace("'{{name}}'", "{{name}}")


def test_awkward_selectors_and_labels_survive_the_round_trip() -> None:
    tricky = field(key="q", selector='input[name="a: b # c"]', label="Why: 'because'?")
    text = recipe_to_yaml(fields_to_recipe(URL, [tricky], None))
    loaded = yaml.safe_load(text)
    assert loaded["steps"][0]["selector"] == 'input[name="a: b # c"]'


def test_the_address_in_the_header_cannot_start_a_new_line() -> None:
    text = recipe_to_yaml({"url": "https://x.io/a\nurl: https://evil", "steps": []})
    assert text.splitlines()[0] == "# Generated by BrowserForge from https://x.io/aurl:https://evil"


def test_a_recipe_the_parser_rejects_is_a_plan_error() -> None:
    with pytest.raises(PlanError, match="not valid"):
        recipe_to_yaml({"url": URL, "steps": [{"type": "no_such_step"}]})


# ---- the generated recipe actually runs ----


def test_a_generated_recipe_runs_with_values_filled_in(tmp_path: Path) -> None:
    recipe = parse_recipe_text(recipe_to_yaml(fields_to_recipe(URL, full_form(), "#send")))
    values = {"first_name": "Ann", "country": "IN", "plan": "pro", "cv": __file__}
    fake = FakePageDriver()
    log = run_recipe(expand_recipe(recipe, values), fake, tmp_path / "job")
    assert log.success is True, [s.error for s in log.steps]
    assert fake.fills == {"#fn": "Ann"}
    assert fake.selected == {"#c": "IN"}
    assert 'input[type=radio][name="plan"][value="pro"]' in fake.checked
    assert "#agree" in fake.checked
    assert fake.clicks == ["#send"]


def test_a_test_run_of_a_generated_recipe_stops_before_the_submit_button(tmp_path: Path) -> None:
    recipe = parse_recipe_text(
        recipe_to_yaml(fields_to_recipe(URL, [field("fill", "n", "#n")], "#send"))
    )
    fake = FakePageDriver()
    log = run_recipe(expand_recipe(recipe, {"n": "Ann"}), fake, tmp_path / "job", dry_run=True)
    assert log.success is True
    assert fake.clicks == []
    assert [s.name for s in log.steps][-1] == "dry_run_before_submit"
