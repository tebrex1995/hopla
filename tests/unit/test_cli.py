"""The `hopla` command line contract that cron wrappers rely on: exit codes and usage."""

import sys
from importlib.metadata import version

import pytest

from hopla.pipeline.cli import main


@pytest.fixture(autouse=True)
def _no_colors(monkeypatch: pytest.MonkeyPatch) -> None:
    # Python 3.14 argparse colours its output when FORCE_COLOR or PYTHON_COLORS=1 is set.
    monkeypatch.setenv("PYTHON_COLORS", "0")


def test_help_exits_0_and_names_the_program(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: hopla [")
    assert "--version" in out


def test_no_arguments_prints_usage_and_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: hopla [")
    assert "Serbian train + bus planner" in out  # the full help, not just the usage line


@pytest.mark.parametrize("argv", [["tick"], ["--no-such-flag"]])
def test_unknown_command_exits_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    # `tick` arrives in T-R0-06; until then a cron calling it must fail loudly, not succeed.
    with pytest.raises(SystemExit) as exc:
        main(argv)

    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_the_console_entry_reads_sys_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The installed `hopla` script calls main() with no arguments, as cron will.
    monkeypatch.setattr(sys, "argv", ["hopla", "tick"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "unrecognized arguments: tick" in capsys.readouterr().err


def test_version_prints_the_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out == f"hopla {version('hopla')}\n"
