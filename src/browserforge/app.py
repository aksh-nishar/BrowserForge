"""FastAPI app: routes only. Logic lives in browserforge.engine and browserforge.jobs."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from browserforge.cli import ENV_DATA_DIR, ENV_JOBS_DIR, ENV_JSON_LOGS, ENV_RECIPES_DIR
from browserforge.data_input import (
    build_rows,
    parse_mapping,
    parse_values,
    prepare_single,
    preview_data,
)
from browserforge.engine.data_rows import (
    MAX_DATA_BYTES,
    DataError,
    find_env_vars,
    find_placeholders,
    secret_columns,
)
from browserforge.engine.driver import playwright_driver
from browserforge.engine.recipe import (
    Recipe,
    RecipeValidationError,
    load_recipe,
    parse_recipe_text,
)
from browserforge.engine.runner import has_submit_step, total_steps
from browserforge.jobs import (
    DriverFactory,
    JobNotFoundError,
    JobStore,
    run_bulk_job,
    run_job,
)
from browserforge.logging_config import configure_logging
from browserforge.recipe_files import (
    RECIPE_EXTENSIONS,
    find_recipe,
    list_recipe_files,
    safe_recipe_name,
)
from browserforge.results import list_results, resolve_job_file
from browserforge.schedule import (
    LOCAL_TIMEZONE,
    Schedule,
    ScheduleRunner,
    ScheduleValidationError,
    new_schedule_id,
    parse_schedule,
    resolve_timezone,
)
from browserforge.schedule_store import ScheduleStore
from browserforge.scheduler import (
    SchedulerService,
    TriggerFactory,
    cron_trigger,
    job_is_active,
    make_starter,
)

logger = structlog.get_logger(__name__)

MAX_RECIPE_BYTES = 1_000_000
TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_default_app() -> FastAPI:
    """The app `uv run browserforge` serves (uvicorn --factory).

    Folders and log format come from the BROWSERFORGE_* environment variables that the
    command line sets; logging is configured here, once, at startup.
    """
    json_logs = os.environ.get(ENV_JSON_LOGS) == "1"
    configure_logging(json_logs=json_logs, log_file=Path("logs") / "app.log" if json_logs else None)
    return create_app(
        recipes_dir=Path(os.environ.get(ENV_RECIPES_DIR, "recipes")),
        jobs_dir=Path(os.environ.get(ENV_JOBS_DIR, "jobs")),
        data_dir=Path(os.environ.get(ENV_DATA_DIR, "data")),
        start_scheduler=True,
    )


class JobRequest(BaseModel):
    recipe_name: str = Field(min_length=1, max_length=64)
    dry_run: bool = False


def _default_driver_factory() -> Any:
    return playwright_driver()


def read_upload(file: UploadFile, limit: int, what: str) -> bytes:
    """The upload's bytes, or a 413 if it is bigger than `limit`."""
    raw = file.file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(413, f"The {what} is too large (limit {limit} bytes)")
    return raw


def check_dry_run(recipe: Recipe, dry_run: bool) -> None:
    if dry_run and not has_submit_step(recipe):
        raise HTTPException(
            400,
            "A test run needs one step marked 'submit: true' (the button that sends the form). "
            "This recipe has none, so it would submit for real.",
        )


def prepare_single_run(recipe: Recipe, dry_run: bool) -> tuple[Recipe, list[str]]:
    """Check a recipe that runs once, with no data sheet. Returns (recipe to run, secret values).

    Recipes with `{{placeholders}}` need data (use /jobs/bulk); `{{env:NAME}}` values are read
    from the environment now, so a missing one fails before any browser starts.
    """
    check_dry_run(recipe, dry_run)
    try:
        return prepare_single(recipe)
    except DataError as exc:
        raise HTTPException(400, str(exc)) from exc


def create_app(
    *,
    recipes_dir: Path,
    jobs_dir: Path,
    driver_factory: DriverFactory = _default_driver_factory,
    store: JobStore | None = None,
    data_dir: Path | None = None,
    start_scheduler: bool = False,
    trigger_factory: TriggerFactory = cron_trigger,
) -> FastAPI:
    """Build the app. Folders, the browser launcher and the job store are injectable for tests.

    `data_dir` holds schedules and saved data sheets (default: a `data` folder next to the jobs
    folder). Scheduled runs only fire while the app is serving with `start_scheduler=True`;
    `trigger_factory` lets tests replace the cron timing.
    """
    data_folder = data_dir if data_dir is not None else jobs_dir.parent / "data"
    job_store = store if store is not None else JobStore()
    schedule_store = ScheduleStore(data_folder)
    runner = ScheduleRunner(
        get=schedule_store.get,
        update=schedule_store.update,
        starter=make_starter(recipes_dir, jobs_dir, job_store, schedule_store, driver_factory),
        is_active=job_is_active(job_store),
    )
    service = SchedulerService(runner, trigger_factory=trigger_factory)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if start_scheduler:
            service.start(schedule_store.all())
        try:
            yield
        finally:
            service.shutdown()

    app = FastAPI(title="BrowserForge", lifespan=lifespan)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    app.state.recipes_dir = recipes_dir
    app.state.jobs_dir = jobs_dir
    app.state.data_dir = data_folder
    app.state.store = job_store
    app.state.schedule_store = schedule_store
    app.state.schedule_runner = runner
    app.state.scheduler = service

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "dashboard.html", {})

    @app.post("/recipes")
    def upload_recipe(file: UploadFile) -> dict[str, Any]:
        filename = file.filename or ""
        if not filename.lower().endswith(RECIPE_EXTENSIONS):
            raise HTTPException(400, "Recipe file must be a .yaml or .yml file")
        name = safe_recipe_name(filename)
        if name is None:
            raise HTTPException(400, "Recipe file needs a name made of letters or digits")

        raw = read_upload(file, MAX_RECIPE_BYTES, "recipe file")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "Recipe file must be UTF-8 text") from None

        try:
            recipe = parse_recipe_text(text, filename)
        except RecipeValidationError as exc:
            logger.warning("recipe_rejected", filename=filename, error=str(exc))
            raise HTTPException(400, str(exc)) from exc

        recipes_dir.mkdir(parents=True, exist_ok=True)
        target = recipes_dir / f"{name}.yaml"
        replaced = target.exists()
        target.write_text(text, encoding="utf-8")
        logger.info("recipe_saved", recipe=name, replaced=replaced, steps=total_steps(recipe))
        return {"name": name, "saved": True, "replaced": replaced, "steps": total_steps(recipe)}

    @app.get("/recipes")
    def list_recipes() -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for path in list_recipe_files(recipes_dir):
            try:
                recipe = load_recipe(path)
            except RecipeValidationError as exc:
                entries.append(
                    {
                        "name": path.stem,
                        "valid": False,
                        "error": str(exc),
                        "url": None,
                        "steps": 0,
                        "placeholders": [],
                        "env_vars": [],
                        "has_submit": False,
                    }
                )
            else:
                entries.append(
                    {
                        "name": path.stem,
                        "valid": True,
                        "error": None,
                        "url": recipe.url,
                        "steps": total_steps(recipe),
                        "placeholders": sorted(find_placeholders(recipe)),
                        "env_vars": sorted(find_env_vars(recipe)),
                        "has_submit": has_submit_step(recipe),
                    }
                )
        return {"recipes": entries}

    def _load_named_recipe(recipe_name: str) -> tuple[Path, Recipe]:
        path = find_recipe(recipes_dir, recipe_name)
        if path is None:
            raise HTTPException(404, f"Recipe '{recipe_name}' not found")
        try:
            return path, load_recipe(path)
        except RecipeValidationError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/jobs", status_code=202)
    def start_job(body: JobRequest, background: BackgroundTasks) -> dict[str, str]:
        path, recipe = _load_named_recipe(body.recipe_name)
        runnable, secrets = prepare_single_run(recipe, body.dry_run)

        record = job_store.create(path.stem, dry_run=body.dry_run)
        logger.info("job_requested", job_id=record.job_id, recipe=path.stem, dry_run=body.dry_run)
        background.add_task(
            run_job,
            job_store,
            record.job_id,
            runnable,
            path.stem,
            jobs_dir / record.job_id,
            driver_factory,
            secrets=secrets,
            dry_run=body.dry_run,
        )
        return {"job_id": record.job_id, "status": record.status}

    @app.post("/data/preview")
    def data_preview(
        recipe_name: Annotated[str, Form(min_length=1, max_length=64)],
        file: UploadFile,
        mapping: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        """Columns of an uploaded sheet, the suggested column mapping, and any problems."""
        _, recipe = _load_named_recipe(recipe_name)
        raw = read_upload(file, MAX_DATA_BYTES, "data file")
        try:
            return preview_data(recipe, raw, file.filename or "", parse_mapping(mapping))
        except DataError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/jobs/bulk", status_code=202)
    def start_bulk_job(
        background: BackgroundTasks,
        recipe_name: Annotated[str, Form(min_length=1, max_length=64)],
        file: UploadFile | None = None,
        values: Annotated[str | None, Form()] = None,
        mapping: Annotated[str | None, Form()] = None,
        dry_run: Annotated[bool, Form()] = False,
    ) -> dict[str, Any]:
        """Run a recipe once per row of an uploaded sheet, or once with typed `values`."""
        path, recipe = _load_named_recipe(recipe_name)
        check_dry_run(recipe, dry_run)
        try:
            rows = build_rows(
                recipe,
                raw=read_upload(file, MAX_DATA_BYTES, "data file") if file else None,
                filename=(file.filename or "") if file else "",
                values=parse_values(values),
                mapping=parse_mapping(mapping),
            )
        except DataError as exc:
            raise HTTPException(400, str(exc)) from exc

        record = job_store.create(path.stem, dry_run=dry_run)
        logger.info(
            "bulk_job_requested",
            job_id=record.job_id,
            recipe=path.stem,
            rows=len(rows),
            dry_run=dry_run,
        )
        background.add_task(
            run_bulk_job,
            job_store,
            record.job_id,
            recipe,
            rows,
            path.stem,
            jobs_dir / record.job_id,
            driver_factory,
            dry_run=dry_run,
        )
        return {"job_id": record.job_id, "status": record.status, "rows": len(rows)}

    def _require_job(job_id: str) -> Path:
        if not job_store.exists(job_id):
            raise HTTPException(404, f"Job '{job_id}' not found")
        return jobs_dir / job_id

    @app.get("/jobs/{job_id}/results")
    def job_results(job_id: str) -> dict[str, Any]:
        job_dir = _require_job(job_id)
        if not job_dir.is_dir():
            raise HTTPException(404, f"Job '{job_id}' has no results yet")
        return list_results(job_id, job_dir)

    @app.get("/jobs/{job_id}/files/{file_path:path}")
    def job_file(job_id: str, file_path: str) -> FileResponse:
        target = resolve_job_file(_require_job(job_id), file_path)
        if target is None:
            raise HTTPException(404, "File not found")
        if target.suffix.lower() == ".png":
            return FileResponse(target)  # shown inline in the gallery
        return FileResponse(target, filename=target.name)  # everything else downloads

    @app.get("/status/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        try:
            return job_store.status(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    # ---- schedules ----

    def _schedule_view(schedule: Schedule) -> dict[str, Any]:
        last_status = None
        if schedule.last_job_id and job_store.exists(schedule.last_job_id):
            last_status = job_store.get(schedule.last_job_id).status
        next_run = service.next_run(schedule)
        return {
            "id": schedule.id,
            "recipe_name": schedule.recipe_name,
            "when": schedule.when,
            "timezone": schedule.timezone,
            "enabled": schedule.enabled,
            "has_data": schedule.data_file is not None,
            "next_run": next_run.isoformat(timespec="minutes") if next_run else None,
            "last_run_at": schedule.last_run_at,
            "last_job_id": schedule.last_job_id,
            "last_status": last_status,
            "last_error": schedule.last_error,
        }

    def _schedule_data(
        recipe: Recipe, raw: bytes | None, filename: str, mapping: dict[str, str] | None
    ) -> None:
        """Check a schedule's data now, so a problem is reported here and not at 3 a.m."""
        if raw is None:
            if find_placeholders(recipe):
                names = ", ".join("{{" + name + "}}" for name in sorted(find_placeholders(recipe)))
                raise DataError(
                    f"This recipe fills in {names}, so a schedule for it needs a data file "
                    "(.csv or .xlsx). Upload one."
                )
            prepare_single(recipe)
            return
        rows = build_rows(recipe, raw=raw, filename=filename, mapping=mapping)
        secrets = secret_columns(rows[0])
        if secrets:
            raise DataError(
                f"The data file has column(s) that look like secrets: {', '.join(secrets)}. "
                "A saved schedule must not hold passwords or ID numbers. Use {{env:NAME}} in "
                "the recipe and set that environment variable on this computer instead."
            )

    @app.post("/schedules", status_code=201)
    def create_schedule(
        recipe_name: Annotated[str, Form(min_length=1, max_length=64)],
        when: Annotated[str, Form(min_length=1, max_length=100)],
        timezone: Annotated[str, Form(max_length=64)] = LOCAL_TIMEZONE,
        file: UploadFile | None = None,
        mapping: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        path, recipe = _load_named_recipe(recipe_name)
        try:
            cron = parse_schedule(when)
            resolve_timezone(timezone)
            raw = read_upload(file, MAX_DATA_BYTES, "data file") if file else None
            filename = (file.filename or "") if file else ""
            chosen = parse_mapping(mapping)
            _schedule_data(recipe, raw, filename, chosen)
        except (ScheduleValidationError, DataError) as exc:
            raise HTTPException(400, str(exc)) from exc

        schedule = Schedule(
            id=new_schedule_id(),
            recipe_name=path.stem,
            when=" ".join(when.split()),
            cron=cron,
            timezone=timezone,
            mapping=chosen,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        saved = schedule_store.add(schedule, (filename, raw) if raw is not None else None)
        service.sync(saved)
        logger.info("schedule_created", schedule=saved.id, recipe=saved.recipe_name, cron=cron)
        return _schedule_view(saved)

    @app.get("/schedules")
    def list_schedules() -> dict[str, Any]:
        return {
            "running": service.running,
            "schedules": [_schedule_view(s) for s in schedule_store.all()],
        }

    def _require_schedule(schedule_id: str) -> Schedule:
        schedule = schedule_store.get(schedule_id)
        if schedule is None:
            raise HTTPException(404, f"Schedule '{schedule_id}' not found")
        return schedule

    @app.delete("/schedules/{schedule_id}")
    def delete_schedule(schedule_id: str) -> dict[str, Any]:
        _require_schedule(schedule_id)
        schedule_store.remove(schedule_id)
        service.remove(schedule_id)
        logger.info("schedule_deleted", schedule=schedule_id)
        return {"deleted": True}

    def _set_enabled(schedule_id: str, enabled: bool) -> dict[str, Any]:
        _require_schedule(schedule_id)
        updated = schedule_store.update(schedule_id, enabled=enabled)
        if updated is None:
            raise HTTPException(404, f"Schedule '{schedule_id}' not found")
        service.sync(updated)
        return _schedule_view(updated)

    @app.post("/schedules/{schedule_id}/pause")
    def pause_schedule(schedule_id: str) -> dict[str, Any]:
        return _set_enabled(schedule_id, False)

    @app.post("/schedules/{schedule_id}/resume")
    def resume_schedule(schedule_id: str) -> dict[str, Any]:
        return _set_enabled(schedule_id, True)

    return app
