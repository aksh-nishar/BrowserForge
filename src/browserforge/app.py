"""FastAPI app: routes only. Logic lives in browserforge.engine and browserforge.jobs."""

import re
from pathlib import Path
from typing import Any

import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from browserforge.engine.driver import playwright_driver
from browserforge.engine.recipe import RecipeValidationError, load_recipe, parse_recipe_text
from browserforge.jobs import DriverFactory, JobNotFoundError, JobStore, run_job

logger = structlog.get_logger(__name__)

MAX_RECIPE_BYTES = 1_000_000
RECIPE_EXTENSIONS = (".yaml", ".yml")
TEMPLATES_DIR = Path(__file__).parent / "templates"


def safe_recipe_name(filename: str) -> str | None:
    """Turn an uploaded file name into a safe recipe name (letters, digits, _ and -)."""
    stem = Path(filename.replace("\\", "/")).stem
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_-")[:64]
    return name or None


def find_recipe(recipes_dir: Path, name: str) -> Path | None:
    """Path of the recipe called `name`, preferring .yaml over .yml, or None."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        return None
    for extension in RECIPE_EXTENSIONS:
        candidate = recipes_dir / f"{name}{extension}"
        if candidate.is_file():
            return candidate
    return None


def list_recipe_files(recipes_dir: Path) -> list[Path]:
    """One file per recipe name, .yaml preferred over .yml, sorted by name."""
    if not recipes_dir.is_dir():
        return []
    by_name: dict[str, Path] = {}
    for extension in reversed(RECIPE_EXTENSIONS):  # .yml first so .yaml overwrites it
        for path in recipes_dir.glob(f"*{extension}"):
            by_name[path.stem] = path
    return [by_name[name] for name in sorted(by_name)]


class JobRequest(BaseModel):
    recipe_name: str = Field(min_length=1, max_length=64)


def _default_driver_factory() -> Any:
    return playwright_driver()


def create_app(
    *,
    recipes_dir: Path,
    jobs_dir: Path,
    driver_factory: DriverFactory = _default_driver_factory,
    store: JobStore | None = None,
) -> FastAPI:
    """Build the app. Folders, the browser launcher and the job store are injectable for tests."""
    app = FastAPI(title="BrowserForge")
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    job_store = store if store is not None else JobStore()
    app.state.recipes_dir = recipes_dir
    app.state.jobs_dir = jobs_dir
    app.state.store = job_store

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

        raw = file.file.read(MAX_RECIPE_BYTES + 1)
        if len(raw) > MAX_RECIPE_BYTES:
            raise HTTPException(413, f"Recipe file is too large (limit {MAX_RECIPE_BYTES} bytes)")
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
        logger.info("recipe_saved", recipe=name, replaced=replaced, steps=len(recipe.steps))
        return {"name": name, "saved": True, "replaced": replaced, "steps": len(recipe.steps)}

    @app.get("/recipes")
    def list_recipes() -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for path in list_recipe_files(recipes_dir):
            try:
                recipe = load_recipe(path)
            except RecipeValidationError as exc:
                entries.append(
                    {"name": path.stem, "valid": False, "error": str(exc), "url": None, "steps": 0}
                )
            else:
                entries.append(
                    {
                        "name": path.stem,
                        "valid": True,
                        "error": None,
                        "url": recipe.url,
                        "steps": len(recipe.steps),
                    }
                )
        return {"recipes": entries}

    @app.post("/jobs", status_code=202)
    def start_job(body: JobRequest, background: BackgroundTasks) -> dict[str, str]:
        path = find_recipe(recipes_dir, body.recipe_name)
        if path is None:
            raise HTTPException(404, f"Recipe '{body.recipe_name}' not found")
        try:
            recipe = load_recipe(path)
        except RecipeValidationError as exc:
            raise HTTPException(400, str(exc)) from exc

        record = job_store.create(path.stem)
        logger.info("job_requested", job_id=record.job_id, recipe=path.stem)
        background.add_task(
            run_job,
            job_store,
            record.job_id,
            recipe,
            path.stem,
            jobs_dir / record.job_id,
            driver_factory,
        )
        return {"job_id": record.job_id, "status": record.status}

    @app.get("/status/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        try:
            return job_store.status(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    return app
