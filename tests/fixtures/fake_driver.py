"""In-memory PageDriver test double. Records calls, returns scripted data, can be told to fail."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from browserforge.engine.driver import (
    DriverTimeoutError,
    ElementNotFoundError,
    ElementState,
)

DRIVER_METHODS = frozenset(
    {
        "goto",
        "current_url",
        "fill",
        "click",
        "select_option",
        "check",
        "upload",
        "screenshot",
        "wait_for_download",
        "get_table_rows",
        "wait_for_selector",
        "get_element_state",
    }
)


@dataclass
class _PagedTable:
    table: str
    next_button: str
    pages: list[list[list[str]]]
    infinite: bool
    index: int = 0

    def has_next(self) -> bool:
        return self.infinite or self.index < len(self.pages) - 1

    def advance(self) -> None:
        if self.has_next():
            self.index = (self.index + 1) % len(self.pages) if self.infinite else self.index + 1


class FakePageDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.url = ""
        self.fills: dict[str, str] = {}
        self.clicks: list[str] = []
        self.selected: dict[str, str] = {}
        self.checked: list[str] = []
        self.uploads: list[tuple[str, list[str]]] = []
        self.screenshots: list[str] = []
        self._tables: dict[str, list[list[str]]] = {}
        self._paged: dict[str, _PagedTable] = {}
        self._states: dict[str, ElementState] = {}
        self._missing: set[str] = set()
        self._downloads: list[str] = []
        self._url_on_click: dict[str, str] = {}
        self._failures: dict[str, list[Exception]] = {}
        self._always_fail: dict[str, Exception] = {}

    # ---- scripting helpers (used by tests, not part of PageDriver) ----

    def set_table(self, selector: str, rows: list[list[str]]) -> None:
        self._tables[selector] = rows

    def set_pages(
        self,
        table: str,
        next_button: str,
        pages: list[list[list[str]]],
        infinite: bool = False,
    ) -> None:
        """Script a paginated table: clicking `next_button` advances to the next page.

        With infinite=True the next button never disables and pages cycle forever.
        """
        paged = _PagedTable(table, next_button, pages, infinite)
        self._paged[table] = paged
        self._paged[next_button] = paged

    def set_element_state(
        self, selector: str, visible: bool = True, disabled: bool = False
    ) -> None:
        self._states[selector] = {"visible": visible, "disabled": disabled}

    def set_missing(self, *selectors: str) -> None:
        """Make these selectors behave as not present on the page."""
        self._missing.update(selectors)

    def queue_download(self, path: str) -> None:
        self._downloads.append(path)

    def set_url_on_click(self, selector: str, url: str) -> None:
        self._url_on_click[selector] = url

    def fail_on(self, method: str, exc: Exception, times: int | None = 1) -> None:
        """Make `method` raise `exc` for the next `times` calls (None = every call)."""
        if method not in DRIVER_METHODS:
            raise ValueError(f"Unknown driver method: {method}")
        if times is None:
            self._always_fail[method] = exc
        else:
            self._failures.setdefault(method, []).extend([exc] * times)

    def calls_to(self, method: str) -> list[tuple[Any, ...]]:
        return [args for name, args in self.calls if name == method]

    # ---- internals ----

    def _enter(self, method: str, *args: Any, selector: str | None = None) -> None:
        self.calls.append((method, args))
        if method in self._always_fail:
            raise self._always_fail[method]
        queued = self._failures.get(method)
        if queued:
            raise queued.pop(0)
        if selector is not None and selector in self._missing:
            raise ElementNotFoundError(f"No element matches selector '{selector}'")

    # ---- PageDriver implementation ----

    def goto(self, url: str) -> None:
        self._enter("goto", url)
        self.url = url

    def current_url(self) -> str:
        self._enter("current_url")
        return self.url

    def fill(self, selector: str, value: str) -> None:
        self._enter("fill", selector, value, selector=selector)
        self.fills[selector] = value

    def click(self, selector: str) -> None:
        self._enter("click", selector, selector=selector)
        self.clicks.append(selector)
        paged = self._paged.get(selector)
        if paged is not None and paged.next_button == selector:
            paged.advance()
        if selector in self._url_on_click:
            self.url = self._url_on_click[selector]

    def select_option(self, selector: str, value: str) -> None:
        self._enter("select_option", selector, value, selector=selector)
        self.selected[selector] = value

    def check(self, selector: str) -> None:
        self._enter("check", selector, selector=selector)
        self.checked.append(selector)

    def upload(self, selector: str, paths: Sequence[str]) -> None:
        self._enter("upload", selector, list(paths), selector=selector)
        self.uploads.append((selector, list(paths)))

    def screenshot(self, path: str, selector: str | None = None, full_page: bool = True) -> None:
        self._enter("screenshot", path, selector, full_page, selector=selector)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-png")
        self.screenshots.append(path)

    def wait_for_download(self) -> str:
        self._enter("wait_for_download")
        if not self._downloads:
            raise DriverTimeoutError("No download started before the timeout")
        return self._downloads.pop(0)

    def get_table_rows(self, selector: str) -> list[list[str]]:
        self._enter("get_table_rows", selector, selector=selector)
        paged = self._paged.get(selector)
        if paged is not None and paged.table == selector:
            return paged.pages[paged.index]
        if selector in self._tables:
            return self._tables[selector]
        raise ElementNotFoundError(f"No table matches selector '{selector}'")

    def wait_for_selector(self, selector: str, timeout_ms: int = 30_000) -> None:
        self._enter("wait_for_selector", selector, timeout_ms)
        if selector in self._missing:
            raise DriverTimeoutError(f"Timed out after {timeout_ms}ms waiting for '{selector}'")

    def get_element_state(self, selector: str) -> ElementState | None:
        self._enter("get_element_state", selector)
        if selector in self._missing:
            return None
        paged = self._paged.get(selector)
        if paged is not None and paged.next_button == selector:
            return {"visible": True, "disabled": not paged.has_next()}
        return self._states.get(selector)
