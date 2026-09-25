"""PageDriver interface: the only surface the engine uses to touch a browser.

Actions, runner and pagination depend on this Protocol, never on Playwright.
Tests inject FakePageDriver; production uses PlaywrightDriver (added in Task 1.12,
the only code that may import playwright).
"""

from collections.abc import Sequence
from typing import Protocol, TypedDict


class DriverError(Exception):
    """Base class for every failure a driver reports."""


class DriverTimeoutError(DriverError):
    """A wait or action exceeded its time limit."""


class ElementNotFoundError(DriverError):
    """A selector matched nothing on the page."""


class ElementState(TypedDict):
    visible: bool
    disabled: bool


class PageDriver(Protocol):
    def goto(self, url: str) -> None: ...

    def current_url(self) -> str: ...

    def fill(self, selector: str, value: str) -> None: ...

    def click(self, selector: str) -> None: ...

    def select_option(self, selector: str, value: str) -> None: ...

    def check(self, selector: str) -> None: ...

    def upload(self, selector: str, paths: Sequence[str]) -> None: ...

    def screenshot(self, path: str, selector: str | None = None, full_page: bool = True) -> None:
        """Save a screenshot. With `selector`, capture only that element."""
        ...

    def wait_for_download(self) -> str:
        """Block until a download triggered earlier finishes; return its local path."""
        ...

    def get_table_rows(self, selector: str) -> list[list[str]]:
        """Return the table at `selector` as rows of cell text (header row included)."""
        ...

    def wait_for_selector(self, selector: str, timeout_ms: int = 30_000) -> None: ...

    def get_element_state(self, selector: str) -> ElementState | None:
        """Return visibility/disabled flags, or None if the element is absent."""
        ...
