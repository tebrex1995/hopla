"""Suite-wide pytest hooks and fixtures."""

import os
import zoneinfo
from pathlib import Path

import pytest

# Time-zone data comes only from the pinned tzdata package, as in production (ADR-0006):
# an OS zoneinfo with other rules must not change a test's answer.
zoneinfo.reset_tzpath(to=())

# The developer's own HOPLA_* settings must not change any test, including module-level
# `Settings()` calls made while tests are collected.
for _name in [
    n for n in os.environ if n.upper().startswith("HOPLA_")
]:  # case-insensitive, as pydantic
    del os.environ[_name]

pytest_plugins = ["pytester"]  # for the tests of this hook (tests/unit/test_collection_hook.py)

TESTS_DIR = Path(__file__).parent

# tests/<dir>/ -> the marker CI selects on (`pytest -m "unit or contract"`).
LAYER_MARKERS = {
    "unit": "unit",
    "contract": "contract",
    "scenarios": "scenario",
    "integration": "integration",
    "e2e": "e2e",
}

# pytest-socket markers that switch the guard off or replace the --allow-hosts list.
SOCKET_OPT_OUTS = ("enable_socket", "allow_hosts")


def _turns_network_on(item: pytest.Item) -> bool:
    fixtures = getattr(item, "fixturenames", ())  # only function items have fixtures
    return "socket_enabled" in fixtures or any(item.get_closest_marker(m) for m in SOCKET_OPT_OUTS)


@pytest.hookimpl(tryfirst=True)  # mark before `-m` deselects
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every test with its layer; refuse tests with no layer or with the network on.

    CI selects tests by marker, so a test outside the layer directories would be
    deselected silently and never run anywhere.
    """
    layers = set(LAYER_MARKERS.values())
    for item in items:
        # pytest-socket's opt-outs would let a test reach a source site (NFR-060).
        if _turns_network_on(item):
            raise pytest.UsageError(f"{item.nodeid}: tests may not turn the network guard off")
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
