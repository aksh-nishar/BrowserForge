"""`browserforge run`: run a recipe from the command line, once or once per data row."""

import io
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from browserforge.cli import build_run_parser, main, run_command
from browserforge.engine.driver import DriverError
from fixtures.fake_driver import FakePageDriver

FORM = (
    "url: https://x.io\n"
    "steps:\n"
    "  - type: fill\n"
    '    selector: "#e"\n'
    '    value: "{{email}}"\n'
    "  - type: click\n"
    "    name: send\n"
    '    selector: "#send"\n'
    "    submit: true\n"
)
PLAIN = "url: https://x.io\nsteps:\n  - type: click\n    selector: '#go'\n"


class Cli:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.recipes = tmp_path / "recipes"
        self.jobs = tmp_path / "jobs"
        self.recipes.mkdir()
        self.fake = FakePageDriver()
        self.out = io.StringIO()
        self.err = io.StringIO()

    def recipe(self, name: str, text: str) -> None:
        (self.recipes / f"{name}.yaml").write_text(text, encoding="utf-8")

    def run(self, *args: str) -> int:
        return run_command(
            [*args, "--recipes-dir", str(self.recipes), "--jobs-dir", str(self.jobs)],
            driver_factory=lambda: nullcontext(self.fake),
            out=self.out,
            err=self.err,
        )


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    c = Cli(tmp_path)
    c.recipe("form", FORM)
    c.recipe("plain", PLAIN)
    return c


# ---- argument parsing ----


def test_run_parser_defaults() -> None:
    args = build_run_parser().parse_args(["demo"])
    assert args.recipe == "demo"
    assert args.data is None
    assert args.map == []
    assert args.dry_run is False
    assert args.headed is False


def test_run_parser_takes_repeated_map_options() -> None:
    args = build_run_parser().parse_args(
        ["demo", "--map", "a=b", "--map", "c=d", "--data", "x.csv"]
    )
    assert args.map == ["a=b", "c=d"]
    assert args.data == "x.csv"


def test_main_hands_the_run_subcommand_to_run_command() -> None:
    with patch("browserforge.cli.run_command", return_value=3) as run:
        with pytest.raises(SystemExit) as info:
            main(["run", "demo", "--dry-run"])
    run.assert_called_once_with(["demo", "--dry-run"])
    assert info.value.code == 3


def test_main_without_run_still_starts_the_dashboard() -> None:
    with patch("browserforge.cli.uvicorn.run") as serve:
        main(["--port", "9999"])
    assert serve.call_args.kwargs["port"] == 9999


# ---- single runs ----


def test_a_plain_recipe_runs_once_and_exits_zero(cli: Cli) -> None:
    assert cli.run("plain") == 0
    assert cli.fake.clicks == ["#go"]
    output = cli.out.getvalue()
    assert "Steps: 2 succeeded, 0 failed" in output
    assert "Result: success" in output
    assert str(cli.jobs) in output


def test_a_failing_step_exits_one_and_names_the_step(cli: Cli) -> None:
    cli.fake.set_missing("#go")
    assert cli.run("plain") == 1
    output = cli.out.getvalue()
    assert "Result: FAILED" in output
    assert "#go" in output


def test_a_recipe_can_be_given_as_a_file_path(cli: Cli, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(PLAIN, encoding="utf-8")
    assert cli.run(str(elsewhere)) == 0
    assert cli.fake.clicks == ["#go"]


def test_a_recipe_that_needs_data_is_refused_without_it(cli: Cli) -> None:
    assert cli.run("form") == 2
    assert "{{email}}" in cli.err.getvalue()
    assert cli.fake.calls == []


# ---- bulk runs ----


def test_data_runs_the_recipe_once_per_row(cli: Cli) -> None:
    data = cli.tmp / "people.csv"
    data.write_text("email\na@x.io\nb@x.io\n", encoding="utf-8")
    assert cli.run("form", "--data", str(data)) == 0
    assert cli.fake.calls_to("fill") == [("#e", "a@x.io"), ("#e", "b@x.io")]
    assert "Rows: 2 succeeded, 0 failed, of 2" in cli.out.getvalue()


def test_a_failed_row_is_listed_and_exits_one(cli: Cli) -> None:
    data = cli.tmp / "people.csv"
    data.write_text("email\na@x.io\nb@x.io\n", encoding="utf-8")
    cli.fake.fail_on("fill", DriverError("nope"), times=1)
    assert cli.run("form", "--data", str(data), "--map", "email=email") == 1
    output = cli.out.getvalue()
    assert "Rows: 1 succeeded, 1 failed, of 2" in output
    assert "Row 1" in output


def test_map_options_pick_the_column_for_a_placeholder(cli: Cli) -> None:
    data = cli.tmp / "people.csv"
    data.write_text("mail address\nz@x.io\n", encoding="utf-8")
    assert cli.run("form", "--data", str(data), "--map", "email=mail address") == 0
    assert cli.fake.fills == {"#e": "z@x.io"}


def test_bad_data_exits_two_and_says_why(cli: Cli) -> None:
    other = cli.tmp / "wrong.csv"
    other.write_text("city\nPune\n", encoding="utf-8")
    assert cli.run("form", "--data", str(other)) == 2
    assert "no column for 'email'" in cli.err.getvalue()
    assert cli.fake.calls == []


def test_a_missing_data_file_exits_two(cli: Cli) -> None:
    assert cli.run("form", "--data", str(cli.tmp / "nope.csv")) == 2
    assert "cannot read data file" in cli.err.getvalue()


@pytest.mark.parametrize("bad", ["noequals", "=x", "x="])
def test_a_malformed_map_option_exits_two(cli: Cli, bad: str) -> None:
    assert cli.run("plain", "--map", bad) == 2
    assert "--map needs PLACEHOLDER=COLUMN" in cli.err.getvalue()


# ---- dry run and lookup errors ----


def test_dry_run_stops_before_submit(cli: Cli) -> None:
    data = cli.tmp / "people.csv"
    data.write_text("email\na@x.io\n", encoding="utf-8")
    assert cli.run("form", "--data", str(data), "--dry-run") == 0
    assert cli.fake.clicks == []


def test_dry_run_of_a_recipe_without_submit_exits_two(cli: Cli) -> None:
    assert cli.run("plain", "--dry-run") == 2
    assert "submit: true" in cli.err.getvalue()
    assert cli.fake.calls == []


def test_an_unknown_recipe_exits_two(cli: Cli) -> None:
    assert cli.run("nope") == 2
    assert "'nope' not found" in cli.err.getvalue()


def test_an_invalid_recipe_exits_two(cli: Cli) -> None:
    cli.recipe("broken", "steps: []\n")
    assert cli.run("broken") == 2
    assert "'url'" in cli.err.getvalue()
