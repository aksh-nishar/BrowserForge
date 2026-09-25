import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from browserforge.app import create_default_app
from browserforge.cli import (
    ENV_DATA_DIR,
    ENV_JOBS_DIR,
    ENV_JSON_LOGS,
    ENV_RECIPES_DIR,
    build_parser,
    main,
)

# ---- argument parsing ----


def test_defaults_are_local_only_and_use_the_project_folders() -> None:
    args = build_parser().parse_args([])
    assert args.host == "127.0.0.1"  # not exposed to the network unless asked
    assert args.port == 8000
    assert args.recipes_dir == "recipes"
    assert args.jobs_dir == "jobs"
    assert args.json_logs is False
    assert args.reload is False


def test_every_option_can_be_overridden() -> None:
    args = build_parser().parse_args(
        [
            "--host",
            "0.0.0.0",  # noqa: S104 - deliberately testing an override
            "--port",
            "9100",
            "--recipes-dir",
            "my_recipes",
            "--jobs-dir",
            "out",
            "--json-logs",
            "--reload",
        ]
    )
    assert (args.host, args.port) == ("0.0.0.0", 9100)
    assert (args.recipes_dir, args.jobs_dir) == ("my_recipes", "out")
    assert args.json_logs is True
    assert args.reload is True


@pytest.mark.parametrize("bad", [["--port", "abc"], ["--port"], ["--nope"]])
def test_bad_arguments_exit_with_a_usage_error(bad: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(bad)
    assert exc.value.code == 2


def test_help_mentions_what_the_tool_does(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--help"])
    assert exc.value.code == 0
    assert "dashboard" in capsys.readouterr().out


# ---- main ----


def test_main_starts_uvicorn_with_the_app_factory_and_the_chosen_address() -> None:
    with patch.dict(os.environ, {}, clear=False), patch("browserforge.cli.uvicorn.run") as run:
        main(["--host", "127.0.0.1", "--port", "8123"])
    run.assert_called_once_with(
        "browserforge.app:create_default_app",
        factory=True,
        host="127.0.0.1",
        port=8123,
        reload=False,
        log_config=None,
    )


def test_main_passes_its_settings_to_the_factory_through_the_environment() -> None:
    with patch.dict(os.environ, {}, clear=False), patch("browserforge.cli.uvicorn.run"):
        main(["--recipes-dir", "r", "--jobs-dir", "j", "--json-logs"])
        assert os.environ[ENV_RECIPES_DIR] == "r"
        assert os.environ[ENV_JOBS_DIR] == "j"
        assert os.environ[ENV_JSON_LOGS] == "1"


def test_main_defaults_turn_json_logs_off() -> None:
    with patch.dict(os.environ, {}, clear=False), patch("browserforge.cli.uvicorn.run"):
        main([])
        assert os.environ[ENV_JSON_LOGS] == "0"


def test_reload_flag_is_forwarded() -> None:
    with patch.dict(os.environ, {}, clear=False), patch("browserforge.cli.uvicorn.run") as run:
        main(["--reload"])
    assert run.call_args.kwargs["reload"] is True


# ---- the default app factory ----


def test_default_app_reads_its_folders_from_the_environment(tmp_path: Path) -> None:
    env = {
        ENV_RECIPES_DIR: str(tmp_path / "r"),
        ENV_JOBS_DIR: str(tmp_path / "j"),
        ENV_JSON_LOGS: "0",
    }
    with patch.dict(os.environ, env), patch("browserforge.app.configure_logging"):
        app = create_default_app()
    assert app.state.recipes_dir == tmp_path / "r"
    assert app.state.jobs_dir == tmp_path / "j"


def test_default_app_falls_back_to_the_project_folders() -> None:
    with (
        patch.dict(os.environ, {}, clear=False),
        patch("browserforge.app.configure_logging"),
    ):
        for name in (ENV_RECIPES_DIR, ENV_JOBS_DIR, ENV_JSON_LOGS):
            os.environ.pop(name, None)
        app = create_default_app()
    assert app.state.recipes_dir == Path("recipes")
    assert app.state.jobs_dir == Path("jobs")


def test_default_app_configures_logging_once_at_startup() -> None:
    configure = MagicMock()
    with (
        patch.dict(os.environ, {ENV_JSON_LOGS: "0"}),
        patch("browserforge.app.configure_logging", configure),
    ):
        create_default_app()
    configure.assert_called_once_with(json_logs=False, log_file=None)


def test_json_logs_also_go_to_a_rotating_file_under_logs() -> None:
    configure = MagicMock()
    with (
        patch.dict(os.environ, {ENV_JSON_LOGS: "1"}),
        patch("browserforge.app.configure_logging", configure),
    ):
        create_default_app()
    configure.assert_called_once_with(json_logs=True, log_file=Path("logs") / "app.log")


def test_the_data_folder_defaults_to_data_and_can_be_changed() -> None:
    assert build_parser().parse_args([]).data_dir == "data"
    assert build_parser().parse_args(["--data-dir", "elsewhere"]).data_dir == "elsewhere"


def test_main_passes_the_data_folder_through_the_environment() -> None:
    with patch.dict(os.environ, {}, clear=False), patch("browserforge.cli.uvicorn.run"):
        main(["--data-dir", "my_data"])
        assert os.environ[ENV_DATA_DIR] == "my_data"


def test_default_app_reads_the_data_folder_and_starts_the_scheduler_with_the_server(
    tmp_path: Path,
) -> None:
    env = {ENV_DATA_DIR: str(tmp_path / "d"), ENV_JSON_LOGS: "0"}
    with patch.dict(os.environ, env), patch("browserforge.app.configure_logging"):
        app = create_default_app()
    assert app.state.data_dir == tmp_path / "d"
    assert app.state.scheduler.running is False  # it starts with the server's lifespan, not here


def test_the_console_script_points_at_the_cli() -> None:
    pyproject = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'browserforge = "browserforge.cli:main"' in pyproject
