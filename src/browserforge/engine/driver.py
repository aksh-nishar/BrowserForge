"""PageDriver interface: the only surface the engine uses to touch a browser.

Actions, runner and pagination depend on the PageDriver Protocol, never on Playwright.
Tests inject FakePageDriver; production uses PlaywrightDriver below. This is the ONLY
module that imports playwright.
"""

import shutil
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, TypedDict

from playwright.sync_api import Download, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


class DriverError(Exception):
    """Base class for every failure a driver reports."""


class DriverTimeoutError(DriverError):
    """A wait or action exceeded its time limit."""


class ElementNotFoundError(DriverError):
    """A selector matched nothing on the page."""


class ElementState(TypedDict):
    visible: bool
    disabled: bool


class FormOption(TypedDict):
    value: str
    text: str


class RawField(TypedDict):
    """One form control as the page shows it (what `scan_form` reports, before any planning)."""

    tag: str  # input | select | textarea | button
    type: str  # text, email, checkbox, radio, file, submit, select, textarea, ...
    id: str
    name: str
    label: str  # the visible label text for this control
    group_label: str  # for radio buttons: the fieldset legend, if there is one
    placeholder: str
    value: str  # the value attribute (radio/checkbox value, or a button's caption)
    options: list[FormOption]
    required: bool
    disabled: bool
    visible: bool
    id_count: int  # how many elements on the page share this id
    name_count: int  # how many elements on the page share this name


class FormScan(TypedDict):
    fields: list[RawField]
    notes: list[str]  # things on the page that BrowserForge cannot handle (iframe, captcha...)


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

    def wait_for_url_contains(self, fragment: str, timeout_ms: int = 30_000) -> None:
        """Block until the page URL contains `fragment`, else raise DriverTimeoutError."""
        ...

    def get_element_state(self, selector: str) -> ElementState | None:
        """Return visibility/disabled flags, or None if the element is absent."""
        ...

    def scan_form(self, url: str) -> FormScan:
        """Open `url` and report every form control on it (for generating a recipe)."""
        ...


# ---------------------------------------------------------------------------
# Real implementation
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_MS = 30_000
_DOWNLOAD_POLL_MS = 50
_SETTLE_MS = 3_000

# Reads every input/select/textarea/button on the page. Runs in the page; returns plain data.
_SCAN_JS = """
() => {
  const clean = (text) => (text || '').replace(/\\s+/g, ' ').trim();
  const quote = (text) => '"' + text.replace(/["\\\\]/g, '\\\\$&') + '"';
  const isVisible = (el) => {
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && (box.width > 0 || box.height > 0);
  };
  const labelOf = (el) => {
    if (el.id) {
      const linked = document.querySelector('label[for=' + quote(el.id) + ']');
      if (linked) return clean(linked.innerText);
    }
    const wrapping = el.closest('label');
    if (wrapping) return clean(wrapping.innerText);
    return clean(el.getAttribute('aria-label'));
  };
  const count = (attribute, value) =>
    value ? document.querySelectorAll('[' + attribute + '=' + quote(value) + ']').length : 0;

  const fields = [];
  for (const el of document.querySelectorAll('input, select, textarea, button')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    const legend = el.closest('fieldset') ? el.closest('fieldset').querySelector('legend') : null;
    const isButton = tag === 'button' || type === 'submit' || type === 'button';
    fields.push({
      tag, type,
      id: el.id || '',
      name: el.getAttribute('name') || '',
      label: isButton ? clean(el.innerText || el.value) : labelOf(el),
      group_label: legend ? clean(legend.innerText) : '',
      placeholder: el.getAttribute('placeholder') || '',
      value: el.getAttribute('value') || '',
      options: tag === 'select'
        ? Array.from(el.options).map((o) => ({ value: o.value, text: clean(o.text) }))
        : [],
      required: Boolean(el.required),
      disabled: Boolean(el.disabled),
      visible: type === 'hidden' ? false : isVisible(el),
      id_count: count('id', el.id),
      name_count: count('name', el.getAttribute('name') || ''),
    });
  }

  const notes = [];
  if (document.querySelector('iframe')) notes.push('iframe');
  if (document.querySelector('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]')) notes.push('captcha');
  if (Array.from(document.querySelectorAll('*')).some((el) => el.shadowRoot)) notes.push('shadow');
  if (document.querySelector('input[type=password]')) notes.push('password');
  return { fields, notes };
}
"""  # noqa: E501 - JavaScript, kept readable rather than wrapped

# Rows of cell text for the first <table>-like element: every <tr>, its <th>/<td> cells.
_TABLE_ROWS_JS = """
el => Array.from(el.querySelectorAll('tr')).map(tr =>
    Array.from(tr.children)
        .filter(cell => cell.tagName === 'TH' || cell.tagName === 'TD')
        .map(cell => cell.innerText.trim()))
"""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "Browser error"


def translate_playwright_error(exc: PlaywrightError, what: str, timeout_ms: int) -> DriverError:
    """Map a Playwright exception to the engine's own error types (pure, no browser needed).

    `what` names the thing being waited for, e.g. "'#login'" or "page to load".
    """
    if isinstance(exc, PlaywrightTimeoutError):
        return DriverTimeoutError(f"Timed out after {timeout_ms}ms waiting for {what}")
    return DriverError(_first_line(exc.message))


class PlaywrightDriver:
    """PageDriver backed by a Playwright sync Page.

    Downloads are captured by a listener registered at construction, so a download
    that starts right after a click is never missed; wait_for_download() then saves it
    under its original file name and returns that path.
    """

    def __init__(self, page: Page, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self.page = page
        self._timeout_ms = timeout_ms
        self._pending_downloads: list[Download] = []
        self._downloads_dir: Path | None = None
        page.set_default_timeout(timeout_ms)
        page.on("download", self._on_download)

    def _on_download(self, download: Download) -> None:
        # A named method, not list.append: Playwright cannot wrap builtin methods as listeners.
        self._pending_downloads.append(download)

    @contextmanager
    def _translated(self, what: str, timeout_ms: int | None = None) -> Iterator[None]:
        try:
            yield
        except PlaywrightError as exc:
            raise translate_playwright_error(exc, what, timeout_ms or self._timeout_ms) from exc

    def goto(self, url: str) -> None:
        with self._translated(f"{url} to load"):
            self.page.goto(url)

    def current_url(self) -> str:
        return self.page.url

    def fill(self, selector: str, value: str) -> None:
        with self._translated(f"'{selector}'"):
            self.page.fill(selector, value)

    def click(self, selector: str) -> None:
        with self._translated(f"'{selector}'"):
            self.page.click(selector)
            # click() can return while a navigation it started is still streaming in, leaving
            # a half-parsed page (a short table, no "Next" link yet). Wait for the document to
            # finish parsing so whatever runs next sees the whole page. No-op when the click
            # did not navigate.
            self.page.wait_for_load_state("domcontentloaded")

    def select_option(self, selector: str, value: str) -> None:
        with self._translated(f"'{selector}'"):
            self.page.select_option(selector, value)

    def check(self, selector: str) -> None:
        with self._translated(f"'{selector}'"):
            self.page.check(selector)

    def upload(self, selector: str, paths: Sequence[str]) -> None:
        with self._translated(f"'{selector}'"):
            self.page.set_input_files(selector, list(paths))

    def screenshot(self, path: str, selector: str | None = None, full_page: bool = True) -> None:
        with self._translated(f"'{selector}'" if selector else "the page to be ready"):
            if selector:
                self.page.locator(selector).first.screenshot(path=path)
            else:
                self.page.screenshot(path=path, full_page=full_page)

    def wait_for_download(self) -> str:
        deadline = time.monotonic() + self._timeout_ms / 1000
        while not self._pending_downloads:
            if time.monotonic() >= deadline:
                raise DriverTimeoutError(f"No download started within {self._timeout_ms}ms")
            self.page.wait_for_timeout(_DOWNLOAD_POLL_MS)  # lets Playwright deliver events

        download = self._pending_downloads.pop(0)
        with self._translated("the download to finish"):
            failure = download.failure()
            if failure:
                raise DriverError(f"Download failed: {failure}")
            if self._downloads_dir is None:
                self._downloads_dir = Path(tempfile.mkdtemp(prefix="browserforge-dl-"))
            target_dir = Path(tempfile.mkdtemp(dir=self._downloads_dir))
            target = target_dir / (Path(download.suggested_filename).name or "download")
            download.save_as(target)
        return str(target)

    def get_table_rows(self, selector: str) -> list[list[str]]:
        with self._translated(f"table '{selector}'"):
            rows = self.page.locator(selector).first.evaluate(_TABLE_ROWS_JS)
        return [[str(cell) for cell in row] for row in rows]

    def wait_for_selector(self, selector: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        with self._translated(f"'{selector}'", timeout_ms):
            self.page.wait_for_selector(selector, timeout=timeout_ms)

    def wait_for_url_contains(self, fragment: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        with self._translated(f"URL containing '{fragment}'", timeout_ms):
            self.page.wait_for_url(lambda url: fragment in url, timeout=timeout_ms)

    def get_element_state(self, selector: str) -> ElementState | None:
        with self._translated(f"'{selector}'"):
            locator = self.page.locator(selector)
            if locator.count() == 0:
                return None
            first = locator.first
            return {"visible": first.is_visible(), "disabled": first.is_disabled()}

    def scan_form(self, url: str) -> FormScan:
        self.goto(url)
        try:  # pages that build their form with JavaScript need a moment; never wait for long
            self.page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
        except PlaywrightError:
            pass
        with self._translated("the page to be read"):
            scan: FormScan = self.page.evaluate(_SCAN_JS)
        return scan

    def close(self) -> None:
        """Remove temporary download files. The page/browser are owned by the caller."""
        if self._downloads_dir is not None:
            shutil.rmtree(self._downloads_dir, ignore_errors=True)
            self._downloads_dir = None


@contextmanager
def playwright_driver(
    *, headless: bool = True, timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> Iterator[PlaywrightDriver]:
    """Launch Chromium, yield a ready PlaywrightDriver, and clean everything up afterwards."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            context = browser.new_context(accept_downloads=True)
            driver = PlaywrightDriver(context.new_page(), timeout_ms=timeout_ms)
            try:
                yield driver
            finally:
                driver.close()
        finally:
            browser.close()
