from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser

from browserforge.engine.driver import PlaywrightDriver
from fixtures.local_site import run_local_site

# Short so "element never appears" tests fail fast; local pages respond in milliseconds.
TEST_TIMEOUT_MS = 2_000


@pytest.fixture(scope="session")
def site() -> Iterator[str]:
    """Base URL of the local test website."""
    with run_local_site() as base_url:
        yield base_url


@pytest.fixture
def driver(browser: Browser) -> Iterator[PlaywrightDriver]:
    """A real PlaywrightDriver on a fresh context (`browser` is a pytest-playwright fixture)."""
    context = browser.new_context(accept_downloads=True)
    playwright_driver = PlaywrightDriver(context.new_page(), timeout_ms=TEST_TIMEOUT_MS)
    try:
        yield playwright_driver
    finally:
        playwright_driver.close()
        context.close()
