"""The form scanner and generated recipes against a real browser and the local /register form."""

import socket
from pathlib import Path
from typing import Any

import pytest

from browserforge.engine.actions import do_choose, do_fill
from browserforge.engine.data_rows import expand_recipe
from browserforge.engine.driver import DriverError, PlaywrightDriver
from browserforge.engine.errors import classify, humanize
from browserforge.engine.form_scan import (
    FormPlan,
    fields_to_recipe,
    plan_form,
    recipe_to_yaml,
)
from browserforge.engine.recipe import Step, parse_recipe_text
from browserforge.engine.runner import run_recipe
from fixtures.live_server import submissions

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def scan(driver: PlaywrightDriver, site: str, path: str = "/register") -> FormPlan:
    return plan_form(driver.scan_form(site + path))


def generated_recipe(driver: PlaywrightDriver, site: str) -> Any:
    plan = scan(driver, site)
    text = recipe_to_yaml(
        fields_to_recipe(site + "/register", plan.fields, plan.submit_candidates[0].selector)
    )
    return parse_recipe_text(text)


def values(tmp_path: Path) -> dict[str, str]:
    cv = tmp_path / "cv.txt"
    cv.write_text("hello cv", encoding="utf-8")
    return {
        "first_name": "Ann",
        "e_mail": "ann@example.com",
        "date_of_birth": "1990-05-17",
        "country": "IN",
        "plan": "pro",
        "notes": "hello there",
        "cv": str(cv),
    }


# ---- scanning ----


def test_the_scan_finds_every_fillable_field_of_the_registration_form(
    driver: PlaywrightDriver, site: str
) -> None:
    plan = scan(driver, site)

    assert [(f.key, f.action, f.kind) for f in plan.fields] == [
        ("first_name", "fill", "text"),
        ("e_mail", "fill", "email"),
        ("date_of_birth", "fill", "date"),
        ("country", "select_option", "select"),
        ("plan", "choose", "radio"),
        ("notes", "fill", "textarea"),
        ("cv", "upload", "file"),
        ("", "check", "checkbox"),
    ]
    by_key = {f.key: f for f in plan.fields if f.key}
    assert by_key["first_name"].selector == "#first_name"
    assert by_key["first_name"].required is True
    assert by_key["country"].options == ("IN", "US")
    assert by_key["plan"].options == ("basic", "pro")
    assert by_key["plan"].selector == 'input[type=radio][name="plan"]'
    assert by_key["plan"].label == "Plan"  # from the fieldset's legend


def test_hidden_and_disabled_controls_are_left_out_and_unidentifiable_ones_explained(
    driver: PlaywrightDriver, site: str
) -> None:
    plan = scan(driver, site)
    selectors = [f.selector for f in plan.fields]
    assert not any("token" in s or "locked" in s for s in selectors)
    assert plan.skipped == ["'Nickname': it has no unique id or name to find it by."]
    assert plan.notes == []


def test_the_submit_button_is_offered(driver: PlaywrightDriver, site: str) -> None:
    plan = scan(driver, site)
    assert [(c.selector, c.label) for c in plan.submit_candidates] == [("#send", "Register")]


def test_a_captcha_and_an_iframe_are_flagged(driver: PlaywrightDriver, site: str) -> None:
    scanned = driver.scan_form(site + "/register-captcha")
    assert scanned["notes"] == ["iframe", "captcha"]
    plan = plan_form(scanned)
    assert len(plan.notes) == 2
    assert [f.selector for f in plan.fields] == ["#q"]


def test_a_form_built_by_javascript_after_load_is_found(
    driver: PlaywrightDriver, site: str
) -> None:
    plan = scan(driver, site, "/register-late")
    assert [(f.key, f.selector) for f in plan.fields] == [("late_name", "#late_name")]
    assert plan.submit_candidates[0].selector == "#late_send"


def test_an_unreachable_page_is_a_plain_error(driver: PlaywrightDriver) -> None:
    with socket.socket() as probe:  # a port that was free a moment ago, so nothing listens on it
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    with pytest.raises(DriverError) as info:
        driver.scan_form(f"http://127.0.0.1:{closed_port}/nothing")
    assert classify(info.value) == "navigation"
    assert "refused the connection" in humanize(classify(info.value), str(info.value))


# ---- running a generated recipe ----


def test_a_generated_recipe_really_submits_the_form_with_the_given_values(
    driver: PlaywrightDriver, site: str, tmp_path: Path
) -> None:
    before = len(submissions(site))
    recipe = expand_recipe(generated_recipe(driver, site), values(tmp_path))
    log = run_recipe(recipe, driver, tmp_path / "job")

    assert log.success is True, [(s.name, s.human_error) for s in log.steps if not s.success]
    received = submissions(site)
    assert len(received) == before + 1
    assert received[-1] == {
        "first_name": "Ann",
        "email": "ann@example.com",
        "dob": "1990-05-17",
        "country": "IN",
        "plan": "pro",
        "notes": "hello there",
        "cv": "[file cv.txt: hello cv]",
        "agree": "yes",
        "token": "abc123",
    }
    assert "Thanks, Ann" in driver.page.inner_text("#thanks")


def test_a_test_run_fills_the_form_but_never_submits_it(
    driver: PlaywrightDriver, site: str, tmp_path: Path
) -> None:
    before = len(submissions(site))
    recipe = expand_recipe(generated_recipe(driver, site), values(tmp_path))
    log = run_recipe(recipe, driver, tmp_path / "job", dry_run=True)

    assert log.success is True
    assert len(submissions(site)) == before  # nothing was sent
    assert driver.current_url().endswith("/register")  # still on the form
    assert driver.page.input_value("#first_name") == "Ann"
    assert driver.page.is_checked('input[name="plan"][value="pro"]')
    assert driver.page.is_checked("#agree")
    shots = sorted((tmp_path / "job" / "screenshots").glob("*dry_run_before_submit*.png"))
    assert len(shots) == 1
    assert shots[0].read_bytes().startswith(PNG_SIGNATURE)


def test_leaving_a_field_unticked_in_the_plan_leaves_it_out_of_the_recipe(
    driver: PlaywrightDriver, site: str, tmp_path: Path
) -> None:
    plan = scan(driver, site)
    kept = [f for f in plan.fields if f.key in ("first_name", "e_mail")]
    recipe = parse_recipe_text(
        recipe_to_yaml(
            fields_to_recipe(site + "/register", kept, plan.submit_candidates[0].selector)
        )
    )
    log = run_recipe(
        expand_recipe(recipe, {"first_name": "Bo", "e_mail": "bo@example.com"}),
        driver,
        tmp_path / "job",
    )
    assert log.success is True
    received = submissions(site)[-1]
    assert received["first_name"] == "Bo"
    assert received["email"] == "bo@example.com"
    assert "plan" not in received  # its radio button was never touched
    assert "agree" not in received


# ---- step-type audit in a real browser ----


def test_choose_picks_the_radio_button_with_that_value(driver: PlaywrightDriver, site: str) -> None:
    driver.goto(site + "/register")
    result = do_choose(
        driver, Step(type="choose", config={"selector": "input[name=plan]", "value": "pro"})
    )
    assert result.success is True
    assert driver.page.is_checked('input[name="plan"][value="pro"]')
    assert not driver.page.is_checked('input[name="plan"][value="basic"]')


def test_choose_with_an_unknown_value_fails_cleanly(driver: PlaywrightDriver, site: str) -> None:
    driver.goto(site + "/register")
    result = do_choose(
        driver, Step(type="choose", config={"selector": "input[name=plan]", "value": "gold"})
    )
    assert result.success is False
    assert result.error_kind == "timeout"
    assert "Step 'choose'" in (result.error or "")


def test_dates_and_text_areas_are_filled_by_the_existing_fill_step(
    driver: PlaywrightDriver, site: str
) -> None:
    driver.goto(site + "/register")
    date = Step(type="fill", config={"selector": "#dob", "value": "2000-01-31"})
    notes = Step(type="fill", config={"selector": "#notes", "value": "line 1\nline 2"})
    assert do_fill(driver, date).success is True
    assert do_fill(driver, notes).success is True
    assert driver.page.input_value("#dob") == "2000-01-31"
    assert driver.page.input_value("#notes") == "line 1\nline 2"
