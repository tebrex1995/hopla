"""The `hopla` command line contract that cron wrappers rely on: exit codes and usage."""

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
    assert capsys.readouterr().out.startswith("usage: hopla")


def test_no_arguments_prints_usage_and_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out.startswith("usage: hopla")


@pytest.mark.parametrize("argv", [["tick"], ["--no-such-flag"]])
def test_unknown_command_exits_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    # `tick` arrives in T-R0-06; until then a cron calling it must fail loudly, not succeed.
    with pytest.raises(SystemExit) as exc:
        main(argv)

    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
