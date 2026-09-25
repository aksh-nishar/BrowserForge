"""Failures as data: a machine-readable kind (drives retry decisions) and a short
plain-language message (what the dashboard shows). The raw technical text is kept
separately in run_log.json for debugging.
"""

import re
from typing import Literal

from browserforge.engine.driver import DriverError, DriverTimeoutError, ElementNotFoundError

ErrorKind = Literal[
    "timeout",  # an element never appeared / could not be used in time
    "not_found",  # a selector matched nothing
    "navigation",  # the page could not be opened
    "url_wait",  # the page address never became what was expected
    "download",  # no download started, or it failed
    "login",  # the login step failed (never retried: it could lock an account)
    "driver",  # any other browser problem
    "unexpected",  # not a browser problem at all: a bug
]

# Worth trying again: the site may just have been slow. Everything else fails the same way
# every time (bad recipe config, wrong password, a bug), and retrying could do harm.
RETRYABLE_KINDS: frozenset[str] = frozenset(
    {"timeout", "not_found", "navigation", "download", "driver"}
)

_MS = re.compile(r"after (\d+)ms")
_TARGET = re.compile(r"waiting for (.+)$")
_URL_FRAGMENT = re.compile(r"URL containing (.+)$")
_STEP_PREFIX = re.compile(r"^Step '[^']*': ")
_QUOTED = re.compile(r"'([^']*)'")


def classify(exc: BaseException) -> ErrorKind:
    """Map an exception to an ErrorKind."""
    message = str(exc)
    if isinstance(exc, ElementNotFoundError):
        return "not_found"
    if isinstance(exc, DriverTimeoutError):
        if message.endswith("to load"):
            return "navigation"
        if "URL containing" in message:
            return "url_wait"
        if "No download started" in message or "the download to finish" in message:
            return "download"
        return "timeout"
    if isinstance(exc, DriverError):
        return "navigation" if "net::ERR_" in message else "driver"
    return "unexpected"


_ANY_MS = re.compile(r"(\d+)ms\b")
_LOGIN_PREFIX = re.compile(r"^Login failed:\s*")


def _format_seconds(ms: int) -> str:
    seconds = ms / 1000
    return f"{seconds:g} second{'' if seconds == 1 else 's'}"


def _seconds(raw: str) -> str:
    match = _MS.search(raw)
    return _format_seconds(int(match.group(1))) if match else "the time limit"


def _target(raw: str) -> str:
    match = _TARGET.search(raw)
    return match.group(1) if match else "the element"


def humanize(kind: ErrorKind | None, raw: str) -> str:
    """A short, plain-language explanation for a failure. Never contains a stack trace."""
    detail = _STEP_PREFIX.sub("", raw).strip()
    if kind is None:
        return detail  # config and login errors are already written for people

    if kind == "timeout":
        return (
            f"Couldn't find or use {_target(raw)} within {_seconds(raw)}. "
            "Check the selector in the recipe, or whether the page has changed."
        )
    if kind == "not_found":
        quoted = _QUOTED.search(detail)  # after the "Step 'x':" prefix, or it would match that
        what = f"'{quoted.group(1)}'" if quoted else "the element"
        return f"Couldn't find {what} on the page. Check the selector in the recipe."
    if kind == "navigation":
        if "ERR_NAME_NOT_RESOLVED" in raw:
            return "The website address could not be found. Check the URL in the recipe."
        if "ERR_CONNECTION_REFUSED" in raw:
            return "The website refused the connection. It may be down, or the address is wrong."
        if "ERR_INTERNET_DISCONNECTED" in raw:
            return "There is no internet connection."
        return (
            "Could not open the page. The site may be down or slow, the address may be wrong, "
            "or there is no internet connection."
        )
    if kind == "url_wait":
        fragment = _URL_FRAGMENT.search(raw)
        expected = f" ({fragment.group(1)})" if fragment else ""
        return (
            f"The page address never changed to what was expected{expected}. "
            "The previous action may not have worked."
        )
    if kind == "download":
        return "Nothing was downloaded after the click, or the download failed."
    if kind == "login":
        # The dashboard already says "Step 2 (login) failed:", so drop the repeated "Login
        # failed:" and show times in seconds.
        text = _ANY_MS.sub(
            lambda m: _format_seconds(int(m.group(1))), _LOGIN_PREFIX.sub("", detail)
        )
        return text[:1].upper() + text[1:]
    if kind == "driver":
        return f"The browser reported a problem: {detail}"
    return "Something unexpected went wrong. The details are saved in the run log."
