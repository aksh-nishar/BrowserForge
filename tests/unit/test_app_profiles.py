"""Profile endpoints: save, list, apply to placeholders, delete."""

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from browserforge.app import create_app
from browserforge.jobs import JobStore
from fixtures.fake_driver import FakePageDriver

FORM = (
    "url: https://x.io\n"
    "steps:\n"
    "  - type: fill\n"
    '    selector: "#e"\n'
    '    value: "{{email}}"\n'
    "  - type: fill\n"
    '    selector: "#n"\n'
    '    value: "{{first_name}}"\n'
)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.fake = FakePageDriver()
        self.client = self.new_client()

    def new_client(self) -> TestClient:
        """A fresh app on the same folders, like a restarted server."""
        return TestClient(
            create_app(
                recipes_dir=self.tmp / "recipes",
                jobs_dir=self.tmp / "jobs",
                data_dir=self.tmp / "data",
                driver_factory=lambda: nullcontext(self.fake),
                store=JobStore(),
            )
        )

    def save(self, name: str = "Ann", **values: Any) -> Any:
        body = values or {"first_name": "Ann", "email": "a@x.io", "city": "Pune"}
        return self.client.put(f"/profiles/{name}", json={"values": body})


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_no_profiles_at_first(env: Env) -> None:
    assert env.client.get("/profiles").json() == {"profiles": []}


def test_saving_a_profile_reports_what_was_kept(env: Env) -> None:
    response = env.save()
    assert response.status_code == 200
    assert response.json() == {
        "name": "Ann",
        "saved": ["city", "email", "first_name"],
        "dropped": [],
    }


def test_secret_looking_fields_are_dropped_and_reported(env: Env) -> None:
    response = env.save("Ann", email="a@x.io", password="hunter22", aadhaar="1234")
    body = response.json()
    assert body["saved"] == ["email"]
    assert sorted(body["dropped"]) == ["aadhaar", "password"]
    assert "hunter22" not in (env.tmp / "data" / "profiles.json").read_text(encoding="utf-8")


def test_the_list_shows_field_names_but_never_values(env: Env) -> None:
    env.save()
    response = env.client.get("/profiles")
    assert response.json() == {
        "profiles": [{"name": "Ann", "fields": ["city", "email", "first_name"]}]
    }
    assert "a@x.io" not in response.text


def test_profiles_survive_a_restart(env: Env) -> None:
    env.save()
    restarted = env.new_client()
    assert [p["name"] for p in restarted.get("/profiles").json()["profiles"]] == ["Ann"]


def test_saving_the_same_name_replaces_it(env: Env) -> None:
    env.save("Ann", email="old@x.io", city="Pune")
    env.save("Ann", email="new@x.io")
    (entry,) = env.client.get("/profiles").json()["profiles"]
    assert entry["fields"] == ["email"]


def test_a_name_with_spaces_works_through_the_url(env: Env) -> None:
    assert (
        env.client.put("/profiles/Home%20address", json={"values": {"city": "Pune"}}).status_code
        == 200
    )
    assert env.client.get("/profiles").json()["profiles"][0]["name"] == "Home address"
    assert env.client.delete("/profiles/Home%20address").status_code == 200


@pytest.mark.parametrize("name", ["-bad", ".hidden", "a%2Fb", "x" * 41])
def test_bad_names_are_a_400(env: Env, name: str) -> None:
    response = env.client.put(f"/profiles/{name}", json={"values": {"x": "1"}})
    assert response.status_code in (400, 404)
    assert env.client.get("/profiles").json() == {"profiles": []}


def test_saving_only_secrets_is_a_400(env: Env) -> None:
    response = env.save("Ann", password="hunter22")
    assert response.status_code == 400
    assert "nothing to save" in response.json()["detail"]


def test_an_oversized_value_is_a_400(env: Env) -> None:
    response = env.save("Ann", notes="x" * 5000)
    assert response.status_code == 400
    assert "too long" in response.json()["detail"]


@pytest.mark.parametrize("body", [{}, {"values": "text"}, {"values": [1]}])
def test_malformed_bodies_are_422(env: Env, body: dict[str, Any]) -> None:
    assert env.client.put("/profiles/Ann", json=body).status_code == 422


def test_delete_removes_and_unknown_is_404(env: Env) -> None:
    env.save()
    assert env.client.delete("/profiles/Ann").json() == {"deleted": True}
    assert env.client.get("/profiles").json() == {"profiles": []}
    response = env.client.delete("/profiles/Ann")
    assert response.status_code == 404
    assert "Ann" in response.json()["detail"]


def test_apply_fills_matching_placeholders_loosely_and_lists_the_rest(env: Env) -> None:
    env.save("Ann", **{"First Name": "Ann", "E-mail": "a@x.io"})
    response = env.client.post(
        "/profiles/Ann/apply", json={"placeholders": ["email", "first_name", "phone"]}
    )
    assert response.status_code == 200
    assert response.json() == {
        "values": {"email": "a@x.io", "first_name": "Ann"},
        "missing": ["phone"],
    }


def test_apply_never_fills_a_secret_placeholder(env: Env) -> None:
    env.save("Ann", email="a@x.io")
    body = env.client.post(
        "/profiles/Ann/apply", json={"placeholders": ["email", "password"]}
    ).json()
    assert body["values"] == {"email": "a@x.io"}
    assert body["missing"] == ["password"]


def test_apply_of_an_unknown_profile_is_404(env: Env) -> None:
    assert (
        env.client.post("/profiles/Nobody/apply", json={"placeholders": ["x"]}).status_code == 404
    )


def test_apply_needs_a_placeholder_list(env: Env) -> None:
    env.save()
    assert env.client.post("/profiles/Ann/apply", json={}).status_code == 422


def test_profile_values_can_drive_a_run_of_a_recipe(env: Env) -> None:
    """The dashboard's flow: apply a profile to a recipe's placeholders, then run with them."""
    (env.tmp / "recipes").mkdir()
    (env.tmp / "recipes" / "form.yaml").write_text(FORM, encoding="utf-8")
    env.save("Ann", first_name="Ann", email="a@x.io")

    recipe = env.client.get("/recipes").json()["recipes"][0]
    applied = env.client.post(
        "/profiles/Ann/apply", json={"placeholders": recipe["placeholders"]}
    ).json()
    assert applied["missing"] == []

    run = env.client.post(
        "/jobs/bulk", data={"recipe_name": "form", "values": json.dumps(applied["values"])}
    )
    assert run.status_code == 202
    assert env.fake.fills == {"#e": "a@x.io", "#n": "Ann"}
