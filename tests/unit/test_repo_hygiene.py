"""Private and secret-bearing paths stay out of the public repo, even if `git add -f`-ed."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# docs/ is the private planning repo; recon/ and *.har hold raw captures with cookies.
NEVER_TRACKED = ["docs", "recon", ".env", "*.har", "*.pem", "*.key", "*.p12", "*.pfx"]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_private_paths_are_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--", *NEVER_TRACKED],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert tracked == []
