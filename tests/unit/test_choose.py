"""Step-type audit (Task 3.4): the `choose` step for radio buttons, and the form-field types
that already work (dates and text areas through `fill`)."""

from typing import Any

import pytest

from browserforge.engine.actions import css_string, do_choose, do_fill
from browserforge.engine.driver import ElementNotFoundError
from browserforge.engine.recipe import STEP_TYPES, Step, parse_recipe_text
from browserforge.engine.runner import HANDLERS
from fixtures.fake_driver import FakePageDriver


def choose(**config: Any) -> Step:
    return Step(type="choose", name="pick", config=config)


# ---- css_string ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("female", '"female"'),
        ("", '""'),
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ("line\nbreak", '"line\\a break"'),
        ("tab\there", '"tab\\9 here"'),
        ("naïve – 日本", '"naïve – 日本"'),
        ("]; drop", '"]; drop"'),
    ],
)
def test_css_string_escapes_what_would_break_a_selector(text: str, expected: str) -> None:
    assert css_string(text) == expected


# ---- do_choose ----


def test_choose_checks_the_radio_with_that_value() -> None:
    fake = FakePageDriver()
    result = do_choose(fake, choose(selector="input[name=gender]", value="female"))
    assert result.success is True
    assert fake.checked == ['input[name=gender][value="female"]']
    assert fake.calls_to("check") == [('input[name=gender][value="female"]',)]


def test_a_number_from_yaml_is_used_as_text() -> None:
    fake = FakePageDriver()
    do_choose(fake, choose(selector="input[name=rating]", value=5))
    assert fake.checked == ['input[name=rating][value="5"]']


def test_awkward_values_cannot_break_out_of_the_selector() -> None:
    fake = FakePageDriver()
    do_choose(fake, choose(selector="input[name=q]", value='a"] , [x="b'))
    assert fake.checked == ['input[name=q][value="a\\"] , [x=\\"b"]']


def test_choose_needs_a_selector() -> None:
    result = do_choose(FakePageDriver(), choose(value="x"))
    assert result.success is False
    assert "missing required field 'selector'" in (result.error or "")


def test_choose_needs_a_value() -> None:
    fake = FakePageDriver()
    result = do_choose(fake, choose(selector="input[name=g]"))
    assert result.success is False
    assert "missing required field 'value'" in (result.error or "")
    assert fake.calls == []


def test_a_group_selector_with_a_comma_is_refused() -> None:
    fake = FakePageDriver()
    result = do_choose(fake, choose(selector="input[name=a], input[name=b]", value="x"))
    assert result.success is False
    assert "without commas" in (result.error or "")
    assert fake.calls == []


def test_a_missing_option_is_a_clear_not_found_failure() -> None:
    fake = FakePageDriver()
    fake.fail_on("check", ElementNotFoundError("No element matches"), times=1)
    result = do_choose(fake, choose(selector="input[name=g]", value="other"))
    assert result.success is False
    assert result.error_kind == "not_found"
    assert "Step 'pick'" in (result.error or "")


# ---- registration ----


def test_choose_is_a_known_step_type_with_a_handler() -> None:
    assert "choose" in STEP_TYPES
    assert "choose" in HANDLERS
    assert set(HANDLERS) == set(STEP_TYPES)


def test_a_recipe_with_a_choose_step_validates() -> None:
    recipe = parse_recipe_text(
        "url: https://x.io\n"
        "steps:\n"
        "  - type: choose\n"
        "    selector: input[name=gender]\n"
        '    value: "{{gender}}"\n'
    )
    assert recipe.steps[0].type == "choose"


# ---- audit: field types that `fill` already covers ----


def test_a_date_written_in_yaml_is_typed_as_iso_text() -> None:
    recipe = parse_recipe_text(
        "url: https://x.io\nsteps:\n  - type: fill\n    selector: '#dob'\n    value: 2000-01-31\n"
    )
    fake = FakePageDriver()
    assert do_fill(fake, recipe.steps[0]).success is True
    assert fake.fills == {"#dob": "2000-01-31"}


def test_a_multi_line_text_area_value_is_passed_through_whole() -> None:
    fake = FakePageDriver()
    step = Step(type="fill", config={"selector": "textarea#notes", "value": "line 1\nline 2"})
    assert do_fill(fake, step).success is True
    assert fake.fills == {"textarea#notes": "line 1\nline 2"}
