"""Suite-wide pytest hooks and fixtures."""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]  # for the tests of this hook (tests/unit/test_layer_guard.py)

TESTS_DIR = Path(__file__).parent

# tests/<dir>/ -> the marker CI selects on (`pytest -m "unit or contract"`).
LAYER_MARKERS = {
    "unit": "unit",
    "contract": "contract",
    "scenarios": "scenario",
    "integration": "integration",
    "e2e": "e2e",
}


@pytest.hookimpl(tryfirst=True)  # mark before `-m` deselects
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every test with its layer, and refuse tests that have none.

    CI selects tests by marker, so a test outside the layer directories would be
    deselected silently and never run anywhere.
    """
    layers = set(LAYER_MARKERS.values())
    for item in items:
        if not item.path.is_relative_to(TESTS_DIR):
            continue  # collected from elsewhere (e.g. doctests in src/): not a suite test
        top = item.path.relative_to(TESTS_DIR).parts[0]
        if top in LAYER_MARKERS:
            item.add_marker(LAYER_MARKERS[top])
        elif not any(item.get_closest_marker(layer) for layer in layers):
            raise pytest.UsageError(
                f"{item.nodeid}: put the test under tests/<layer>/ or mark it with its layer"
            )


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Async tests run on asyncio only (the backend production uses, ADR-0001)."""
    return "asyncio"
