"""Pagination: a pure "is there a next page?" decision plus a capped collection loop."""

from collections.abc import Mapping
from dataclasses import dataclass

from browserforge.engine.driver import PageDriver

DEFAULT_MAX_PAGES = 50


def has_next_page(next_button_state: Mapping[str, object] | None) -> bool:
    """True only if the next button exists, is visible and is not disabled.

    Flags missing from the mapping default to visible=True, disabled=False.
    """
    if next_button_state is None:
        return False
    return bool(next_button_state.get("visible", True)) and not next_button_state.get(
        "disabled", False
    )


@dataclass(frozen=True)
class Collected:
    rows: list[list[str]]
    pages: int
    hit_cap: bool


def paginate_and_collect(
    driver: PageDriver,
    table_selector: str,
    next_selector: str,
    max_pages: int = DEFAULT_MAX_PAGES,
    has_header: bool = True,
) -> Collected:
    """Read the table on each page, clicking "next" until it is gone or `max_pages` is hit.

    Returns raw rows in page order. With `has_header`, the header row is kept once
    (from page 1); the repeated header on later pages is dropped. `hit_cap` is True
    when more pages were still available after `max_pages` were read.
    """
    all_rows: list[list[str]] = []
    for page in range(1, max_pages + 1):
        page_rows = driver.get_table_rows(table_selector)
        all_rows.extend(page_rows[1:] if has_header and page > 1 else page_rows)

        if not has_next_page(driver.get_element_state(next_selector)):
            return Collected(rows=all_rows, pages=page, hit_cap=False)
        if page == max_pages:
            return Collected(rows=all_rows, pages=page, hit_cap=True)
        driver.click(next_selector)

    return Collected(rows=all_rows, pages=0, hit_cap=False)  # max_pages < 1: nothing read
