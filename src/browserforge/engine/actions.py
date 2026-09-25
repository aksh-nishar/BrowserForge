"""One function per step type: (driver, step) -> ActionResult.

Actions that write files (download, screenshot) also take an ActionContext.

Actions never raise for expected failures (bad config, driver timeouts, missing
elements); they return ActionResult(success=False, error=...) so the runner has a
single failure path. Unexpected exceptions are bugs and are left to propagate.
"""

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from browserforge.engine.driver import DriverError, DriverTimeoutError, PageDriver
from browserforge.engine.pagination import DEFAULT_MAX_PAGES, paginate_and_collect
from browserforge.engine.recipe import Step
from browserforge.engine.table_extract import extract_table

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ActionResult:
    success: bool
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionContext:
    """Per-step run context: where to write files and what time it is."""

    job_dir: Path
    step_index: int = 1
    now: Callable[[], datetime] = datetime.now


def _label(step: Step) -> str:
    return f"Step '{step.name or step.type}'"


def _fail(step: Step, message: str) -> ActionResult:
    return ActionResult(success=False, error=f"{_label(step)}: {message}")


def _selector_of(step: Step) -> str | None:
    selector = step.config.get("selector")
    if isinstance(selector, str) and selector.strip():
        return selector
    return None


def _run(
    step: Step,
    call: Callable[[str, str], None],
    needs_value: bool = False,
) -> ActionResult:
    """Validate selector (and value), run `call(selector, value)`, map driver errors."""
    selector = _selector_of(step)
    if selector is None:
        return _fail(step, "missing required field 'selector'")

    value = ""
    if needs_value:
        raw = step.config.get("value")
        if raw is None:
            return _fail(step, "missing required field 'value'")
        value = str(raw)

    try:
        call(selector, value)
    except DriverError as exc:
        return _fail(step, str(exc))
    return ActionResult(success=True)


def do_fill(driver: PageDriver, step: Step) -> ActionResult:
    return _run(step, lambda selector, value: driver.fill(selector, value), needs_value=True)


def do_click(driver: PageDriver, step: Step) -> ActionResult:
    return _run(step, lambda selector, _value: driver.click(selector))


def do_select_option(driver: PageDriver, step: Step) -> ActionResult:
    return _run(
        step,
        lambda selector, value: driver.select_option(selector, value),
        needs_value=True,
    )


def do_check(driver: PageDriver, step: Step) -> ActionResult:
    return _run(step, lambda selector, _value: driver.check(selector))


def do_upload(driver: PageDriver, step: Step) -> ActionResult:
    """Upload one file (str) or several (list) to a file input.

    Every path is checked on disk first, so a typo fails fast with a clear message
    instead of a confusing browser error.
    """
    selector = _selector_of(step)
    if selector is None:
        return _fail(step, "missing required field 'selector'")

    raw = step.config.get("files")
    if raw is None or raw == [] or raw == "":
        return _fail(step, "missing required field 'files'")
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list) or not all(isinstance(p, str) and p.strip() for p in items):
        return _fail(step, "'files' must be a file path or a list of file paths")

    missing = [p for p in items if not Path(p).is_file()]
    if missing:
        return _fail(step, f"file(s) not found: {', '.join(missing)}")

    try:
        driver.upload(selector, items)
    except DriverError as exc:
        return _fail(step, str(exc))
    return ActionResult(success=True)


def _unique_path(directory: Path, filename: str) -> Path:
    """`directory/filename`, or `name_2.ext`, `name_3.ext`... if it already exists."""
    candidate = directory / filename
    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def do_download(driver: PageDriver, step: Step, ctx: ActionContext) -> ActionResult:
    """Click `selector` to trigger a download, then move the file into job_dir/downloads/.

    Optional `save_as` renames the file (directory parts are stripped). Existing files
    are never overwritten. On success, data["path"] is the saved location.
    """
    selector = _selector_of(step)
    if selector is None:
        return _fail(step, "missing required field 'selector'")

    save_as = step.config.get("save_as")
    if save_as is not None and (
        not isinstance(save_as, str) or Path(save_as).name.strip() in ("", ".", "..")
    ):
        return _fail(step, "'save_as' must be a file name")

    try:
        driver.click(selector)
        downloaded = Path(driver.wait_for_download())
    except DriverTimeoutError:
        return _fail(step, "no download started after the click (timed out waiting for it)")
    except DriverError as exc:
        return _fail(step, str(exc))

    if not downloaded.is_file():
        return _fail(step, f"the browser reported a download but the file is missing: {downloaded}")

    downloads_dir = ctx.job_dir / "downloads"
    filename = Path(save_as).name if isinstance(save_as, str) else downloaded.name
    try:
        downloads_dir.mkdir(parents=True, exist_ok=True)
        destination = _unique_path(downloads_dir, filename)
        shutil.move(str(downloaded), str(destination))
    except OSError as exc:
        return _fail(step, f"could not save the downloaded file: {exc}")
    return ActionResult(success=True, data={"path": str(destination)})


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_") or "step"


def screenshot_path(ctx: ActionContext, step: Step) -> Path:
    """job_dir/screenshots/<index>_<step name>_<YYYYmmdd-HHMMSS>.png"""
    stamp = ctx.now().strftime("%Y%m%d-%H%M%S")
    name = _safe_name(step.name or step.type)
    return ctx.job_dir / "screenshots" / f"{ctx.step_index:02d}_{name}_{stamp}.png"


def do_screenshot(driver: PageDriver, step: Step, ctx: ActionContext) -> ActionResult:
    """Full-page screenshot, or just one element when `selector` is given.

    On success, data["path"] is the saved image.
    """
    selector = step.config.get("selector")
    if selector is not None and (not isinstance(selector, str) or not selector.strip()):
        return _fail(step, "'selector' must be a non-empty string when given")

    path = screenshot_path(ctx, step)
    full_page = selector is None and bool(step.config.get("full_page", True))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        driver.screenshot(str(path), selector=selector, full_page=full_page)
    except DriverError as exc:
        return _fail(step, str(exc))
    except OSError as exc:
        return _fail(step, f"could not create the screenshots folder: {exc}")
    return ActionResult(success=True, data={"path": str(path)})


def _has_header_of(step: Step) -> bool | None:
    """The step's `has_header` flag (default True), or None if it is not a boolean."""
    value = step.config.get("has_header", True)
    return value if isinstance(value, bool) else None


def do_extract_table(driver: PageDriver, step: Step) -> ActionResult:
    """Read the table at `selector` on the current page. data["rows"] is a list of dicts."""
    selector = _selector_of(step)
    if selector is None:
        return _fail(step, "missing required field 'selector'")
    has_header = _has_header_of(step)
    if has_header is None:
        return _fail(step, "'has_header' must be true or false")

    try:
        raw = driver.get_table_rows(selector)
    except DriverError as exc:
        return _fail(step, str(exc))
    return ActionResult(success=True, data={"rows": extract_table(raw, has_header)})


def do_paginate(driver: PageDriver, step: Step) -> ActionResult:
    """Collect a table across pages. See paginate_and_collect.

    Config: selector (table), next_selector, optional max_pages (default 50) and
    has_header. data: rows (list of dicts), pages, truncated (True if the cap cut it short).
    """
    selector = _selector_of(step)
    if selector is None:
        return _fail(step, "missing required field 'selector'")
    next_selector = step.config.get("next_selector")
    if not isinstance(next_selector, str) or not next_selector.strip():
        return _fail(step, "missing required field 'next_selector'")
    has_header = _has_header_of(step)
    if has_header is None:
        return _fail(step, "'has_header' must be true or false")
    max_pages = step.config.get("max_pages", DEFAULT_MAX_PAGES)
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        return _fail(step, "'max_pages' must be a positive integer")

    try:
        collected = paginate_and_collect(driver, selector, next_selector, max_pages, has_header)
    except DriverError as exc:
        return _fail(step, str(exc))

    if collected.hit_cap:
        logger.warning("pagination_cap_reached", step=step.name or step.type, max_pages=max_pages)
    return ActionResult(
        success=True,
        data={
            "rows": extract_table(collected.rows, has_header),
            "pages": collected.pages,
            "truncated": collected.hit_cap,
        },
    )


_LOGIN_REQUIRED = (
    "username_selector",
    "password_selector",
    "submit_selector",
    "username",
    "password",
)
_DEFAULT_LOGIN_TIMEOUT_MS = 30_000


def _login_failed(reason: str, message: str) -> ActionResult:
    # Never log credentials: only the reason and the (already redacted) message.
    logger.warning("login_failed", reason=reason, error=message)
    return ActionResult(success=False, error=message)


def do_login(driver: PageDriver, step: Step) -> ActionResult:
    """Fill credentials, submit, then wait for the post-login marker.

    Config: username_selector, password_selector, submit_selector, username, password,
    plus success_selector and/or success_url_contains; optional timeout_ms.
    """
    config = step.config

    missing = [key for key in _LOGIN_REQUIRED if not str(config.get(key) or "").strip()]
    if missing:
        return ActionResult(
            success=False,
            error=f"Login step is missing required field(s): {', '.join(missing)}",
        )

    success_selector = config.get("success_selector")
    success_url = config.get("success_url_contains")
    if not success_selector and not success_url:
        return ActionResult(
            success=False,
            error=(
                "Login step needs 'success_selector' or 'success_url_contains' "
                "to know when login worked"
            ),
        )

    timeout_ms = config.get("timeout_ms", _DEFAULT_LOGIN_TIMEOUT_MS)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        return ActionResult(success=False, error="Login 'timeout_ms' must be a positive integer")

    password = str(config["password"])

    def redact(text: str) -> str:
        return text.replace(password, "***")

    try:
        driver.fill(str(config["username_selector"]), str(config["username"]))
        driver.fill(str(config["password_selector"]), password)
        driver.click(str(config["submit_selector"]))
    except DriverError as exc:
        return _login_failed("form_error", f"Login failed: {redact(str(exc))}")

    try:
        if success_selector:
            driver.wait_for_selector(str(success_selector), timeout_ms)
        if success_url:
            driver.wait_for_url_contains(str(success_url), timeout_ms)
    except DriverTimeoutError:
        return _login_failed(
            "post_login_timeout",
            f"Login failed: the logged-in page did not appear within {timeout_ms}ms. "
            "Check the username and password.",
        )
    except DriverError as exc:
        return _login_failed("post_login_error", f"Login failed: {redact(str(exc))}")

    return ActionResult(success=True)
