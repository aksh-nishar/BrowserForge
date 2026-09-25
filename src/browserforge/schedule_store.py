"""Saving schedules to disk: `<data dir>/schedules.json`, plus each schedule's data sheet.

Schedules survive a restart. The file holds recipe names, times and last-run info only:
never passwords (those come from environment variables at run time).
"""

import shutil
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import structlog

from browserforge.jsonfile import load_json, save_json
from browserforge.schedule import Schedule

logger = structlog.get_logger(__name__)

SCHEDULES_FILE = "schedules.json"
DATA_FOLDER = "schedule_data"
DATA_SUFFIXES = (".csv", ".xlsx")


def _entries_of(raw: Any) -> list[Any]:
    entries = raw["schedules"]
    if not isinstance(entries, list):
        raise TypeError("'schedules' must be a list")
    return entries


class ScheduleStore:
    """Thread-safe: the scheduler thread updates last-run info while requests read and edit."""

    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        self._path = data_dir / SCHEDULES_FILE
        self._lock = threading.RLock()

    # ---- reading ----

    def _load(self) -> list[Schedule]:
        entries = load_json(self._path, _entries_of) or []
        schedules: list[Schedule] = []
        for entry in entries:
            try:
                schedules.append(Schedule.from_dict(entry))
            except (TypeError, ValueError, AttributeError):
                logger.warning("schedule_entry_skipped")
        return schedules

    def all(self) -> list[Schedule]:
        with self._lock:
            return self._load()

    def get(self, schedule_id: str) -> Schedule | None:
        with self._lock:
            return next((s for s in self._load() if s.id == schedule_id), None)

    # ---- writing ----

    def _save(self, schedules: list[Schedule]) -> None:
        save_json(self._path, {"schedules": [s.to_dict() for s in schedules]})

    def _data_dir_of(self, schedule_id: str) -> Path:
        return self._dir / DATA_FOLDER / schedule_id

    def add(self, schedule: Schedule, data: tuple[str, bytes] | None = None) -> Schedule:
        """Save a new schedule. `data` is (file name, bytes) of its data sheet, if it has one."""
        with self._lock:
            schedules = self._load()
            if any(s.id == schedule.id for s in schedules):
                raise ValueError(f"A schedule with id '{schedule.id}' already exists")
            if data is not None:
                schedule = replace(schedule, data_file=self._write_data(schedule.id, *data))
            schedules.append(schedule)
            self._save(schedules)
            return schedule

    def _write_data(self, schedule_id: str, filename: str, raw: bytes) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in DATA_SUFFIXES:
            raise ValueError("The data file must be a .csv or .xlsx file")
        folder = self._data_dir_of(schedule_id)
        folder.mkdir(parents=True, exist_ok=True)
        stored = f"data{suffix}"  # a fixed name: nothing from the upload is used as a path
        (folder / stored).write_bytes(raw)
        return stored

    def update(self, schedule_id: str, **changes: Any) -> Schedule | None:
        """Change fields of a schedule. Returns the new version, or None if it no longer exists."""
        with self._lock:
            schedules = self._load()
            for index, schedule in enumerate(schedules):
                if schedule.id == schedule_id:
                    schedules[index] = replace(schedule, **changes)
                    self._save(schedules)
                    return schedules[index]
            return None

    def remove(self, schedule_id: str) -> bool:
        with self._lock:
            schedules = self._load()
            kept = [s for s in schedules if s.id != schedule_id]
            if len(kept) == len(schedules):
                return False
            self._save(kept)
            shutil.rmtree(self._data_dir_of(schedule_id), ignore_errors=True)
            return True

    def read_data(self, schedule: Schedule) -> tuple[str, bytes] | None:
        """(file name, bytes) of the schedule's saved data sheet, or None if it has none."""
        if schedule.data_file is None or schedule.data_file not in {
            f"data{s}" for s in DATA_SUFFIXES
        }:
            return None
        path = self._data_dir_of(schedule.id) / schedule.data_file
        try:
            return schedule.data_file, path.read_bytes()
        except OSError:
            return None
