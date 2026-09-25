"""What a finished (or running) job left on disk, as URLs the dashboard can link to."""

from pathlib import Path
from typing import Any
from urllib.parse import quote

FAILURE_PREFIX = "failure_"
ROW_PREFIX = "row_"


def file_url(job_id: str, relative: Path) -> str:
    return f"/jobs/{quote(job_id)}/files/{quote(relative.as_posix())}"


def _entry(job_id: str, job_dir: Path, path: Path, name: str | None = None) -> dict[str, Any]:
    return {"name": name or path.name, "url": file_url(job_id, path.relative_to(job_dir))}


def _row_dirs(job_dir: Path) -> list[Path]:
    """Per-row subfolders of a bulk run (row_0001, row_0002, ...), in order."""
    if not job_dir.is_dir():
        return []
    return sorted(p for p in job_dir.iterdir() if p.is_dir() and p.name.startswith(ROW_PREFIX))


def _files_in(directory: Path, suffix: str | None = None) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and (suffix is None or path.suffix.lower() == suffix)
    )


def list_results(job_id: str, job_dir: Path) -> dict[str, Any]:
    """Screenshots, the Excel report, downloaded files and the run log of one job."""
    # A bulk run keeps each row's files in its own subfolder; they are listed with the row's
    # folder name in front so they stay apart ("row_0002/03_step_....png").
    folders = [(job_dir, "")] + [(row_dir, f"{row_dir.name}/") for row_dir in _row_dirs(job_dir)]
    screenshots = [
        {
            **_entry(job_id, job_dir, path, prefix + path.name),
            "failure": path.name.startswith(FAILURE_PREFIX),
        }
        for folder, prefix in folders
        for path in _files_in(folder / "screenshots", ".png")
    ]
    downloads = [
        _entry(job_id, job_dir, path, prefix + path.name)
        for folder, prefix in folders
        for path in _files_in(folder / "downloads")
    ]
    excel_path = job_dir / "output.xlsx"
    log_path = job_dir / "run_log.json"
    return {
        "job_id": job_id,
        "screenshots": screenshots,
        "excel": _entry(job_id, job_dir, excel_path) if excel_path.is_file() else None,
        "downloads": downloads,
        "run_log": _entry(job_id, job_dir, log_path) if log_path.is_file() else None,
    }


def resolve_job_file(job_dir: Path, relative: str) -> Path | None:
    """The file `relative` inside `job_dir`, or None if it is missing or escapes the folder."""
    base = job_dir.resolve()
    try:
        target = (base / relative).resolve()
    except (OSError, ValueError):  # e.g. NUL bytes or invalid characters in the path
        return None
    if not target.is_relative_to(base) or not target.is_file():
        return None
    return target
