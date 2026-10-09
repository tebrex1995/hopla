"""Security and branch-protection invariants of the GitHub Actions workflows.

Required status checks match check names, so renaming a job silently blocks every PR.
Unpinned actions, wider token permissions or fork-triggered runs are supply-chain risks
a reviewer can miss.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.specifiers import SpecifierSet

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
WORKFLOWS = sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])
ENV_REF = re.compile(r"^\$\{\{\s*env\.(\w+)\s*\}\}$")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
READ_ONLY: tuple[dict[str, str], ...] = ({}, {"contents": "read"})
REQUIRED_CI_CHECKS = {"lint", "types", "test"}
# Both run with repository secrets on code a fork controls.
UNSAFE_TRIGGERS = {"pull_request_target", "workflow_run"}


def _load(path: Path) -> dict[Any, Any]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


def _uses(workflow: dict[Any, Any]) -> list[str]:
    """Every `uses:` of the workflow: reusable-workflow jobs and steps."""
    jobs = [job["uses"] for job in workflow["jobs"].values() if "uses" in job]
    return jobs + [step["uses"] for step in _steps(workflow) if "uses" in step]


def test_ci_workflow_is_found() -> None:
    # Guards the parametrized tests below against passing on an empty list.
    assert WORKFLOWS_DIR / "ci.yml" in WORKFLOWS


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit_sha(path: Path) -> None:
    uses = _uses(_load(path))
    assert uses, "no actions found: the parser is looking in the wrong place"
    # Local actions (./path) live in this repo and are reviewed with it.
    assert [u for u in uses if not (PINNED.match(u) or u.startswith("./"))] == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_token_is_read_only(path: Path) -> None:
    workflow = _load(path)
    assert workflow["permissions"] in READ_ONLY
    widened = {
        name: job["permissions"]
        for name, job in workflow["jobs"].items()
        if job.get("permissions", {}) not in READ_ONLY
    }
    assert widened == {}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_trigger_runs_fork_code_with_secrets(path: Path) -> None:
    workflow = _load(path)
    triggers = workflow.get("on", workflow.get(True))  # PyYAML reads the key `on` as True
    assert triggers, "no triggers found: the parser is looking in the wrong place"
    names = {triggers} if isinstance(triggers, str) else set(triggers)
    assert names & UNSAFE_TRIGGERS == set()


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_expression_is_pasted_into_a_script(path: Path) -> None:
    # `${{ }}` in `run:` is pasted into the shell before it runs (script injection);
    # values reach scripts through `env:` instead.
    scripts = [step["run"] for step in _steps(_load(path)) if "run" in step]
    assert [script for script in scripts if "${{" in script] == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_has_a_timeout(path: Path) -> None:
    jobs = _load(path)["jobs"]
    # A reusable-workflow job sets its timeouts in the called workflow.
    missing = [n for n, job in jobs.items() if "uses" not in job and "timeout-minutes" not in job]
    assert missing == []
    assert [n for n, job in jobs.items() if not 0 < job.get("timeout-minutes", 1) <= 30] == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_job_or_step_may_fail_silently(path: Path) -> None:
    jobs = _load(path)["jobs"].values()
    assert [
        s for job in jobs for s in [job, *job.get("steps", [])] if "continue-on-error" in s
    ] == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_download_is_checksum_verified(path: Path) -> None:
    steps = _steps(_load(path))
    setup_uv = [s for s in steps if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert [s for s in setup_uv if not s.get("with", {}).get("checksum")] == []
    downloads = [s["run"] for s in steps if re.search(r"\b(curl|wget)\b", s.get("run", ""))]
    assert [run for run in downloads if "sha256sum -c" not in run] == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_setup_uv_checksum_resolves_to_a_sha256(path: Path) -> None:
    # `${{ env.X }}` with X missing or empty evaluates to "", and setup-uv then skips the check.
    workflow = _load(path)
    for job in workflow["jobs"].values():
        env = {**workflow.get("env", {}), **job.get("env", {})}
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                raw = str(step["with"].get("checksum", ""))
                value = env.get(ref[1], "") if (ref := ENV_REF.match(raw)) else raw
                assert re.fullmatch(r"[0-9a-f]{64}", str(value)), step


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_check_is_neutralised(path: Path) -> None:
    runs = [s["run"] for s in _steps(_load(path)) if "run" in s]
    assert [r for r in runs if re.search(r"\|\|\s*(true|:)(\s|$)|--exit-code[ =]0", r)] == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_does_not_keep_the_token(path: Path) -> None:
    checkouts = [
        s for s in _steps(_load(path)) if s.get("uses", "").startswith("actions/checkout@")
    ]
    assert [s for s in checkouts if s.get("with", {}).get("persist-credentials") is not False] == []


def test_ci_keeps_the_required_check_names() -> None:
    # A check is named after the job's `name:`, or its id when there is none. A matrix job
    # reports one check per entry ("test (3.14)"), so it never satisfies a required name.
    jobs = _load(WORKFLOWS_DIR / "ci.yml")["jobs"]
    checks = {job.get("name", job_id) for job_id, job in jobs.items() if "strategy" not in job}
    assert REQUIRED_CI_CHECKS - checks == set()


def test_ci_uv_is_inside_the_projects_uv_range() -> None:
    # Bumping [tool.uv] required-version without PINNED_UV_VERSION would test with another uv.
    pyproject = tomllib.loads((WORKFLOWS_DIR.parents[1] / "pyproject.toml").read_text("utf-8"))
    pinned = _load(WORKFLOWS_DIR / "ci.yml")["env"]["PINNED_UV_VERSION"]
    assert pinned in SpecifierSet(pyproject["tool"]["uv"]["required-version"])


def test_ci_test_job_runs_both_layers() -> None:
    # `-m unit` alone would keep the required check green while contract tests never run.
    steps = _load(WORKFLOWS_DIR / "ci.yml")["jobs"]["test"]["steps"]
    runs = [step.get("run", "").strip() for step in steps]
    assert 'uv run --no-sync pytest -m "unit or contract"' in runs


def test_required_jobs_and_their_steps_never_skip() -> None:
    # A job or step skipped by `if:` reports success, so a required check would pass doing nothing.
    jobs = _load(WORKFLOWS_DIR / "ci.yml")["jobs"]
    required = [
        job for job_id, job in jobs.items() if job.get("name", job_id) in REQUIRED_CI_CHECKS
    ]
    assert [s for job in required for s in [job, *job["steps"]] if "if" in s] == []


def test_the_secret_scan_keeps_its_hardening() -> None:
    # gitleaks is the only secret gate in CI; each of these closes a way to scan less.
    steps = _load(WORKFLOWS_DIR / "ci.yml")["jobs"]["lint"]["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    script = next(s for s in steps if s.get("name") == "gitleaks")["run"]
    for hardening in (
        "--is-shallow-repository",
        'RANGE="${HEAD}"',
        "--ignore-gitleaks-allow",
        "--config gitleaks.toml",
        "useDefault = true",
        "--diff-merges=first-parent",
    ):
        assert hardening in script


def test_ci_reads_time_zone_data_only_from_tzdata() -> None:
    # An OS zoneinfo with other rules must not change CI's answers (ADR-0006, TS-004).
    assert _load(WORKFLOWS_DIR / "ci.yml")["env"]["PYTHONTZPATH"] == ""
