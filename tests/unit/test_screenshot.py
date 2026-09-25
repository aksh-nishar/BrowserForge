import re
from datetime import datetime
from pathlib import Path

import pytest

from browserforge.engine.actions import ActionContext, do_screenshot, screenshot_path
from browserforge.engine.driver import DriverTimeoutError
from browserforge.engine.recipe import Step
from fixtures.fake_driver import FakePageDriver

FIXED_NOW = datetime(2026, 9, 25, 10, 15, 30)


def shot_step(name: str | None = "after_login", **config: object) -> Step:
    return Step(type="screenshot", name=name, config=dict(config))


@pytest.fixture
def ctx(tmp_path: Path) -> ActionContext:
    return ActionContext(job_dir=tmp_path / "job1", step_index=3, now=lambda: FIXED_NOW)


# ---- filename format ----


def test_filename_has_index_step_name_and_timestamp(ctx: ActionContext) -> None:
    path = screenshot_path(ctx, shot_step())
    assert path.parent == ctx.job_dir / "screenshots"
    assert re.fullmatch(r"03_after_login_\d{8}-\d{6}\.png", path.name)
    assert path.name == "03_after_login_20260925-101530.png"


def test_default_clock_produces_timestamp_in_the_expected_shape(tmp_path: Path) -> None:
    path = screenshot_path(ActionContext(job_dir=tmp_path), shot_step())
    assert re.fullmatch(r"01_after_login_\d{8}-\d{6}\.png", path.name)


def test_step_name_is_sanitised_for_the_filesystem(ctx: ActionContext) -> None:
    path = screenshot_path(ctx, shot_step(name="My Step/../1!"))
    assert path.parent == ctx.job_dir / "screenshots"
    assert path.name == "03_My_Step_1_20260925-101530.png"


def test_unnamed_step_falls_back_to_step_type(ctx: ActionContext) -> None:
    assert "_screenshot_" in screenshot_path(ctx, shot_step(name=None)).name


def test_symbol_only_name_falls_back_to_generic_name(ctx: ActionContext) -> None:
    assert "_step_" in screenshot_path(ctx, shot_step(name="///")).name


def test_different_step_indexes_give_different_filenames(tmp_path: Path) -> None:
    def make(index: int) -> Path:
        return screenshot_path(
            ActionContext(job_dir=tmp_path, step_index=index, now=lambda: FIXED_NOW),
            shot_step(),
        )

    assert make(1) != make(2)


# ---- full page vs element ----


def test_without_selector_takes_full_page_screenshot(ctx: ActionContext) -> None:
    fake = FakePageDriver()
    result = do_screenshot(fake, shot_step(), ctx)
    expected = str(screenshot_path(ctx, shot_step()))
    assert result.success
    assert fake.calls == [("screenshot", (expected, None, True))]


def test_with_selector_takes_element_screenshot(ctx: ActionContext) -> None:
    fake = FakePageDriver()
    result = do_screenshot(fake, shot_step(selector="#chart"), ctx)
    expected = str(screenshot_path(ctx, shot_step()))
    assert result.success
    assert fake.calls == [("screenshot", (expected, "#chart", False))]


def test_full_page_false_is_respected_without_selector(ctx: ActionContext) -> None:
    fake = FakePageDriver()
    do_screenshot(fake, shot_step(full_page=False), ctx)
    assert fake.calls_to("screenshot")[0][2] is False


# ---- output ----


def test_result_data_path_matches_file_written(ctx: ActionContext) -> None:
    fake = FakePageDriver()
    result = do_screenshot(fake, shot_step(), ctx)
    assert result.data["path"] == fake.screenshots[0]
    assert Path(result.data["path"]).exists()


def test_screenshots_folder_is_created_on_demand(ctx: ActionContext) -> None:
    assert not ctx.job_dir.exists()
    do_screenshot(FakePageDriver(), shot_step(), ctx)
    assert (ctx.job_dir / "screenshots").is_dir()


# ---- failures ----


@pytest.mark.parametrize("bad", ["", "   ", 5])
def test_invalid_selector_fails_without_calling_driver(bad: object, ctx: ActionContext) -> None:
    fake = FakePageDriver()
    result = do_screenshot(fake, shot_step(selector=bad), ctx)
    assert not result.success
    assert result.error is not None
    assert "selector" in result.error
    assert fake.calls == []


def test_element_not_found_is_a_failed_result(ctx: ActionContext) -> None:
    fake = FakePageDriver()
    fake.set_missing("#chart")
    result = do_screenshot(fake, shot_step(selector="#chart"), ctx)
    assert not result.success
    assert result.error is not None
    assert "#chart" in result.error
    assert result.data == {}


def test_driver_timeout_is_a_failed_result(ctx: ActionContext) -> None:
    fake = FakePageDriver()
    fake.fail_on("screenshot", DriverTimeoutError("Timed out after 30000ms"))
    result = do_screenshot(fake, shot_step(), ctx)
    assert not result.success
    assert result.error is not None
    assert "Timed out after 30000ms" in result.error
