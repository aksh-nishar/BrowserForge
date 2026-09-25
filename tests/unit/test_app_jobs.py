from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browserforge.app import create_app
from browserforge.jobs import JobStore
from fixtures.fake_driver import FakePageDriver

RECIPE = "url: https://x.io\nsteps:\n  - type: click\n    selector: '#go'\n"


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

    def add_recipe(self, name: str = "demo", text: str = RECIPE) -> None:
        (self.recipes / f"{name}.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ---- POST /jobs ----


def test_post_jobs_returns_202_with_a_job_id_immediately(env: Env) -> None:
    env.add_recipe()
    response = env.client.post("/jobs", json={"recipe_name": "demo"})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert len(body["job_id"]) == 12


def test_the_recipe_is_run_in_the_background_with_the_injected_driver(env: Env) -> None:
    env.add_recipe()
    job_id = env.client.post("/jobs", json={"recipe_name": "demo"}).json()["job_id"]

    assert [name for name, _ in env.fake.calls] == ["goto", "click"]  # start page, then the step
    assert env.fake.calls[0] == ("goto", ("https://x.io",))
    assert (env.jobs / job_id / "run_log.json").is_file()


def test_status_reflects_the_store_after_the_run(env: Env) -> None:
    env.add_recipe()
    job_id = env.client.post("/jobs", json={"recipe_name": "demo"}).json()["job_id"]
    status = env.client.get(f"/status/{job_id}").json()
    assert status["status"] == "complete"
    assert status["done"] is True
    assert env.store.get(job_id).status == "complete"


def test_a_failing_recipe_ends_as_failed(env: Env) -> None:
    env.add_recipe()
    env.fake.set_missing("#go")
    job_id = env.client.post("/jobs", json={"recipe_name": "demo"}).json()["job_id"]
    assert env.client.get(f"/status/{job_id}").json()["status"] == "failed"


def test_a_browser_that_cannot_start_ends_as_failed_with_an_error(tmp_path: Path) -> None:
    (tmp_path / "recipes").mkdir()
    (tmp_path / "recipes" / "demo.yaml").write_text(RECIPE, encoding="utf-8")

    def broken() -> object:
        raise RuntimeError("no browser")

    client = TestClient(
        create_app(
            recipes_dir=tmp_path / "recipes",
            jobs_dir=tmp_path / "jobs",
            driver_factory=broken,  # type: ignore[arg-type]
        )
    )
    job_id = client.post("/jobs", json={"recipe_name": "demo"}).json()["job_id"]
    status = client.get(f"/status/{job_id}").json()
    assert status["status"] == "failed"
    assert "no browser" in status["error"]


def test_each_request_creates_a_separate_job(env: Env) -> None:
    env.add_recipe()
    first = env.client.post("/jobs", json={"recipe_name": "demo"}).json()["job_id"]
    second = env.client.post("/jobs", json={"recipe_name": "demo"}).json()["job_id"]
    assert first != second
    assert (env.jobs / first).is_dir()
    assert (env.jobs / second).is_dir()


# ---- POST /jobs: bad requests ----


def test_unknown_recipe_is_a_404_naming_it(env: Env) -> None:
    response = env.client.post("/jobs", json={"recipe_name": "nope"})
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


@pytest.mark.parametrize("name", ["../secret", "a/b", "..", "x.yaml", "a b"])
def test_unsafe_recipe_names_are_rejected_as_not_found(env: Env, name: str) -> None:
    assert env.client.post("/jobs", json={"recipe_name": name}).status_code == 404


def test_recipe_that_became_invalid_on_disk_is_a_400(env: Env) -> None:
    env.add_recipe("broken", "steps: []\n")
    response = env.client.post("/jobs", json={"recipe_name": "broken"})
    assert response.status_code == 400
    assert "'url'" in response.json()["detail"]
    assert not env.jobs.exists()  # no job was created, so no job folder either


@pytest.mark.parametrize("body", [{}, {"recipe_name": ""}, {"recipe_name": 5}, {"other": "x"}])
def test_malformed_bodies_are_422(env: Env, body: dict[str, object]) -> None:
    assert env.client.post("/jobs", json=body).status_code == 422


# ---- GET /status ----


def test_status_of_an_unknown_job_is_a_clear_404(env: Env) -> None:
    response = env.client.get("/status/doesnotexist")
    assert response.status_code == 404
    assert "doesnotexist" in response.json()["detail"]


def test_status_of_a_pending_job(env: Env) -> None:
    job = env.store.create("demo")
    status = env.client.get(f"/status/{job.job_id}").json()
    assert status["status"] == "pending"
    assert status["done"] is False
