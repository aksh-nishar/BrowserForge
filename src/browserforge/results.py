"""What a finished (or running) job left on disk, as URLs the dashboard can link to."""

from pathlib import Path
from typing import Any
from urllib.parse import quote

FAILURE_PREFIX = "failure_"


def file_url(job_id: str, relative: Path) -> str:
    return f"/jobs/{quote(job_id)}/files/{quote(relative.as_posix())}"


def _entry(job_id: str, job_dir: Path, path: Path) -> dict[str, Any]:
    return {"name": path.name, "url": file_url(job_id, path.relative_to(job_dir))}


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
    screenshots = [
        {**_entry(job_id, job_dir, path), "failure": path.name.startswith(FAILURE_PREFIX)}
        for path in _files_in(job_dir / "screenshots", ".png")
    ]
    excel_path = job_dir / "output.xlsx"
    log_path = job_dir / "run_log.json"
    return {
        "job_id": job_id,
        "screenshots": screenshots,
        "excel": _entry(job_id, job_dir, excel_path) if excel_path.is_file() else None,
        "downloads": [_entry(job_id, job_dir, p) for p in _files_in(job_dir / "downloads")],
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
