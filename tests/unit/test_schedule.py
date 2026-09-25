"""Schedule parsing, next-run times and the fire/skip decision. No real clock, no browser."""

from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from browserforge.schedule import (
    FireResult,
    Schedule,
    ScheduleRunner,
    ScheduleValidationError,
    new_schedule_id,
    next_run_times,
    parse_schedule,
    resolve_timezone,
)

# ---- parse_schedule: friendly forms ----


@pytest.mark.parametrize(
    ("text", "cron"),
    [
        ("hourly", "0 * * * *"),
        ("daily 08:00", "0 8 * * *"),
        ("Daily  8:05", "5 8 * * *"),
        ("  DAILY   23:59 ", "59 23 * * *"),
        ("daily 00:00", "0 0 * * *"),
        ("weekly mon 09:30", "30 9 * * mon"),
        ("weekly SUN 0:00", "0 0 * * sun"),
        ("every 15 minutes", "*/15 * * * *"),
        ("every 5 minutes", "*/5 * * * *"),
        ("every 30 minutes", "*/30 * * * *"),
        ("every 2 hours", "0 */2 * * *"),
        ("every 1 hour", "0 */1 * * *"),
        ("every 12 hours", "0 */12 * * *"),
    ],
)
def test_friendly_schedules_become_cron(text: str, cron: str) -> None:
    assert parse_schedule(text) == cron


@pytest.mark.parametrize(
    "text",
    [
        "daily 24:00",
        "daily 08:60",
        "daily 8:5",
        "daily noon",
        "weekly funday 09:00",
        "weekly mon 25:00",
        "every 3 minutes",  # too often for browser jobs
        "every 7 minutes",  # does not divide an hour evenly
        "every 60 minutes",
        "every 0 hours",
        "every 5 hours",
        "every 24 hours",
    ],
)
def test_bad_friendly_schedules_are_rejected_with_a_reason(text: str) -> None:
    with pytest.raises(ScheduleValidationError) as info:
        parse_schedule(text)
    assert str(info.value)


def test_the_error_for_a_bad_time_says_what_is_expected() -> None:
    with pytest.raises(ScheduleValidationError, match="HH:MM"):
        parse_schedule("daily 8:5")


# ---- parse_schedule: cron ----


@pytest.mark.parametrize(
    "cron",
    ["30 8 * * mon-fri", "*/5 * * * *", "0 9 1 * *", "0 0 * * *", "15 10 * * mon,wed,fri"],
)
def test_valid_cron_is_accepted_unchanged(cron: str) -> None:
    assert parse_schedule(cron) == cron


def test_cron_is_normalised_for_case_and_spaces() -> None:
    assert parse_schedule("  30  8 * *  MON-FRI ") == "30 8 * * mon-fri"


@pytest.mark.parametrize("cron", ["30 8 * * 1", "30 8 * * 0-4", "30 8 * * 7"])
def test_numeric_weekdays_are_refused_because_tools_disagree(cron: str) -> None:
    with pytest.raises(ScheduleValidationError, match="as names"):
        parse_schedule(cron)


def test_an_unknown_day_name_is_named() -> None:
    with pytest.raises(ScheduleValidationError, match="'foo'"):
        parse_schedule("30 8 * * foo")


@pytest.mark.parametrize("cron", ["99 8 * * *", "0 25 * * *", "0 8 32 * *", "0 8 * 13 *"])
def test_out_of_range_cron_fields_are_rejected(cron: str) -> None:
    with pytest.raises(ScheduleValidationError, match="cron"):
        parse_schedule(cron)


@pytest.mark.parametrize("text", ["", "   ", "daily", "sometimes", "* * * *", "* * * * * *"])
def test_unrecognised_text_is_rejected(text: str) -> None:
    with pytest.raises(ScheduleValidationError):
        parse_schedule(text)


# ---- timezones ----


def test_local_and_named_timezones_resolve() -> None:
    assert isinstance(resolve_timezone("local"), tzinfo)
    assert resolve_timezone("UTC") == ZoneInfo("UTC")
    assert str(resolve_timezone("Asia/Kolkata")) == "Asia/Kolkata"


@pytest.mark.parametrize("name", ["Nope/Zone", "", "../../etc/passwd", "IST5"])
def test_unknown_timezones_are_rejected(name: str) -> None:
    with pytest.raises(ScheduleValidationError, match="timezone"):
        resolve_timezone(name)


# ---- next_run_times ----


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_daily_runs_cross_a_month_boundary() -> None:
    times = next_run_times("0 8 * * *", "UTC", utc(2026, 1, 31, 9, 0))
    assert times == [utc(2026, 2, 1, 8), utc(2026, 2, 2, 8), utc(2026, 2, 3, 8)]


def test_daily_runs_cross_a_year_boundary() -> None:
    times = next_run_times("0 8 * * *", "UTC", utc(2026, 12, 31, 23, 0), count=2)
    assert times == [utc(2027, 1, 1, 8), utc(2027, 1, 2, 8)]


def test_a_run_exactly_at_the_current_time_counts_as_next() -> None:
    assert next_run_times("0 8 * * *", "UTC", utc(2026, 5, 5, 8, 0), count=1) == [
        utc(2026, 5, 5, 8)
    ]


def test_weekly_runs_land_on_the_named_day() -> None:
    (first,) = next_run_times("30 9 * * mon", "UTC", utc(2026, 9, 23, 12, 0), count=1)
    assert (first.year, first.month, first.day, first.hour, first.minute) == (2026, 9, 28, 9, 30)
    assert first.strftime("%a") == "Mon"


def test_weekday_ranges_skip_the_weekend() -> None:
    days = [
        t.strftime("%a") for t in next_run_times("0 8 * * mon-fri", "UTC", utc(2026, 9, 25, 9), 4)
    ]
    assert days == ["Mon", "Tue", "Wed", "Thu"]  # Friday 25th 08:00 has passed


def test_every_15_minutes_steps_evenly() -> None:
    times = next_run_times("*/15 * * * *", "UTC", utc(2026, 1, 1, 10, 7))
    assert [(t.hour, t.minute) for t in times] == [(10, 15), (10, 30), (10, 45)]


def test_the_timezone_decides_the_local_hour_across_daylight_saving() -> None:
    new_york = "America/New_York"
    before = next_run_times("0 8 * * *", new_york, utc(2026, 3, 7, 0, 0), count=1)[0]
    after = next_run_times("0 8 * * *", new_york, utc(2026, 3, 9, 0, 0), count=1)[0]
    assert before.hour == after.hour == 8  # 08:00 local both times
    assert before.astimezone(UTC).hour == 13  # winter: UTC-5
    assert after.astimezone(UTC).hour == 12  # summer: UTC-4


def test_count_limits_the_result_and_zero_gives_nothing() -> None:
    assert len(next_run_times("0 8 * * *", "UTC", utc(2026, 1, 1), count=5)) == 5
    assert next_run_times("0 8 * * *", "UTC", utc(2026, 1, 1), count=0) == []


def test_next_run_times_rejects_an_unknown_timezone() -> None:
    with pytest.raises(ScheduleValidationError):
        next_run_times("0 8 * * *", "Nope/Zone", utc(2026, 1, 1))


# ---- Schedule ----


def make(**changes: Any) -> Schedule:
    base = Schedule(id="abc12345", recipe_name="demo", when="daily 08:00", cron="0 8 * * *")
    return replace(base, **changes)


def test_a_schedule_round_trips_through_a_dict() -> None:
    original = make(mapping={"email": "E-mail"}, data_file="data.csv", last_job_id="j1")
    assert Schedule.from_dict(original.to_dict()) == original


def test_unknown_keys_in_saved_data_are_ignored() -> None:
    data = make().to_dict() | {"from_the_future": 1}
    assert Schedule.from_dict(data) == make()


def test_new_ids_are_short_and_unique() -> None:
    ids = {new_schedule_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 8 for i in ids)


# ---- ScheduleRunner.fire ----


class Rig:
    """A runner wired to in-memory fakes so every decision can be checked."""

    def __init__(self, *schedules: Schedule) -> None:
        self.schedules = {s.id: s for s in schedules}
        self.active: set[str] = set()
        self.started: list[str] = []
        self.ran: list[str] = []
        self.fail_with: Exception | None = None
        self.now = datetime(2026, 9, 25, 8, 0, 0)
        self.runner = ScheduleRunner(
            get=self.schedules.get,
            update=self._update,
            starter=self._starter,
            is_active=lambda job_id: job_id in self.active,
            clock=lambda: self.now,
        )

    def _update(self, schedule_id: str, **changes: Any) -> Schedule | None:
        if schedule_id not in self.schedules:
            return None
        self.schedules[schedule_id] = replace(self.schedules[schedule_id], **changes)
        return self.schedules[schedule_id]

    def _starter(self, schedule: Schedule) -> tuple[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        job_id = f"job{len(self.started) + 1}"
        self.started.append(job_id)
        return job_id, lambda: self.ran.append(job_id)


def test_firing_starts_a_job_runs_it_and_records_it() -> None:
    rig = Rig(make())
    result = rig.runner.fire("abc12345")

    assert result == FireResult("started", "job1")
    assert rig.ran == ["job1"]
    saved = rig.schedules["abc12345"]
    assert saved.last_job_id == "job1"
    assert saved.last_run_at == "2026-09-25T08:00:00"
    assert saved.last_error is None


def test_a_paused_schedule_does_not_fire() -> None:
    rig = Rig(make(enabled=False))
    assert rig.runner.fire("abc12345") == FireResult("paused")
    assert rig.started == []


def test_a_deleted_schedule_is_reported_missing() -> None:
    rig = Rig()
    assert rig.runner.fire("gone") == FireResult("missing")
    assert rig.started == []


def test_a_still_running_previous_job_causes_a_skip_not_a_second_run() -> None:
    rig = Rig(make(last_job_id="job0"))
    rig.active.add("job0")
    result = rig.runner.fire("abc12345")

    assert result.outcome == "overlap"
    assert rig.started == []
    assert "previous run was still going" in (rig.schedules["abc12345"].last_error or "")
    assert rig.schedules["abc12345"].last_job_id == "job0"


def test_once_the_previous_job_has_finished_the_next_firing_runs() -> None:
    rig = Rig(make(last_job_id="job0"))
    rig.active.add("job0")
    assert rig.runner.fire("abc12345").outcome == "overlap"
    rig.active.clear()  # job0 finished
    assert rig.runner.fire("abc12345").outcome == "started"
    assert rig.schedules["abc12345"].last_error is None  # the skip note is cleared


def test_a_starter_that_fails_is_recorded_and_never_raises() -> None:
    rig = Rig(make(last_job_id="job0"))
    rig.fail_with = ValueError("The recipe 'demo' no longer exists.")
    result = rig.runner.fire("abc12345")

    assert result.outcome == "error"
    assert result.detail == "The recipe 'demo' no longer exists."
    assert rig.schedules["abc12345"].last_error == "The recipe 'demo' no longer exists."
    assert rig.schedules["abc12345"].last_job_id == "job0"  # unchanged
    assert rig.ran == []


def test_an_error_without_a_message_still_says_something() -> None:
    rig = Rig(make())
    rig.fail_with = RuntimeError()
    assert rig.runner.fire("abc12345").detail == "RuntimeError"


def test_each_firing_starts_its_own_job() -> None:
    rig = Rig(make())
    rig.runner.fire("abc12345")
    rig.runner.fire("abc12345")
    assert rig.started == ["job1", "job2"]
    assert rig.ran == ["job1", "job2"]
