"""The layer hook in tests/conftest.py: CI selects tests by marker, so no test may go unmarked."""

from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"
# The inner run is in-process: its pytest-socket teardown would lift this test's network guard.
INNER = ("-p", "no:socket")


@pytest.fixture
def suite(pytester: pytest.Pytester) -> pytest.Pytester:
    """A throwaway project with this repo's conftest in its tests/ folder."""
    (pytester.path / "tests" / "unit").mkdir(parents=True)
    (pytester.path / "tests" / "conftest.py").write_text(CONFTEST.read_text(encoding="utf-8"))
    (pytester.path / "tests" / "unit" / "test_a.py").write_text("def test_a():\n    pass\n")
    return pytester


def test_a_test_in_a_layer_folder_gets_its_marker(suite: pytest.Pytester) -> None:
    result = suite.runpytest(*INNER, "tests", "-m", "unit")

    result.assert_outcomes(passed=1)


def test_an_unmarked_test_outside_the_layer_folders_stops_the_run(suite: pytest.Pytester) -> None:
    (suite.path / "tests" / "test_stray.py").write_text("def test_stray():\n    pass\n")

    result = suite.runpytest(*INNER, "tests", "-m", "unit")

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*tests/test_stray.py::test_stray: put the test under*"])


def test_a_marked_test_outside_the_layer_folders_is_accepted(suite: pytest.Pytester) -> None:
    stray = "import pytest\n\n@pytest.mark.unit\ndef test_stray():\n    pass\n"
    (suite.path / "tests" / "test_stray.py").write_text(stray)

    result = suite.runpytest(*INNER, "tests", "-m", "unit")

    result.assert_outcomes(passed=2)


def test_items_from_outside_tests_are_left_alone(suite: pytest.Pytester) -> None:
    (suite.path / "other").mkdir()
    (suite.path / "other" / "test_o.py").write_text("def test_o():\n    pass\n")

    result = suite.runpytest(*INNER, "tests", "other/test_o.py")

    result.assert_outcomes(passed=2)
