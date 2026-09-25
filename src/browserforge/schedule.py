"""Scheduled runs: the plain-language schedule parser and the decision of what to do when one fires.

Pure logic (no browser, no clock of its own, no files) so it can be tested with a fake clock.
`ScheduleRunner.fire` decides between starting a job and skipping it (paused, or the previous
run of the same schedule is still going); the APScheduler wiring lives in `browserforge.scheduler`.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from apscheduler.triggers.cron import CronTrigger
from tzlocal import get_localzone

logger = structlog.get_logger(__name__)

LOCAL_TIMEZONE = "local"
MIN_MINUTES = 5

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME = re.compile(r"(\d{1,2}):(\d{2})")
_EVERY = re.compile(r"every (\d+) (minute|minutes|hour|hours)")
_DAY_FIELD = re.compile(r"\*|[a-z]{3}([-,][a-z]{3})*")


class ScheduleValidationError(Exception):
    """The schedule text or timezone is not usable. The message is for people."""


@dataclass(frozen=True)
class Schedule:
    id: str
    recipe_name: str
    when: str  # what the user typed, shown back to them ("daily 08:00")
    cron: str  # the same thing as a 5-field cron expression
    timezone: str = LOCAL_TIMEZONE
    enabled: bool = True
    data_file: str | None = None  # name of the saved data sheet, for recipes with placeholders
    mapping: dict[str, str] | None = None
    created_at: str = ""
    last_run_at: str | None = None
    last_job_id: str | None = None
    last_error: str | None = None  # why the last firing could not start a job

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Schedule":
        """Rebuild from saved JSON; unknown keys are ignored so old files keep loading."""
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


def new_schedule_id() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _clock_time(text: str) -> tuple[int, int]:
    match = _TIME.fullmatch(text)
    if match is None:
        raise ScheduleValidationError(f"'{text}' is not a time. Use HH:MM, for example 08:30.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ScheduleValidationError(f"'{text}' is not a valid time of day (00:00 to 23:59).")
    return hour, minute


def _check_day_field(text: str) -> None:
    if not _DAY_FIELD.fullmatch(text):
        raise ScheduleValidationError(
            "In a cron schedule, write the day of the week as names (mon, tue, wed...), "
            "for example 'mon-fri', or * for every day. Numbers are avoided because tools "
            "disagree on whether the week starts on Sunday or Monday."
        )
    unknown = [name for name in re.findall(r"[a-z]{3}", text) if name not in _DAYS]
    if unknown:
        raise ScheduleValidationError(f"'{unknown[0]}' is not a day name. Use mon, tue, wed...")


def parse_schedule(text: str) -> str:
    """A 5-field cron expression from plain words or from cron itself.

    Accepted: `hourly`, `daily 08:00`, `weekly mon 09:30`, `every 15 minutes` (5 minutes or
    more, dividing an hour evenly), `every 2 hours` (dividing a day evenly), or a cron
    expression `minute hour day-of-month month day-of-week` with day names.
    """
    cleaned = " ".join(text.lower().split())
    if not cleaned:
        raise ScheduleValidationError("Say when to run, for example 'daily 08:00'.")

    if cleaned == "hourly":
        return "0 * * * *"

    parts = cleaned.split(" ")
    if parts[0] == "daily" and len(parts) == 2:
        hour, minute = _clock_time(parts[1])
        return f"{minute} {hour} * * *"
    if parts[0] == "weekly" and len(parts) == 3:
        if parts[1] not in _DAYS:
            raise ScheduleValidationError(
                f"'{parts[1]}' is not a day name. Use mon, tue, wed, thu, fri, sat or sun."
            )
        hour, minute = _clock_time(parts[2])
        return f"{minute} {hour} * * {parts[1]}"

    every = _EVERY.fullmatch(cleaned)
    if every is not None:
        count, unit = int(every.group(1)), every.group(2)
        if unit.startswith("minute"):
            if count < MIN_MINUTES or count >= 60 or 60 % count:
                raise ScheduleValidationError(
                    f"Use at least {MIN_MINUTES} minutes, in steps that divide an hour evenly "
                    "(5, 6, 10, 12, 15, 20, 30). For every hour, write 'hourly'."
                )
            return f"*/{count} * * * *"
        if not 1 <= count <= 12 or 24 % count:
            raise ScheduleValidationError(
                "Use hours that divide a day evenly (1, 2, 3, 4, 6, 8, 12)."
            )
        return f"0 */{count} * * *"

    fields = cleaned.split(" ")
    if len(fields) != 5:
        raise ScheduleValidationError(
            "Not understood. Try 'daily 08:00', 'weekly mon 09:00', 'hourly', "
            "'every 15 minutes', or a 5-field cron expression."
        )
    _check_day_field(fields[4])
    try:
        CronTrigger.from_crontab(cleaned, timezone=ZoneInfo("UTC"))
    except ValueError as exc:
        raise ScheduleValidationError(f"Not a valid cron expression: {exc}") from exc
    return cleaned


def resolve_timezone(name: str) -> tzinfo:
    """`local` (this computer's timezone) or an IANA name such as `Asia/Kolkata`."""
    if name == LOCAL_TIMEZONE:
        return get_localzone()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleValidationError(
            f"'{name}' is not a timezone name. Use local, or a name like Asia/Kolkata or UTC."
        ) from None


def make_trigger(cron: str, timezone: str) -> CronTrigger:
    try:
        return CronTrigger.from_crontab(cron, timezone=resolve_timezone(timezone))
    except ValueError as exc:
        raise ScheduleValidationError(f"Not a valid cron expression: {exc}") from exc


def next_run_times(cron: str, timezone: str, after: datetime, count: int = 3) -> list[datetime]:
    """The next `count` times a schedule fires after `after` (a timezone-aware datetime)."""
    trigger = make_trigger(cron, timezone)
    times: list[datetime] = []
    previous: datetime | None = None
    moment = after
    for _ in range(count):
        upcoming = trigger.get_next_fire_time(previous, moment)
        if upcoming is None:
            break
        times.append(upcoming)
        previous, moment = upcoming, upcoming
    return times


# ---------------------------------------------------------------------------
# What to do when a schedule fires
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FireResult:
    outcome: str  # started | missing | paused | overlap | error
    job_id: str | None = None
    detail: str | None = None


# `starter(schedule)` creates the job and returns (job_id, run): calling run() executes it.
Starter = Callable[[Schedule], tuple[str, Callable[[], None]]]


class ScheduleRunner:
    """Called by the scheduler each time a schedule's time comes.

    Skips (and remembers why) instead of piling up: a paused schedule does nothing, and a
    schedule whose previous run is still going is skipped rather than started twice.
    """

    def __init__(
        self,
        get: Callable[[str], Schedule | None],
        update: Callable[..., Schedule | None],
        starter: Starter,
        is_active: Callable[[str], bool],
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._get = get
        self._update = update
        self._starter = starter
        self._is_active = is_active
        self._clock = clock

    def fire(self, schedule_id: str) -> FireResult:
        schedule = self._get(schedule_id)
        if schedule is None:
            logger.warning("schedule_missing", schedule=schedule_id)
            return FireResult("missing")
        if not schedule.enabled:
            logger.info("schedule_paused_skip", schedule=schedule_id)
            return FireResult("paused")
        if schedule.last_job_id and self._is_active(schedule.last_job_id):
            logger.warning("schedule_overlap_skip", schedule=schedule_id, job=schedule.last_job_id)
            self._update(schedule_id, last_error="Skipped: the previous run was still going.")
            return FireResult("overlap", schedule.last_job_id)

        try:
            job_id, run = self._starter(schedule)
        except Exception as exc:  # a bad data file or deleted recipe must not kill the scheduler
            message = str(exc) or type(exc).__name__
            logger.warning("schedule_start_failed", schedule=schedule_id, error=message)
            self._update(schedule_id, last_error=message)
            return FireResult("error", detail=message)

        self._update(
            schedule_id,
            last_run_at=self._clock().isoformat(timespec="seconds"),
            last_job_id=job_id,
            last_error=None,
        )
        logger.info("schedule_started", schedule=schedule_id, job=job_id)
        run()
        return FireResult("started", job_id)
