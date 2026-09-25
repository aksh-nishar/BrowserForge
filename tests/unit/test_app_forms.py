"""The form scanner and recipe generator endpoints, end to end with a fake browser."""

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from browserforge.app import create_app
from browserforge.engine.driver import DriverError, DriverTimeoutError
from browserforge.jobs import JobStore
from fixtures.fake_driver import FakePageDriver, raw_field

URL = "https://forms.example.com/register"


def registration_fields() -> list[Any]:
    return [
        raw_field(id="fn", label="First name", required=True),
        raw_field(id="em", type="email", label="E-mail"),
        raw_field(
            tag="select",
            type="select",
            id="country",
            label="Country",
            options=[{"value": "", "text": "-"}, {"value": "IN", "text": "India"}],
        ),
        raw_field(id="agree", type="checkbox", label="I agree to the terms"),
        raw_field(tag="button", type="submit", id="send", label="Register"),
        raw_field(label="Mystery"),
    ]


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.recipes = tmp_path / "recipes"
        self.jobs = tmp_path / "jobs"
        self.fake = FakePageDriver()
        self.fake.set_form(URL, registration_fields(), notes=["captcha"])
        self.client = TestClient(
            create_app(
                recipes_dir=self.recipes,
                jobs_dir=self.jobs,
                driver_factory=lambda: nullcontext(self.fake),
                store=JobStore(),
            )
        )

    def scan(self, url: str = URL) -> Any:
        return self.client.post("/scan", json={"url": url})

    def generate(self, **overrides: Any) -> Any:
        body: dict[str, Any] = {
            "name": "registration",
            "url": URL,
            "fields": [
                {"action": "fill", "selector": "#fn", "key": "first_name", "label": "First name"},
                {"action": "fill", "selector": "#em", "key": "email"},
                {"action": "select_option", "selector": "#country", "key": "country"},
                {"action": "check", "selector": "#agree", "label": "I agree"},
            ],
            "submit_selector": "#send",
        }
        body.update(overrides)
        return self.client.post("/recipes/generate", json=body)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ---- POST /scan ----


def test_scan_lists_the_fillable_fields_and_submit_candidates(env: Env) -> None:
    response = env.scan()
    assert response.status_code == 200
    body = response.json()
    assert body["url"] == URL
    assert [f["key"] for f in body["fields"]] == ["first_name", "e_mail", "country", ""]
    assert body["fields"][0]["selector"] == "#fn"
    assert body["fields"][2]["options"] == ["IN"]
    assert body["submit_candidates"] == [{"selector": "#send", "label": "Register"}]
    assert body["skipped"] == ["'Mystery': it has no unique id or name to find it by."]
    assert "CAPTCHA" in body["notes"][0]


def test_scan_opens_the_page_and_touches_nothing_else(env: Env) -> None:
    env.scan()
    assert [name for name, _ in env.fake.calls] == ["scan_form"]
    assert env.fake.calls[0][1] == (URL,)
    assert not env.recipes.exists()  # scanning saves no recipe


def test_scan_adds_https_to_a_bare_address(env: Env) -> None:
    env.fake.set_form("https://forms.example.com/bare", registration_fields())
    response = env.scan("forms.example.com/bare")
    assert response.status_code == 200
    assert response.json()["url"] == "https://forms.example.com/bare"


def test_scan_cleans_the_address(env: Env) -> None:
    assert env.scan("  " + URL + "  ").json()["url"] == URL


@pytest.mark.parametrize(
    "url", ["", "not a url", "file:///etc/passwd", "javascript:alert(1)", "ftp://x.io"]
)
def test_scan_refuses_addresses_that_are_not_web_pages(env: Env, url: str) -> None:
    response = env.scan(url)
    assert response.status_code in (400, 422)
    assert env.fake.calls == []


def test_scan_reports_a_page_that_cannot_be_opened_in_plain_words(env: Env) -> None:
    env.fake.fail_on("scan_form", DriverError("net::ERR_NAME_NOT_RESOLVED"))
    response = env.scan()
    assert response.status_code == 502
    assert "website address could not be found" in response.json()["detail"]


def test_scan_reports_a_slow_page(env: Env) -> None:
    env.fake.fail_on(
        "scan_form", DriverTimeoutError(f"Timed out after 30000ms waiting for {URL} to load")
    )
    response = env.scan()
    assert response.status_code == 502
    assert "Could not open the page" in response.json()["detail"]


def test_scan_hides_internal_errors(tmp_path: Path) -> None:
    def broken() -> Any:
        raise RuntimeError("secret internal detail")

    client = TestClient(
        create_app(recipes_dir=tmp_path / "r", jobs_dir=tmp_path / "j", driver_factory=broken)
    )
    response = client.post("/scan", json={"url": URL})
    assert response.status_code == 500
    assert "secret internal detail" not in response.text


def test_scan_of_an_empty_page_is_an_empty_plan(env: Env) -> None:
    response = env.scan("https://forms.example.com/blank")
    assert response.status_code == 200
    assert response.json()["fields"] == []


# ---- POST /recipes/generate ----


def test_generate_saves_a_recipe_that_needs_values_and_can_be_test_run(env: Env) -> None:
    response = env.generate()
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "registration"
    assert body["saved"] is True
    assert body["replaced"] is False
    assert body["placeholders"] == ["country", "email", "first_name"]
    assert body["has_submit"] is True
    assert body["steps"] == 1 + 4 + 3  # start page, four fields, screenshot + submit + screenshot
    text = (env.recipes / "registration.yaml").read_text(encoding="utf-8")
    assert "{{first_name}}" in text
    assert "submit: true" in text


def test_the_new_recipe_shows_up_in_the_list_with_its_placeholders(env: Env) -> None:
    env.generate()
    (entry,) = env.client.get("/recipes").json()["recipes"]
    assert entry["valid"] is True
    assert entry["placeholders"] == ["country", "email", "first_name"]
    assert entry["has_submit"] is True
    assert entry["url"] == URL


def test_generating_again_replaces_the_recipe(env: Env) -> None:
    env.generate()
    assert env.generate().json()["replaced"] is True


def test_the_recipe_name_is_made_safe(env: Env) -> None:
    body = env.generate(name="My Form!.yaml").json()
    assert body["name"] == "My_Form"
    assert (env.recipes / "My_Form.yaml").is_file()


@pytest.mark.parametrize("name", ["!!!", "...", "___"])
def test_a_name_with_no_usable_characters_is_a_400(env: Env, name: str) -> None:
    assert env.generate(name=name).status_code == 400


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"fields": []}, "at least one field"),
        ({"url": "file:///etc/passwd"}, "http"),
        ({"submit_selector": "{{x}}"}, "reserved"),
        (
            {"fields": [{"action": "fill", "selector": "#a", "key": "1bad"}]},
            "needs a name",
        ),
        (
            {"fields": [{"action": "explode", "selector": "#a", "key": "a"}]},
            "unknown action",
        ),
        (
            {
                "fields": [
                    {"action": "fill", "selector": "#a", "key": "same"},
                    {"action": "fill", "selector": "#b", "key": "same"},
                ]
            },
            "two fields",
        ),
    ],
)
def test_bad_choices_are_a_400_and_nothing_is_saved(
    env: Env, override: dict[str, Any], message: str
) -> None:
    response = env.generate(**override)
    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert not env.recipes.exists()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": "x", "url": URL},
        {"name": "", "url": URL, "fields": []},
        {"name": "x", "url": URL, "fields": [{"action": "fill"}]},
        {"name": "x", "url": URL, "fields": [{"action": "fill", "selector": "a" * 501}]},
    ],
)
def test_malformed_bodies_are_422(env: Env, body: dict[str, Any]) -> None:
    assert env.client.post("/recipes/generate", json=body).status_code == 422


def test_selectors_and_names_from_a_hostile_page_cannot_smuggle_yaml(env: Env) -> None:
    hostile = 'input[name="x"]\nsteps:\n  - type: click'
    response = env.generate(fields=[{"action": "fill", "selector": hostile, "key": "x"}])
    assert response.status_code == 200
    recipe = env.client.get("/recipes").json()["recipes"][0]
    assert recipe["valid"] is True
    assert recipe["steps"] == 1 + 1 + 3  # the injected text is just a selector, not extra steps


# ---- the whole flow ----


def test_scan_generate_then_test_run_then_real_run(env: Env) -> None:
    plan = env.scan().json()
    fields = [
        {"action": f["action"], "selector": f["selector"], "key": f["key"], "label": f["label"]}
        for f in plan["fields"]
    ]
    submit = plan["submit_candidates"][0]["selector"]
    assert env.generate(fields=fields, submit_selector=submit).status_code == 200

    values = json.dumps({"first_name": "Ann", "e_mail": "a@x.io", "country": "IN"})
    # a test run fills everything but never presses the submit button
    test_run = env.client.post(
        "/jobs/bulk",
        data={"recipe_name": "registration", "values": values, "dry_run": "true"},
    )
    assert test_run.status_code == 202
    assert env.fake.fills == {"#fn": "Ann", "#em": "a@x.io"}
    assert env.fake.selected == {"#country": "IN"}
    assert env.fake.clicks == []
    status = env.client.get(f"/status/{test_run.json()['job_id']}").json()
    assert status["status"] == "complete"
    assert status["dry_run"] is True

    # the real run submits
    real = env.client.post("/jobs/bulk", data={"recipe_name": "registration", "values": values})
    assert real.status_code == 202
    assert env.fake.clicks == ["#send"]
    assert env.client.get(f"/status/{real.json()['job_id']}").json()["status"] == "complete"


def test_a_generated_recipe_can_run_once_per_row_of_a_sheet(env: Env) -> None:
    env.generate()
    sheet = b"first_name,email,country\nAnn,a@x.io,IN\nBob,b@x.io,IN\n"
    response = env.client.post(
        "/jobs/bulk",
        data={"recipe_name": "registration"},
        files={"file": ("people.csv", sheet, "text/csv")},
    )
    assert response.status_code == 202
    assert response.json()["rows"] == 2
    assert env.fake.calls_to("fill") == [
        ("#fn", "Ann"),
        ("#em", "a@x.io"),
        ("#fn", "Bob"),
        ("#em", "b@x.io"),
    ]
    assert env.fake.clicks.count("#send") == 2
