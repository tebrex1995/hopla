"""`hopla.core` is pure: it imports only listed pure stdlib modules and itself (ADR-0001, ADR-0006).

Ruff's banned-api list catches the usual direct imports at lint time. This test is
the allowlist behind it: any import that isn't listed below fails, so a new package
or an I/O module can't slip in just because nobody added it to the denylist.
Transitive contracts arrive with import-linter (T-R0-30).
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
CORE_DIR = SRC_DIR / "hopla" / "core"

# Stdlib modules with no I/O, clocks or randomness. Add one here when core needs it.
# zoneinfo reads the bundled tz database, which core needs for Europe/Belgrade times.
PURE_STDLIB = frozenset(
    {
        "__future__",
        "abc",
        "base64",
        "bisect",
        "collections",
        "contextlib",
        "copy",
        "dataclasses",
        "datetime",
        "decimal",
        "difflib",
        "enum",
        "fractions",
        "functools",
        "graphlib",
        "hashlib",
        "heapq",
        "itertools",
        "json",
        "math",
        "numbers",
        "operator",
        "re",
        "statistics",
        "string",
        "textwrap",
        "types",
        "typing",
        "unicodedata",
        "urllib.parse",
        "uuid",  # content-derived ids only (uuid3/uuid5, UUID); ruff bans uuid1/4/6/7/8
        "zoneinfo",
    }
)
ALLOWED = PURE_STDLIB | {"hopla.core"}

# Builtins that do I/O or run code from strings; flagged when named at all (`f = open` too).
BANNED_NAMES = frozenset(
    {"__builtins__", "__import__", "breakpoint", "compile", "eval", "exec", "input", "open"}
)


def _is_allowed(name: str) -> bool:
    return any(name == allowed or name.startswith(f"{allowed}.") for allowed in ALLOWED)


def _has_allowed_submodule(name: str) -> bool:
    return any(allowed.startswith(f"{name}.") for allowed in ALLOWED)


def _resolve_relative(package: str, level: int, target: str | None) -> str | None:
    """The absolute name of `from <level dots><target> import ...` in `package`, or None."""
    parts = package.split(".")
    if level > len(parts):
        return None
    base = parts[: len(parts) - level + 1]
    return ".".join([*base, target] if target else base)


def import_violations(source: str, package: str) -> list[str]:
    """Every forbidden import and banned builtin name in `source`, a module of `package`."""
    violations = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            violations += [alias.name for alias in node.names if not _is_allowed(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            target = node.module
            if node.level:
                target = _resolve_relative(package, node.level, node.module)
            if target is None:
                violations.append("." * node.level + (node.module or ""))
            elif _has_allowed_submodule(target):
                # `from urllib import parse` may name an allowed submodule of a package that isn't.
                violations += [
                    f"{target}.{alias.name}"
                    for alias in node.names
                    if not _is_allowed(f"{target}.{alias.name}")
                ]
            elif not _is_allowed(target):
                violations.append(target)
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            violations.append(node.id)
    return violations


def test_core_imports_only_pure_modules() -> None:
    files = sorted(CORE_DIR.rglob("*.py"))
    assert CORE_DIR / "__init__.py" in files  # the scan must not pass because it found nothing

    found = {}
    for path in files:
        # Relative imports resolve from the file's folder, for __init__.py and modules alike.
        package = ".".join(path.relative_to(SRC_DIR).parent.parts)
        if violations := import_violations(path.read_text(encoding="utf-8"), package):
            found[str(path.relative_to(REPO_ROOT))] = violations

    assert found == {}


# The checker itself, against code core may contain one day (a module of hopla.core).
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import httpx", ["httpx"]),
        ("import pydantic.fields as f", ["pydantic.fields"]),
        ("from yaml import safe_load", ["yaml"]),
        ("import asyncio", ["asyncio"]),
        ("import os", ["os"]),
        ("import time", ["time"]),
        ("import _socket", ["_socket"]),
        ("from pathlib import Path", ["pathlib"]),
        ("import urllib.request", ["urllib.request"]),
        ("from urllib import parse, request", ["urllib.request"]),
        ("from http.client import HTTPSConnection", ["http.client"]),
        ("from hopla.storage import RawStore", ["hopla.storage"]),
        ("from hopla import storage", ["hopla.storage"]),
        ("from .. import storage", ["hopla.storage"]),
        ("from ...outside import x", ["...outside"]),
        ("mod = __import__('httpx')", ["__import__"]),
        ("exec('import httpx')", ["exec"]),
        ("with open('f') as f:\n    pass", ["open"]),
        ("def load(opener=open):\n    pass", ["open"]),
        ("__builtins__.open('f')", ["__builtins__"]),
        ("def f():\n    import boto3", ["boto3"]),
        ("if TYPE_CHECKING:\n    import httpx", ["httpx"]),
        ("try:\n    import yaml\nexcept ImportError:\n    yaml = None", ["yaml"]),
    ],
)
def test_checker_rejects(source: str, expected: list[str]) -> None:
    assert import_violations(source, "hopla.core") == expected


@pytest.mark.parametrize(
    "source",
    [
        "from __future__ import annotations",
        "import datetime",
        "from collections.abc import Iterable",
        "from zoneinfo import ZoneInfo",
        "from urllib.parse import quote",
        "from urllib import parse",
        "import hashlib, json, re",
        "from hopla.core.time import Clock",
        "from . import rules",
        "from .rules import evaluate",
    ],
)
def test_checker_allows(source: str) -> None:
    assert import_violations(source, "hopla.core") == []
