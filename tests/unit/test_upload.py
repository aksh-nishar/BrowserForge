from pathlib import Path

import pytest

from browserforge.engine.actions import ActionResult, do_upload
from browserforge.engine.driver import DriverTimeoutError
from browserforge.engine.recipe import Step
from fixtures.fake_driver import FakePageDriver


def upload_step(**config: object) -> Step:
    return Step(type="upload", config=dict(config))


@pytest.fixture
def files(tmp_path: Path) -> list[str]:
    paths = []
    for name in ("a.txt", "b.txt"):
        file = tmp_path / name
        file.write_text("data")
        paths.append(str(file))
    return paths


def test_single_path_string_is_passed_as_one_item_list(files: list[str]) -> None:
    fake = FakePageDriver()
    result = do_upload(fake, upload_step(selector="#file", files=files[0]))
    assert result == ActionResult(success=True)
    assert fake.calls == [("upload", ("#file", [files[0]]))]


def test_multiple_paths_are_passed_through_as_list(files: list[str]) -> None:
    fake = FakePageDriver()
    result = do_upload(fake, upload_step(selector="#file", files=files))
    assert result.success
    assert fake.uploads == [("#file", files)]


def test_nonexistent_file_fails_before_calling_driver(tmp_path: Path) -> None:
    fake = FakePageDriver()
    ghost = str(tmp_path / "ghost.pdf")
    result = do_upload(fake, upload_step(selector="#file", files=ghost))
    assert not result.success
    assert result.error is not None
    assert "not found" in result.error
    assert ghost in result.error
    assert fake.calls == []


def test_all_missing_files_are_reported_and_none_uploaded(files: list[str], tmp_path: Path) -> None:
    fake = FakePageDriver()
    ghost1, ghost2 = str(tmp_path / "g1.txt"), str(tmp_path / "g2.txt")
    result = do_upload(fake, upload_step(selector="#file", files=[files[0], ghost1, ghost2]))
    assert not result.success
    assert result.error is not None
    assert ghost1 in result.error
    assert ghost2 in result.error
    assert files[0] not in result.error
    assert fake.calls == []


def test_directory_is_not_accepted_as_a_file(tmp_path: Path) -> None:
    fake = FakePageDriver()
    result = do_upload(fake, upload_step(selector="#file", files=str(tmp_path)))
    assert not result.success
    assert fake.calls == []


def test_missing_selector_fails_without_calling_driver(files: list[str]) -> None:
    fake = FakePageDriver()
    result = do_upload(fake, upload_step(files=files[0]))
    assert not result.success
    assert result.error is not None
    assert "selector" in result.error
    assert fake.calls == []


@pytest.mark.parametrize("empty", [None, "", []])
def test_missing_or_empty_files_fails(empty: object) -> None:
    fake = FakePageDriver()
    step = (
        upload_step(selector="#file")
        if empty is None
        else upload_step(selector="#file", files=empty)
    )
    result = do_upload(fake, step)
    assert not result.success
    assert result.error is not None
    assert "files" in result.error
    assert fake.calls == []


@pytest.mark.parametrize("bad", [42, {"a": "b"}, [1, 2], ["ok.txt", None], ["  "]])
def test_wrongly_typed_files_fails(bad: object) -> None:
    fake = FakePageDriver()
    result = do_upload(fake, upload_step(selector="#file", files=bad))
    assert not result.success
    assert result.error is not None
    assert "'files' must be" in result.error
    assert fake.calls == []


def test_driver_failure_becomes_failed_result(files: list[str]) -> None:
    fake = FakePageDriver()
    fake.fail_on("upload", DriverTimeoutError("Timed out after 30000ms"))
    result = do_upload(fake, upload_step(selector="#file", files=files[0]))
    assert not result.success
    assert result.error is not None
    assert "Timed out after 30000ms" in result.error


def test_missing_file_input_on_page_becomes_failed_result(files: list[str]) -> None:
    fake = FakePageDriver()
    fake.set_missing("#file")
    result = do_upload(fake, upload_step(selector="#file", files=files[0]))
    assert not result.success
    assert result.error is not None
    assert "#file" in result.error
    assert fake.uploads == []
