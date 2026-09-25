from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browserforge.app import create_app
from browserforge.jobs import JobStore
from browserforge.results import file_url, list_results, resolve_job_file

PNG = b"\x89PNG\r\n\x1a\n" + b"fake"


def populate(job_dir: Path) -> None:
    (job_dir / "screenshots").mkdir(parents=True)
    (job_dir / "screenshots" / "02_table_20260925-101530.png").write_bytes(PNG)
    (job_dir / "screenshots" / "01_logged_in_20260925-101529.png").write_bytes(PNG)
    (job_dir / "screenshots" / "failure_03_click_20260925-101531.png").write_bytes(PNG)
    (job_dir / "downloads").mkdir()
    (job_dir / "downloads" / "employees.csv").write_bytes(b"id,name\n1,Ann\n")
    (job_dir / "output.xlsx").write_bytes(b"PK-fake-xlsx")
    (job_dir / "run_log.json").write_text('{"success": true}')


# ---- list_results (pure, on a real temp folder) ----


def test_lists_exactly_the_files_a_job_produced(tmp_path: Path) -> None:
    job_dir = tmp_path / "abc"
    populate(job_dir)
    results = list_results("abc", job_dir)

    assert results["job_id"] == "abc"
    assert [s["name"] for s in results["screenshots"]] == [
        "01_logged_in_20260925-101529.png",
        "02_table_20260925-101530.png",
        "failure_03_click_20260925-101531.png",
    ]
    assert results["excel"] == {"name": "output.xlsx", "url": "/jobs/abc/files/output.xlsx"}
    assert results["downloads"] == [
        {"name": "employees.csv", "url": "/jobs/abc/files/downloads/employees.csv"}
    ]
    assert results["run_log"] == {"name": "run_log.json", "url": "/jobs/abc/files/run_log.json"}


def test_screenshots_are_urls_and_failure_screenshots_are_flagged(tmp_path: Path) -> None:
    job_dir = tmp_path / "abc"
    populate(job_dir)
    shots = list_results("abc", job_dir)["screenshots"]
    assert shots[0]["url"] == "/jobs/abc/files/screenshots/01_logged_in_20260925-101529.png"
    assert [s["failure"] for s in shots] == [False, False, True]


def test_empty_job_folder_lists_nothing(tmp_path: Path) -> None:
    job_dir = tmp_path / "abc"
    job_dir.mkdir()
    assert list_results("abc", job_dir) == {
        "job_id": "abc",
        "screenshots": [],
        "excel": None,
        "downloads": [],
        "run_log": None,
    }


def test_only_png_files_count_as_screenshots(tmp_path: Path) -> None:
    job_dir = tmp_path / "abc"
    (job_dir / "screenshots").mkdir(parents=True)
    (job_dir / "screenshots" / "a.png").write_bytes(PNG)
    (job_dir / "screenshots" / "notes.txt").write_text("x")
    (job_dir / "screenshots" / "b.PNG").write_bytes(PNG)
    names = [s["name"] for s in list_results("abc", job_dir)["screenshots"]]
    assert names == ["a.png", "b.PNG"]


def test_subfolders_are_not_listed_as_files(tmp_path: Path) -> None:
    job_dir = tmp_path / "abc"
    (job_dir / "downloads" / "nested").mkdir(parents=True)
    (job_dir / "downloads" / "real.csv").write_text("x")
    assert [d["name"] for d in list_results("abc", job_dir)["downloads"]] == ["real.csv"]


def test_file_names_with_spaces_and_symbols_are_url_encoded(tmp_path: Path) -> None:
    job_dir = tmp_path / "abc"
    (job_dir / "downloads").mkdir(parents=True)
    (job_dir / "downloads" / "my report (1) #2.csv").write_text("x")
    entry = list_results("abc", job_dir)["downloads"][0]
    assert entry["name"] == "my report (1) #2.csv"
    assert entry["url"] == "/jobs/abc/files/downloads/my%20report%20%281%29%20%232.csv"


def test_file_url_encodes_the_job_id_too() -> None:
    assert file_url("a b", Path("x/y.png")) == "/jobs/a%20b/files/x/y.png"


# ---- resolve_job_file: nothing outside the job folder is ever reachable ----


@pytest.fixture
def job_with_secret(tmp_path: Path) -> Path:
    (tmp_path / "secret.txt").write_text("top secret")
    job_dir = tmp_path / "job"
    populate(job_dir)
    return job_dir


def test_resolves_files_inside_the_job(job_with_secret: Path) -> None:
    assert resolve_job_file(job_with_secret, "output.xlsx") == (
        job_with_secret.resolve() / "output.xlsx"
    )
    assert resolve_job_file(job_with_secret, "downloads/employees.csv") is not None


@pytest.mark.parametrize(
    "relative",
    [
        "../secret.txt",
        "../../secret.txt",
        "downloads/../../secret.txt",
        "..\\secret.txt",
        "downloads\\..\\..\\secret.txt",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "..",
        "",
        ".",
        "downloads",  # a directory, not a file
        "missing.txt",
        "bad\x00name.txt",
    ],
)
def test_paths_outside_or_not_files_resolve_to_none(job_with_secret: Path, relative: str) -> None:
    assert resolve_job_file(job_with_secret, relative) is None


def test_absolute_path_to_the_secret_is_refused(job_with_secret: Path) -> None:
    secret = job_with_secret.parent / "secret.txt"
    assert secret.is_file()
    assert resolve_job_file(job_with_secret, str(secret)) is None


def test_symlink_escaping_the_job_folder_is_refused(job_with_secret: Path) -> None:
    link = job_with_secret / "downloads" / "link.txt"
    try:
        link.symlink_to(job_with_secret.parent / "secret.txt")
    except OSError:
        pytest.skip("symlinks need extra privileges on this system")
    assert resolve_job_file(job_with_secret, "downloads/link.txt") is None


# ---- routes ----


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.jobs = tmp_path / "jobs"
        self.store = JobStore()
        self.client = TestClient(
            create_app(recipes_dir=tmp_path / "recipes", jobs_dir=self.jobs, store=self.store)
        )

    def job(self, with_files: bool = True) -> str:
        job_id = self.store.create("demo").job_id
        if with_files:
            populate(self.jobs / job_id)
        return job_id


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_results_endpoint_lists_the_jobs_files(env: Env) -> None:
    job_id = env.job()
    response = env.client.get(f"/jobs/{job_id}/results")
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job_id
    assert len(body["screenshots"]) == 3
    assert body["excel"]["url"] == f"/jobs/{job_id}/files/output.xlsx"
    assert [d["name"] for d in body["downloads"]] == ["employees.csv"]


def test_every_listed_url_can_be_fetched(env: Env) -> None:
    job_id = env.job()
    body = env.client.get(f"/jobs/{job_id}/results").json()
    urls = [s["url"] for s in body["screenshots"]]
    urls += [body["excel"]["url"], body["run_log"]["url"]] + [d["url"] for d in body["downloads"]]
    assert len(urls) == 6
    for url in urls:
        assert env.client.get(url).status_code == 200, url


def test_results_for_an_unknown_job_is_a_clear_404(env: Env) -> None:
    response = env.client.get("/jobs/nope/results")
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_results_for_a_job_that_has_not_produced_files_is_a_404(env: Env) -> None:
    job_id = env.job(with_files=False)
    response = env.client.get(f"/jobs/{job_id}/results")
    assert response.status_code == 404
    assert "no results yet" in response.json()["detail"]


def test_results_of_an_empty_job_folder_is_200_with_empty_lists(env: Env) -> None:
    job_id = env.job(with_files=False)
    (env.jobs / job_id).mkdir(parents=True)
    body = env.client.get(f"/jobs/{job_id}/results").json()
    assert body["screenshots"] == []
    assert body["excel"] is None


def test_screenshots_are_served_inline_as_images(env: Env) -> None:
    job_id = env.job()
    name = "01_logged_in_20260925-101529.png"
    response = env.client.get(f"/jobs/{job_id}/files/screenshots/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "attachment" not in response.headers.get("content-disposition", "")
    assert response.content == PNG


def test_excel_and_downloads_are_served_as_attachments(env: Env) -> None:
    job_id = env.job()
    xlsx = env.client.get(f"/jobs/{job_id}/files/output.xlsx")
    assert xlsx.content == b"PK-fake-xlsx"
    assert 'filename="output.xlsx"' in xlsx.headers["content-disposition"]
    assert "attachment" in xlsx.headers["content-disposition"]

    csv = env.client.get(f"/jobs/{job_id}/files/downloads/employees.csv")
    assert csv.content == b"id,name\n1,Ann\n"


def test_missing_file_is_a_404(env: Env) -> None:
    job_id = env.job()
    assert env.client.get(f"/jobs/{job_id}/files/nope.txt").status_code == 404


def test_unknown_job_file_request_is_a_404(env: Env) -> None:
    assert env.client.get("/jobs/nope/files/output.xlsx").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "..%2F..%2Fsecret.txt",
        "%2e%2e/%2e%2e/secret.txt",
        "downloads/..%2F..%2F..%2Fsecret.txt",
        "..%5C..%5Csecret.txt",
        "%2Fetc%2Fpasswd",
        "C%3A%2FWindows%2Fwin.ini",
    ],
)
def test_traversal_attempts_never_return_outside_files(env: Env, tmp_path: Path, path: str) -> None:
    job_id = env.job()  # creates the jobs folder
    (tmp_path / "secret.txt").write_text("top secret")
    (env.jobs / "secret.txt").write_text("also secret")
    response = env.client.get(f"/jobs/{job_id}/files/{path}")
    assert response.status_code == 404
    assert "secret" not in response.text


def test_a_job_id_cannot_be_used_to_reach_other_folders(env: Env) -> None:
    env.job()
    assert env.client.get("/jobs/../files/x").status_code == 404
    assert env.client.get("/jobs/%2e%2e/files/x").status_code == 404
