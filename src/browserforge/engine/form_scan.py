"""Form scan -> recipe. Pure: works on plain data (what `PageDriver.scan_form` reports).

`plan_form` decides which controls of a page can be filled and how each one is reached with a
selector that matches exactly one element (a control without a stable id or name is skipped
rather than guessed). `fields_to_recipe` turns the user's confirmed choices into a recipe whose
values are `{{placeholders}}`, so the same recipe runs for one person or for a whole sheet.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

import yaml

from browserforge.engine.actions import css_string
from browserforge.engine.data_rows import is_secret_name
from browserforge.engine.driver import FormScan, RawField
from browserforge.engine.recipe import RecipeValidationError, parse_recipe_text

MAX_KEY_LENGTH = 40
MAX_SELECTOR_LENGTH = 500
MAX_URL_LENGTH = 2000
KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

TEXT_TYPES = frozenset(
    {
        "text",
        "email",
        "tel",
        "url",
        "number",
        "date",
        "time",
        "datetime-local",
        "month",
        "week",
        "search",
        "password",
        "textarea",
    }
)
_IGNORED_TYPES = frozenset({"hidden", "reset", "image"})
_BUTTON_TYPES = frozenset({"submit", "button"})
# Actions that take a `{{placeholder}}` value, and the one that does not.
VALUE_ACTIONS = frozenset({"fill", "select_option", "choose", "upload"})
ACTIONS = VALUE_ACTIONS | {"check"}
_SUBMIT_WORDS = re.compile(
    r"submit|send|save|register|sign ?up|apply|next|continue|book|confirm|create|update|finish",
    re.IGNORECASE,
)

NOTES = {
    "iframe": (
        "This page has embedded frames (iframes). Fields inside them are not detected, so the "
        "recipe may be missing some."
    ),
    "captcha": (
        "This page has a CAPTCHA. BrowserForge cannot solve it: a run will need a person "
        "to step in, or the site may block automation."
    ),
    "shadow": (
        "This page hides some parts of the form in shadow DOM. Fields inside are not detected."
    ),
    "password": (
        "This page has a password field, so it may be a login form. For logging in, a recipe's "
        "`login:` block is the better tool; a password typed here is a {{placeholder}} you "
        "supply (or use {{env:NAME}} for a stored secret)."
    ),
}


class PlanError(Exception):
    """The chosen fields cannot make a recipe. The message is for people."""


@dataclass(frozen=True)
class PlannedField:
    key: str  # the {{placeholder}} name ("" for a checkbox, which takes no value)
    label: str  # what the page calls it
    action: str  # fill | select_option | choose | upload | check
    selector: str
    kind: str = ""  # text, email, select, radio, ...
    options: tuple[str, ...] = ()  # choices of a dropdown or radio group
    required: bool = False
    secret: bool = False  # should be typed into a masked box

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = list(self.options)
        return data


@dataclass(frozen=True)
class SubmitCandidate:
    selector: str
    label: str


@dataclass(frozen=True)
class FormPlan:
    fields: list[PlannedField]
    submit_candidates: list[SubmitCandidate]
    skipped: list[str]  # controls left out, each with the reason
    notes: list[str]  # page-level warnings

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": [f.to_dict() for f in self.fields],
            "submit_candidates": [asdict(c) for c in self.submit_candidates],
            "skipped": self.skipped,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Names and selectors
# ---------------------------------------------------------------------------


def normalize_label(text: str) -> str:
    """A placeholder name from a label: 'E-mail Address *' -> 'e_mail_address'; '' if nothing."""
    plain = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    words = re.sub(r"[^a-z0-9]+", "_", plain.lower()).strip("_")
    if not words:
        return ""
    if words[0].isdigit():
        words = f"field_{words}"
    return words[:MAX_KEY_LENGTH].rstrip("_")


def _is_identifier(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value) is not None


def build_selector(field: RawField) -> str | None:
    """A selector that matches exactly this control, or None if the page gives no stable handle.

    Prefers the id, then the name; an id or name shared with another element is not used.
    A selector containing `{{` is refused so page text can never act as a placeholder.
    """
    selector: str | None = None
    if field["id"] and field["id_count"] == 1:
        ident = field["id"]
        selector = f"#{ident}" if _is_identifier(ident) else f"[id={css_string(ident)}]"
    elif field["name"] and field["name_count"] == 1:
        selector = f"{field['tag']}[name={css_string(field['name'])}]"
    if selector is None or "{{" in selector or len(selector) > MAX_SELECTOR_LENGTH:
        return None
    return selector


def _display(field: RawField) -> str:
    return (
        field["label"]
        or field["placeholder"]
        or field["name"]
        or field["id"]
        or f"a {field['type']} control"
    )


def _base_key(field: RawField) -> str:
    for text in (field["label"], field["placeholder"], field["name"], field["id"]):
        key = normalize_label(text)
        if key:
            return key
    return ""


def _unique_key(base: str, taken: set[str], position: int) -> str:
    root = base or f"field_{position}"
    candidate = root
    counter = 2
    while candidate in taken:
        candidate = f"{root}_{counter}"
        counter += 1
    taken.add(candidate)
    return candidate


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _button_candidate(field: RawField) -> tuple[int, SubmitCandidate] | None:
    label = field["label"] or field["value"]
    selector = build_selector(field)
    if selector is None and field["tag"] == "button" and label and "{{" not in label:
        selector = f"button:text-is({css_string(label)})"
    if (
        selector is None
        and field["type"] == "submit"
        and field["value"]
        and "{{" not in field["value"]
    ):
        selector = f"input[type=submit][value={css_string(field['value'])}]"
    if selector is None or len(selector) > MAX_SELECTOR_LENGTH:
        return None
    if field["type"] == "submit":
        rank = 0
    elif _SUBMIT_WORDS.search(label):
        rank = 1
    else:
        rank = 2
    return rank, SubmitCandidate(selector=selector, label=label or "Button")


def _radio_groups(fields: Sequence[RawField]) -> dict[str, list[RawField]]:
    groups: dict[str, list[RawField]] = {}
    for field in fields:
        if (
            field["type"] == "radio"
            and field["visible"]
            and not field["disabled"]
            and field["name"]
        ):
            groups.setdefault(field["name"], []).append(field)
    return groups


def plan_form(scan: FormScan) -> FormPlan:
    """Which controls can be filled, with their selectors, plus submit-button candidates."""
    raw = scan["fields"]
    radio_groups = _radio_groups(raw)
    drafts: list[tuple[RawField, str, str, str, tuple[str, ...]]] = []
    ranked_buttons: list[tuple[int, int, SubmitCandidate]] = []
    skipped: list[str] = []
    seen_radio: set[str] = set()
    seen_selectors: set[str] = set()

    for order, field in enumerate(raw):
        if field["type"] in _IGNORED_TYPES or not field["visible"] or field["disabled"]:
            continue
        shown = _display(field)

        if field["tag"] == "button" or field["type"] in _BUTTON_TYPES:
            candidate = _button_candidate(field)
            if candidate is not None:
                ranked_buttons.append((candidate[0], order, candidate[1]))
            continue

        if field["type"] == "radio":
            name = field["name"]
            if not name:
                skipped.append(f"'{shown}': a radio button with no name cannot be grouped.")
                continue
            if name in seen_radio:
                continue
            seen_radio.add(name)
            values = tuple(dict.fromkeys(r["value"] for r in radio_groups[name] if r["value"]))
            if not values:
                skipped.append(f"'{name}': its radio buttons have no values to choose between.")
                continue
            group_selector = f"input[type=radio][name={css_string(name)}]"
            if "{{" in group_selector:
                skipped.append(f"'{name}': its name contains '{{{{', which is reserved.")
                continue
            group: RawField = {
                **radio_groups[name][0],
                "label": field["group_label"] or name.replace("_", " "),
            }
            drafts.append((group, "choose", "radio", group_selector, values))
            continue

        selector = build_selector(field)
        if selector is None and field["type"] == "checkbox" and field["name"] and field["value"]:
            candidate_selector = (
                f"input[type=checkbox][name={css_string(field['name'])}]"
                f"[value={css_string(field['value'])}]"
            )
            if "{{" not in candidate_selector:
                selector = candidate_selector
        if selector is None:
            skipped.append(f"'{shown}': it has no unique id or name to find it by.")
            continue
        if selector in seen_selectors:
            skipped.append(f"'{shown}': another control already uses the same selector.")
            continue
        seen_selectors.add(selector)

        if field["type"] == "checkbox":
            drafts.append((field, "check", "checkbox", selector, ()))
        elif field["type"] == "file":
            drafts.append((field, "upload", "file", selector, ()))
        elif field["tag"] == "select":
            options = tuple(o["value"] for o in field["options"] if o["value"] != "")
            if not options:
                skipped.append(f"'{shown}': the dropdown has no options to choose from.")
                continue
            drafts.append((field, "select_option", "select", selector, options))
        elif field["tag"] == "textarea":
            drafts.append((field, "fill", "textarea", selector, ()))
        elif field["type"] in TEXT_TYPES:
            drafts.append((field, "fill", field["type"], selector, ()))
        else:
            skipped.append(f"'{shown}': fields of type '{field['type']}' are not supported.")

    planned: list[PlannedField] = []
    taken: set[str] = set()
    for position, (field, action, kind, selector, options) in enumerate(drafts, start=1):
        base = _base_key(field)
        key = _unique_key(base, taken, position) if action in VALUE_ACTIONS else base
        planned.append(
            PlannedField(
                key=key if action in VALUE_ACTIONS else "",
                label=field["label"] or key or _display(field),
                action=action,
                selector=selector,
                kind=kind,
                options=options,
                required=field["required"],
                secret=action in VALUE_ACTIONS
                and (kind == "password" or is_secret_name(key or base)),
            )
        )

    seen_button_selectors: set[str] = set()
    candidates: list[SubmitCandidate] = []
    for _rank, _order, button in sorted(ranked_buttons, key=lambda item: item[:2]):
        if button.selector not in seen_button_selectors:
            seen_button_selectors.add(button.selector)
            candidates.append(button)

    return FormPlan(
        fields=planned,
        submit_candidates=candidates,
        skipped=skipped,
        notes=[NOTES[note] for note in scan["notes"] if note in NOTES],
    )


# ---------------------------------------------------------------------------
# Building the recipe
# ---------------------------------------------------------------------------


_BARE_ADDRESS = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*(:\d+)?([/?#].*)?")


def normalize_address(text: str) -> str:
    """What a person typed, with `https://` added if it looks like a bare address (x.io/form).

    Anything else (an existing scheme, spaces, `javascript:...`) is left as it is for
    `check_url` to judge.
    """
    cleaned = text.strip()
    if "://" not in cleaned and _BARE_ADDRESS.fullmatch(cleaned):
        return "https://" + cleaned
    return cleaned


def check_url(url: str) -> str:
    """The URL if it is a plain http(s) address, else PlanError (no file:, javascript:, ...)."""
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PlanError("Enter a web address starting with http:// or https://")
    if len(cleaned) > MAX_URL_LENGTH or re.search(r"\s", cleaned):
        raise PlanError("That web address is too long or contains spaces.")
    return cleaned


def _check_selector(selector: str, what: str) -> None:
    if not selector.strip():
        raise PlanError(f"{what} has no selector.")
    if len(selector) > MAX_SELECTOR_LENGTH:
        raise PlanError(f"{what} has a selector that is too long.")
    if "{{" in selector:
        raise PlanError(f"{what} has a selector containing '{{{{', which is reserved.")


def _step_name(field: PlannedField) -> str:
    if field.key:
        return field.key
    return "tick_" + (normalize_label(field.label) or "checkbox")


def _step_for(field: PlannedField) -> dict[str, Any]:
    step: dict[str, Any] = {
        "type": field.action,
        "name": _step_name(field),
        "selector": field.selector,
    }
    placeholder = "{{" + field.key + "}}"
    if field.action in ("fill", "select_option", "choose"):
        step["value"] = placeholder
    elif field.action == "upload":
        step["files"] = placeholder
    return step


def fields_to_recipe(
    url: str, fields: Sequence[PlannedField], submit_selector: str | None
) -> dict[str, Any]:
    """The recipe (as a dict) for the confirmed fields. Raises PlanError with a plain reason."""
    address = check_url(url)
    if not fields:
        raise PlanError("Pick at least one field to fill in.")

    seen_keys: set[str] = set()
    steps: list[dict[str, Any]] = []
    for field in fields:
        shown = field.label or field.key or field.selector
        if field.action not in ACTIONS:
            raise PlanError(f"'{shown}' has an unknown action '{field.action}'.")
        _check_selector(field.selector, f"'{shown}'")
        if field.action in VALUE_ACTIONS:
            if not KEY_PATTERN.fullmatch(field.key):
                raise PlanError(
                    f"'{shown}' needs a name made of letters, digits and underscores "
                    "(it cannot start with a digit)."
                )
            if field.key in seen_keys:
                raise PlanError(f"The name '{field.key}' is used for two fields. Rename one.")
            seen_keys.add(field.key)
        steps.append(_step_for(field))

    steps.append({"type": "screenshot", "name": "before_submit"})
    if submit_selector:
        _check_selector(submit_selector, "The submit button")
        steps.append(
            {"type": "click", "name": "submit", "selector": submit_selector, "submit": True}
        )
        steps.append({"type": "screenshot", "name": "after_submit"})
    return {"url": address, "retries": 1, "backoff_seconds": 1, "steps": steps}


def recipe_to_yaml(recipe: dict[str, Any]) -> str:
    """YAML text for a generated recipe, checked with the same parser that will run it."""
    source = re.sub(r"\s", "", str(recipe["url"]))
    header = (
        f"# Generated by BrowserForge from {source}\n"
        "# Each {{name}} is filled in when the recipe runs (typed values, or one row of a sheet).\n"
        "# The step marked `submit: true` is skipped in a test run.\n"
    )
    text = header + yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True)
    try:
        parse_recipe_text(text, "generated recipe")
    except RecipeValidationError as exc:
        raise PlanError(f"The generated recipe is not valid: {exc}") from exc
    return text
