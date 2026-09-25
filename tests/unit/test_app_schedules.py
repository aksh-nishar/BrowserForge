"""Schedule endpoints: create, list, pause/resume, delete, persistence and startup."""

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from browserforge.app import create_app
from browserforge.jobs import JobStore
from browserforge.schedule import Schedule
from browserforge.schedule_store import ScheduleStore
from fixtures.fake_driver import FakePageDriver

PLAIN = "url: https://x.io\nsteps:\n  - type: click\n    selector: '#go'\n"
FORM = 'url: https://x.io\nsteps:\n  - type: fill\n    selector: "#e"\n    value: "{{email}}"\n'
SECRET = (
    'url: https://x.io\nsteps:\n  - type: fill\n    selector: "#p"\n    value: "{{env:SCHED_PW}}"\n'
)
CSV = b"email\na@x.io\nb@x.io\n"


class Env:
    def __init__(self, tmp_path: Path, start_scheduler: bool = False) -> None:
        self.tmp = tmp_path
        self.recipes = tmp_path / "recipes"
        self.jobs = tmp_path / "jobs"
        self.data = tmp_path / "data"
        self.recipes.mkdir(exist_ok=True)
        self.fake = FakePageDriver()
        self.store = JobStore()
        self.app = create_app(
            recipes_dir=self.recipes,
            jobs_dir=self.jobs,
            data_dir=self.data,
            driver_factory=lambda: nullcontext(self.fake),
            store=self.store,
            start_scheduler=start_scheduler,
        )
        self.client = TestClient(self.app)

    def recipe(self, name: str, text: str) -> None:
        (self.recipes / f"{name}.yaml").write_text(text, encoding="utf-8")

    def create(
        self,
        recipe: str = "plain",
        when: str = "daily 08:00",
        data: bytes | None = None,
        **fields: str,
    ) -> Any:
        files = {"file": ("people.csv", data, "text/csv")} if data is not None else None
        return self.client.post(
            "/schedules", data={"recipe_name": recipe, "when": when, **fields}, files=files
        )

    @property
    def service(self) -> Any:
        return self.app.state.scheduler

    def fire(self, schedule_id: str) -> Any:
        return self.app.state.schedule_runner.fire(schedule_id)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    e.recipe("plain", PLAIN)
    e.recipe("form", FORM)
    return e


# ---- create ----


def test_creating_a_schedule_returns_it_with_the_next_run(env: Env) -> None:
    response = env.create(timezone="UTC")
    assert response.status_code == 201
    body = response.json()
    assert body["recipe_name"] == "plain"
    assert body["when"] == "daily 08:00"
    assert body["timezone"] == "UTC"
    assert body["enabled"] is True
    assert body["has_data"] is False
    assert body["next_run"] is not None
    assert body["next_run"].endswith("+00:00")
    assert body["last_run_at"] is None
    assert body["last_status"] is None
    assert len(body["id"]) == 8


def test_the_schedule_is_saved_to_disk_and_given_to_the_scheduler(env: Env) -> None:
    schedule_id = env.create().json()["id"]
    saved = json.loads((env.data / "schedules.json").read_text(encoding="utf-8"))
    assert saved["schedules"][0]["id"] == schedule_id
    assert saved["schedules"][0]["cron"] == "0 8 * * *"
    assert env.service.is_scheduled(schedule_id)


def test_the_typed_time_is_kept_as_written_but_tidied(env: Env) -> None:
    assert env.create(when="  weekly   MON 09:30 ").json()["when"] == "weekly MON 09:30"


def test_a_recipe_that_needs_data_takes_a_sheet_and_keeps_it(env: Env) -> None:
    response = env.create("form", data=CSV)
    assert response.status_code == 201
    assert response.json()["has_data"] is True
    schedule_id = response.json()["id"]
    assert (env.data / "schedule_data" / schedule_id / "data.csv").read_bytes() == CSV


def test_a_column_mapping_is_saved_with_the_schedule(env: Env) -> None:
    mapping = json.dumps({"email": "mail"})
    response = env.create("form", data=b"mail\na@x.io\n", mapping=mapping)
    assert response.status_code == 201
    saved = json.loads((env.data / "schedules.json").read_text(encoding="utf-8"))
    assert saved["schedules"][0]["mapping"] == {"email": "mail"}


# ---- create: refusals ----


def test_a_recipe_that_needs_data_is_refused_without_a_sheet(env: Env) -> None:
    response = env.create("form")
    assert response.status_code == 400
    assert "{{email}}" in response.json()["detail"]
    assert not (env.data / "schedules.json").exists()


def test_a_sheet_for_a_recipe_without_placeholders_is_refused(env: Env) -> None:
    assert env.create("plain", data=CSV).status_code == 400


def test_a_sheet_with_a_secret_looking_column_is_refused(env: Env) -> None:
    response = env.create("form", data=b"email,password\na@x.io,hunter22\n")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "password" in detail
    assert "{env:NAME}" in detail
    assert not (env.data / "schedule_data").exists()


def test_a_sheet_with_problems_is_refused_now_not_at_run_time(env: Env) -> None:
    response = env.create("form", data=b"email\na@x.io\n\n,\n" + b"\n")
    assert response.status_code == 201  # blank lines are ignored...
    bad = env.create("form", data=b"other\nx\n")
    assert bad.status_code == 400
    assert "no column for 'email'" in bad.json()["detail"]


@pytest.mark.parametrize("when", ["sometimes", "daily 25:00", "every 3 minutes", "30 8 * * 1"])
def test_a_bad_schedule_text_is_a_400_with_a_reason(env: Env, when: str) -> None:
    response = env.create(when=when)
    assert response.status_code == 400
    assert response.json()["detail"]


def test_a_bad_timezone_is_a_400(env: Env) -> None:
    response = env.create(timezone="Mars/Olympus")
    assert response.status_code == 400
    assert "timezone" in response.json()["detail"]


def test_an_unknown_recipe_is_a_404(env: Env) -> None:
    assert env.create("nope").status_code == 404


def test_a_missing_environment_value_is_refused_at_creation(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.recipe("secret", SECRET)
    monkeypatch.delenv("SCHED_PW", raising=False)
    response = env.create("secret")
    assert response.status_code == 400
    assert "SCHED_PW" in response.json()["detail"]


def test_an_environment_recipe_can_be_scheduled_when_the_value_is_set(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.recipe("secret", SECRET)
    monkeypatch.setenv("SCHED_PW", "letmein99")
    assert env.create("secret").status_code == 201
    saved = (env.data / "schedules.json").read_text(encoding="utf-8")
    assert "letmein99" not in saved  # the value is never written down


def test_an_oversized_sheet_is_a_413(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("browserforge.app.MAX_DATA_BYTES", 5)
    assert env.create("form", data=CSV).status_code == 413


@pytest.mark.parametrize("fields", [{"recipe_name": "plain"}, {"when": "hourly"}, {}])
def test_missing_form_fields_are_422(env: Env, fields: dict[str, str]) -> None:
    assert env.client.post("/schedules", data=fields).status_code == 422


# ---- list ----


def test_listing_shows_every_schedule_and_whether_the_scheduler_runs(env: Env) -> None:
    env.create(when="hourly")
    env.create(when="weekly mon 09:00")
    body = env.client.get("/schedules").json()
    assert body["running"] is False  # tests do not start the background thread
    assert [s["when"] for s in body["schedules"]] == ["hourly", "weekly mon 09:00"]


def test_an_empty_list_is_fine(env: Env) -> None:
    assert env.client.get("/schedules").json() == {"running": False, "schedules": []}


def test_after_a_firing_the_list_shows_the_last_run_and_its_status(env: Env) -> None:
    schedule_id = env.create().json()["id"]
    assert env.fire(schedule_id).outcome == "started"

    (entry,) = env.client.get("/schedules").json()["schedules"]
    assert entry["last_status"] == "complete"
    assert entry["last_run_at"] is not None
    assert entry["last_error"] is None
    assert env.fake.clicks == ["#go"]
    assert env.store.get(entry["last_job_id"]).status == "complete"


def test_a_scheduled_sheet_run_is_a_bulk_job(env: Env) -> None:
    schedule_id = env.create("form", data=CSV).json()["id"]
    env.fire(schedule_id)
    (entry,) = env.client.get("/schedules").json()["schedules"]
    status = env.client.get(f"/status/{entry['last_job_id']}").json()
    assert status["bulk"]["rows_done"] == 2
    assert env.fake.calls_to("fill") == [("#e", "a@x.io"), ("#e", "b@x.io")]


def test_a_firing_that_cannot_start_shows_why_in_the_list(env: Env) -> None:
    schedule_id = env.create().json()["id"]
    (env.recipes / "plain.yaml").unlink()
    assert env.fire(schedule_id).outcome == "error"
    (entry,) = env.client.get("/schedules").json()["schedules"]
    assert "no longer exists" in entry["last_error"]
    assert entry["last_status"] is None


# ---- pause / resume / delete ----


def test_pausing_stops_the_schedule_and_clears_its_next_run(env: Env) -> None:
    schedule_id = env.create().json()["id"]
    response = env.client.post(f"/schedules/{schedule_id}/pause")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["next_run"] is None
    assert not env.service.is_scheduled(schedule_id)
    assert env.fire(schedule_id).outcome == "paused"
    assert env.fake.calls == []


def test_resuming_schedules_it_again(env: Env) -> None:
    schedule_id = env.create().json()["id"]
    env.client.post(f"/schedules/{schedule_id}/pause")
    response = env.client.post(f"/schedules/{schedule_id}/resume")
    assert response.json()["enabled"] is True
    assert response.json()["next_run"] is not None
    assert env.service.is_scheduled(schedule_id)


def test_pause_state_survives_a_restart(env: Env) -> None:
    schedule_id = env.create().json()["id"]
    env.client.post(f"/schedules/{schedule_id}/pause")
    fresh = ScheduleStore(env.data).get(schedule_id)
    assert fresh is not None
    assert fresh.enabled is False


def test_deleting_removes_the_schedule_its_sheet_and_its_job(env: Env) -> None:
    schedule_id = env.create("form", data=CSV).json()["id"]
    response = env.client.delete(f"/schedules/{schedule_id}")
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert env.client.get("/schedules").json()["schedules"] == []
    assert not env.service.is_scheduled(schedule_id)
    assert not (env.data / "schedule_data" / schedule_id).exists()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("delete", "/schedules/nope"),
        ("post", "/schedules/nope/pause"),
        ("post", "/schedules/nope/resume"),
    ],
)
def test_unknown_schedules_are_404(env: Env, method: str, path: str) -> None:
    response = getattr(env.client, method)(path)
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


# ---- startup ----


def test_the_scheduler_starts_with_the_app_and_stops_with_it(tmp_path: Path) -> None:
    env = Env(tmp_path, start_scheduler=True)
    env.recipe("plain", PLAIN)
    assert env.service.running is False
    with TestClient(env.app) as client:
        assert env.service.running is True
        assert client.get("/schedules").json()["running"] is True
    assert env.service.running is False


def test_saved_schedules_are_restored_when_the_app_starts(tmp_path: Path) -> None:
    ScheduleStore(tmp_path / "data").add(
        Schedule(id="restored1", recipe_name="plain", when="hourly", cron="0 * * * *")
    )
    ScheduleStore(tmp_path / "data").add(
        Schedule(
            id="paused001", recipe_name="plain", when="hourly", cron="0 * * * *", enabled=False
        )
    )
    env = Env(tmp_path, start_scheduler=True)
    with TestClient(env.app):
        assert env.service.is_scheduled("restored1")
        assert not env.service.is_scheduled("paused001")


def test_without_start_scheduler_nothing_is_run_in_the_background(env: Env) -> None:
    with TestClient(env.app):
        assert env.service.running is False


def test_the_default_data_folder_sits_next_to_the_jobs_folder(tmp_path: Path) -> None:
    app = create_app(recipes_dir=tmp_path / "r", jobs_dir=tmp_path / "work" / "jobs")
    assert app.state.data_dir == tmp_path / "work" / "data"
