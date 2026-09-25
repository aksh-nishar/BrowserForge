"""Bulk and data endpoints, placeholders in single runs, dry runs, and per-row results."""

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from browserforge.app import create_app
from browserforge.engine.driver import DriverError
from browserforge.jobs import JobStore
from fixtures.fake_driver import FakePageDriver

FORM_RECIPE = (
    "url: https://x.io\n"
    "steps:\n"
    "  - type: fill\n"
    "    name: email\n"
    '    selector: "#e"\n'
    '    value: "{{email}}"\n'
    "  - type: fill\n"
    "    name: first_name\n"
    '    selector: "#n"\n'
    '    value: "{{first_name}}"\n'
    "  - type: screenshot\n"
    "    name: filled\n"
)
CSV = b"E-mail,First Name\na@x.io,Ann\nb@x.io,Bob\n"
PLAIN = "url: https://x.io\nsteps:\n  - type: click\n    selector: '#go'\n"
SUBMIT = (
    "url: https://x.io\n"
    "steps:\n"
    "  - type: fill\n"
    '    selector: "#n"\n'
    "    value: Ann\n"
    "  - type: click\n"
    "    name: send\n"
    '    selector: "#send"\n'
    "    submit: true\n"
)
SECRET = (
    "url: https://x.io\n"
    "steps:\n"
    "  - type: fill\n"
    '    selector: "#p"\n'
    '    value: "{{env:PORTAL_PW}}"\n'
)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.recipes = tmp_path / "recipes"
        self.jobs = tmp_path / "jobs"
        self.fake = FakePageDriver()
        self.store = JobStore()
        self.recipes.mkdir()
        self.client = TestClient(
            create_app(
                recipes_dir=self.recipes,
                jobs_dir=self.jobs,
                driver_factory=lambda: nullcontext(self.fake),
                store=self.store,
            )
        )

    def add_recipe(self, name: str, text: str) -> None:
        (self.recipes / f"{name}.yaml").write_text(text, encoding="utf-8")

    def bulk(
        self,
        name: str = "form",
        data: bytes | None = CSV,
        filename: str = "people.csv",
        **fields: str,
    ) -> Any:
        files = {"file": (filename, data, "text/csv")} if data is not None else None
        return self.client.post("/jobs/bulk", data={"recipe_name": name, **fields}, files=files)

    def bulk_job_id(self, **kwargs: Any) -> str:
        job_id: str = self.bulk(**kwargs).json()["job_id"]
        return job_id


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    e.add_recipe("form", FORM_RECIPE)
    e.add_recipe("plain", PLAIN)
    e.add_recipe("submit", SUBMIT)
    return e


# ---- recipe listing ----


def test_the_recipe_list_says_which_recipes_need_data(env: Env) -> None:
    recipes = {r["name"]: r for r in env.client.get("/recipes").json()["recipes"]}
    assert recipes["form"]["placeholders"] == ["email", "first_name"]
    assert recipes["plain"]["placeholders"] == []
    assert recipes["submit"]["has_submit"] is True
    assert recipes["plain"]["has_submit"] is False


def test_an_invalid_recipe_in_the_list_has_no_placeholders(env: Env) -> None:
    env.add_recipe("broken", "steps: []\n")
    broken = next(r for r in env.client.get("/recipes").json()["recipes"] if r["name"] == "broken")
    assert broken["valid"] is False
    assert broken["placeholders"] == []


# ---- POST /jobs/bulk ----


def test_a_sheet_runs_the_recipe_once_per_row(env: Env) -> None:
    response = env.bulk()
    assert response.status_code == 202
    body = response.json()
    assert body["rows"] == 2
    assert len(body["job_id"]) == 12

    assert env.fake.calls_to("fill") == [
        ("#e", "a@x.io"),
        ("#n", "Ann"),
        ("#e", "b@x.io"),
        ("#n", "Bob"),
    ]
    status = env.client.get(f"/status/{body['job_id']}").json()
    assert status["status"] == "complete"
    assert status["bulk"]["rows_done"] == 2
    assert status["bulk"]["rows_failed"] == 0
    assert status["summary"]["rows_total"] == 2


def test_an_explicit_column_mapping_is_used(env: Env) -> None:
    mapping = json.dumps({"email": "second", "first_name": "first"})
    response = env.bulk(data=b"first,second\nAnn,a@x.io\n", mapping=mapping)
    assert response.status_code == 202
    assert ("#e", "a@x.io") in env.fake.calls_to("fill")


def test_typed_values_run_the_recipe_once(env: Env) -> None:
    values = json.dumps({"email": "z@x.io", "first_name": "Zed"})
    response = env.bulk(data=None, values=values)
    assert response.status_code == 202
    assert response.json()["rows"] == 1
    assert env.fake.fills == {"#e": "z@x.io", "#n": "Zed"}


def test_a_failing_row_ends_the_job_failed_and_lists_the_row(env: Env) -> None:
    env.fake.set_missing("#e")
    job_id = env.bulk_job_id()
    status = env.client.get(f"/status/{job_id}").json()
    assert status["status"] == "failed"
    assert status["bulk"]["rows_failed"] == 2
    assert status["bulk"]["failed_rows"][0]["row"] == 1


def test_a_dry_run_bulk_does_not_submit(env: Env) -> None:
    env.add_recipe("submit_form", SUBMIT.replace("value: Ann", 'value: "{{name}}"'))
    response = env.bulk("submit_form", data=b"name\nAnn\nBob\n", dry_run="true")
    assert response.status_code == 202
    assert env.fake.clicks == []
    job_id = response.json()["job_id"]
    assert env.client.get(f"/status/{job_id}").json()["dry_run"] is True


def test_a_dry_run_needs_a_submit_step(env: Env) -> None:
    response = env.bulk(dry_run="true")
    assert response.status_code == 400
    assert "submit: true" in response.json()["detail"]
    assert env.fake.calls == []


@pytest.mark.parametrize("problem", ["blank cell", "missing column", "no rows"])
def test_bad_data_is_a_400_and_no_job_is_created(env: Env, problem: str) -> None:
    data = {
        "blank cell": b"E-mail,First Name\na@x.io,\n",
        "missing column": b"E-mail\na@x.io\n",
        "no rows": b"E-mail,First Name\n",
    }[problem]
    response = env.bulk(data=data)
    assert response.status_code == 400
    assert not env.jobs.exists()
    assert env.fake.calls == []


def test_the_400_message_names_the_row_and_column(env: Env) -> None:
    response = env.bulk(data=b"E-mail,First Name\na@x.io,\n")
    assert "Row 1: no value for 'first_name'" in response.json()["detail"]


def test_no_data_is_a_400(env: Env) -> None:
    assert env.bulk(data=None).status_code == 400


def test_a_recipe_without_placeholders_refuses_a_sheet(env: Env) -> None:
    response = env.bulk("plain", data=b"a\n1\n")
    assert response.status_code == 400
    assert "placeholders" in response.json()["detail"]


def test_wrong_file_type_is_a_400(env: Env) -> None:
    response = env.bulk(data=b"x", filename="people.txt")
    assert response.status_code == 400
    assert ".csv or .xlsx" in response.json()["detail"]


def test_unknown_recipe_is_a_404(env: Env) -> None:
    assert env.bulk("nope").status_code == 404


def test_bad_mapping_json_is_a_400(env: Env) -> None:
    assert env.bulk(mapping="not json").status_code == 400


def test_an_oversized_data_file_is_a_413(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("browserforge.app.MAX_DATA_BYTES", 10)
    assert env.bulk().status_code == 413


def test_an_unusable_typed_values_body_is_a_400(env: Env) -> None:
    assert env.bulk(data=None, values="not json").status_code == 400


# ---- POST /data/preview ----


def test_preview_returns_columns_mapping_and_problems(env: Env) -> None:
    response = env.client.post(
        "/data/preview",
        data={"recipe_name": "form"},
        files={"file": ("p.csv", CSV, "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == 2
    assert body["mapping"] == {"email": "E-mail", "first_name": "First Name"}
    assert body["problems"] == []
    assert env.fake.calls == []  # a preview never opens a browser


def test_preview_with_a_chosen_mapping(env: Env) -> None:
    response = env.client.post(
        "/data/preview",
        data={"recipe_name": "form", "mapping": json.dumps({"email": "a", "first_name": "b"})},
        files={"file": ("p.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert response.json()["mapping"] == {"email": "a", "first_name": "b"}


def test_preview_rejects_a_bad_file_and_an_unknown_recipe(env: Env) -> None:
    bad = env.client.post(
        "/data/preview",
        data={"recipe_name": "form"},
        files={"file": ("p.txt", b"x", "text/plain")},
    )
    assert bad.status_code == 400
    missing = env.client.post(
        "/data/preview",
        data={"recipe_name": "nope"},
        files={"file": ("p.csv", CSV, "text/csv")},
    )
    assert missing.status_code == 404


# ---- POST /jobs (single run) with placeholders, env values and dry run ----


def test_a_recipe_that_needs_data_cannot_be_run_without_it(env: Env) -> None:
    response = env.client.post("/jobs", json={"recipe_name": "form"})
    assert response.status_code == 400
    assert "{{email}}" in response.json()["detail"]
    assert not env.jobs.exists()


def test_environment_values_are_filled_in_at_run_time(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.add_recipe("secret", SECRET)
    monkeypatch.setenv("PORTAL_PW", "letmein99")
    assert env.client.post("/jobs", json={"recipe_name": "secret"}).status_code == 202
    assert env.fake.fills == {"#p": "letmein99"}


def test_a_missing_environment_value_is_a_400_before_any_browser_starts(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.add_recipe("secret", SECRET)
    monkeypatch.delenv("PORTAL_PW", raising=False)
    response = env.client.post("/jobs", json={"recipe_name": "secret"})
    assert response.status_code == 400
    assert "PORTAL_PW" in response.json()["detail"]
    assert env.fake.calls == []


def test_an_environment_value_never_appears_in_the_saved_run_log(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.add_recipe("secret", SECRET)
    monkeypatch.setenv("PORTAL_PW", "letmein99")
    env.fake.fail_on("fill", DriverError("cannot type letmein99"), times=None)
    job_id = env.client.post("/jobs", json={"recipe_name": "secret"}).json()["job_id"]
    text = (env.jobs / job_id / "run_log.json").read_text(encoding="utf-8")
    assert "letmein99" not in text
    assert "***" in text
    assert "letmein99" not in json.dumps(env.client.get(f"/status/{job_id}").json())


def test_a_single_dry_run_stops_before_submit(env: Env) -> None:
    response = env.client.post("/jobs", json={"recipe_name": "submit", "dry_run": True})
    assert response.status_code == 202
    assert env.fake.clicks == []
    job_id = response.json()["job_id"]
    status = env.client.get(f"/status/{job_id}").json()
    assert status["status"] == "complete"
    assert status["dry_run"] is True


def test_a_single_dry_run_of_a_recipe_without_submit_is_a_400(env: Env) -> None:
    response = env.client.post("/jobs", json={"recipe_name": "plain", "dry_run": True})
    assert response.status_code == 400
    assert env.fake.calls == []


def test_a_normal_run_of_a_submit_recipe_still_submits(env: Env) -> None:
    env.client.post("/jobs", json={"recipe_name": "submit"})
    assert env.fake.clicks == ["#send"]


# ---- results of a bulk run ----


def test_results_list_each_rows_files_under_the_row_folder(env: Env) -> None:
    job_id = env.bulk_job_id()
    results = env.client.get(f"/jobs/{job_id}/results").json()
    names = [shot["name"] for shot in results["screenshots"]]
    assert len(names) == 2
    assert names[0].startswith("row_0001/")
    assert names[1].startswith("row_0002/")
    assert results["excel"]["name"] == "output.xlsx"
    assert results["run_log"]["name"] == "run_log.json"


def test_a_row_file_can_be_downloaded(env: Env) -> None:
    job_id = env.bulk_job_id()
    results = env.client.get(f"/jobs/{job_id}/results").json()
    response = env.client.get(results["screenshots"][0]["url"])
    assert response.status_code == 200
    assert response.content == b"fake-png"


def test_failure_screenshots_of_rows_are_flagged(env: Env) -> None:
    env.fake.set_missing("#e")
    job_id = env.bulk_job_id()
    results = env.client.get(f"/jobs/{job_id}/results").json()
    failures = [s for s in results["screenshots"] if s["failure"]]
    assert len(failures) == 2
