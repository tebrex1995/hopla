"""Security and branch-protection invariants of the GitHub Actions workflows.

Required status checks match check names, so renaming a job silently blocks every PR.
Unpinned actions, wider token permissions or fork-triggered runs are supply-chain risks
a reviewer can miss.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
WORKFLOWS = sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
READ_ONLY: tuple[dict[str, str], ...] = ({}, {"contents": "read"})
REQUIRED_CI_CHECKS = {"lint", "types", "test"}
# Both run with repository secrets on code a fork controls.
UNSAFE_TRIGGERS = {"pull_request_target", "workflow_run"}


def _load(path: Path) -> dict[Any, Any]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _uses(workflow: dict[Any, Any]) -> list[str]:
    """Every `uses:` of the workflow: steps and reusable-workflow jobs."""
    uses = []
    for job in workflow["jobs"].values():
        uses += [job["uses"]] if "uses" in job else []
        uses += [step["uses"] for step in job.get("steps", []) if "uses" in step]
    return uses


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
def test_every_job_has_a_timeout(path: Path) -> None:
    jobs = _load(path)["jobs"]
    # A reusable-workflow job sets its timeouts in the called workflow.
    missing = [n for n, job in jobs.items() if "uses" not in job and "timeout-minutes" not in job]
    assert missing == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_does_not_keep_the_token(path: Path) -> None:
    steps = [step for job in _load(path)["jobs"].values() for step in job.get("steps", [])]
    checkouts = [s for s in steps if s.get("uses", "").startswith("actions/checkout@")]
    assert [s for s in checkouts if s.get("with", {}).get("persist-credentials") is not False] == []


def test_ci_keeps_the_required_check_names() -> None:
    # A check is named after the job's `name:`, or its id when there is none. A matrix job
    # reports one check per entry ("test (3.14)"), so it never satisfies a required name.
    jobs = _load(WORKFLOWS_DIR / "ci.yml")["jobs"]
    checks = {job.get("name", job_id) for job_id, job in jobs.items() if "strategy" not in job}
    assert REQUIRED_CI_CHECKS - checks == set()
