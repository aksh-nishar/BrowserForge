from pathlib import Path

import pytest

from browserforge.engine.actions import ActionContext, do_download
from browserforge.engine.driver import DriverError
from browserforge.engine.recipe import Step
from fixtures.fake_driver import FakePageDriver


def download_step(**config: object) -> Step:
    return Step(type="download", config=dict(config))


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    return tmp_path / "job1"  # deliberately not created: the action must create what it needs


@pytest.fixture
def ctx(job_dir: Path) -> ActionContext:
    return ActionContext(job_dir=job_dir)


@pytest.fixture
def browser_download(tmp_path: Path) -> Path:
    """A file where the browser would have left the finished download."""
    file = tmp_path / "browser_tmp" / "report.csv"
    file.parent.mkdir()
    file.write_text("a,b\n1,2\n")
    return file


def test_clicks_then_waits_for_download(browser_download: Path, ctx: ActionContext) -> None:
    fake = FakePageDriver()
    fake.queue_download(str(browser_download))
    result = do_download(fake, download_step(selector="#export"), ctx)
    assert result.success
    assert fake.calls == [("click", ("#export",)), ("wait_for_download", ())]


def test_file_is_moved_into_job_downloads_dir(
    browser_download: Path, ctx: ActionContext, job_dir: Path
) -> None:
    fake = FakePageDriver()
    fake.queue_download(str(browser_download))
    result = do_download(fake, download_step(selector="#export"), ctx)
    saved = job_dir / "downloads" / "report.csv"
    assert result.data == {"path": str(saved)}
    assert saved.read_text() == "a,b\n1,2\n"
    assert not browser_download.exists()  # moved, not copied


def test_save_as_renames_the_file(
    browser_download: Path, ctx: ActionContext, job_dir: Path
) -> None:
    fake = FakePageDriver()
    fake.queue_download(str(browser_download))
    result = do_download(fake, download_step(selector="#export", save_as="payroll.csv"), ctx)
    assert result.data["path"] == str(job_dir / "downloads" / "payroll.csv")
    assert (job_dir / "downloads" / "payroll.csv").exists()


def test_save_as_cannot_escape_the_downloads_dir(
    browser_download: Path, ctx: ActionContext, job_dir: Path
) -> None:
    fake = FakePageDriver()
    fake.queue_download(str(browser_download))
    result = do_download(fake, download_step(selector="#export", save_as="../../evil.csv"), ctx)
    assert result.success
    assert result.data["path"] == str(job_dir / "downloads" / "evil.csv")
    assert not (job_dir.parent / "evil.csv").exists()


def test_existing_file_is_never_overwritten(tmp_path: Path, ctx: ActionContext) -> None:
    fake = FakePageDriver()
    for i in range(2):
        source = tmp_path / f"src{i}" / "report.csv"
        source.parent.mkdir()
        source.write_text(f"run {i}")
        fake.queue_download(str(source))

    first = do_download(fake, download_step(selector="#export"), ctx)
    second = do_download(fake, download_step(selector="#export"), ctx)
    assert Path(first.data["path"]).name == "report.csv"
    assert Path(second.data["path"]).name == "report_2.csv"
    assert Path(first.data["path"]).read_text() == "run 0"
    assert Path(second.data["path"]).read_text() == "run 1"


def test_no_download_started_is_a_clear_failure(ctx: ActionContext, job_dir: Path) -> None:
    fake = FakePageDriver()  # nothing queued -> wait_for_download times out
    step = Step(type="download", name="get_report", config={"selector": "#export"})
    result = do_download(fake, step, ctx)
    assert not result.success
    assert result.error is not None
    assert result.error.startswith("Step 'get_report'")
    assert "no download started" in result.error
    assert not (job_dir / "downloads").exists()


def test_click_failure_skips_waiting(ctx: ActionContext) -> None:
    fake = FakePageDriver()
    fake.set_missing("#export")
    result = do_download(fake, download_step(selector="#export"), ctx)
    assert not result.success
    assert result.error is not None
    assert "#export" in result.error
    assert fake.calls_to("wait_for_download") == []


def test_other_driver_error_while_waiting_is_reported(ctx: ActionContext) -> None:
    fake = FakePageDriver()
    fake.fail_on("wait_for_download", DriverError("download was cancelled"))
    result = do_download(fake, download_step(selector="#export"), ctx)
    assert not result.success
    assert result.error is not None
    assert "download was cancelled" in result.error


def test_reported_file_that_does_not_exist_fails(tmp_path: Path, ctx: ActionContext) -> None:
    fake = FakePageDriver()
    ghost = tmp_path / "ghost.csv"
    fake.queue_download(str(ghost))
    result = do_download(fake, download_step(selector="#export"), ctx)
    assert not result.success
    assert result.error is not None
    assert "missing" in result.error
    assert str(ghost) in result.error


def test_missing_selector_fails_without_calling_driver(ctx: ActionContext) -> None:
    fake = FakePageDriver()
    result = do_download(fake, download_step(), ctx)
    assert not result.success
    assert result.error is not None
    assert "selector" in result.error
    assert fake.calls == []


@pytest.mark.parametrize("bad", ["", "   ", 123, "/", "..", "a/.."])
def test_invalid_save_as_fails_without_calling_driver(bad: object, ctx: ActionContext) -> None:
    fake = FakePageDriver()
    result = do_download(fake, download_step(selector="#export", save_as=bad), ctx)
    assert not result.success
    assert result.error is not None
    assert "save_as" in result.error
    assert fake.calls == []
