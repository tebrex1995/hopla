"""Nothing .gitignore excludes is tracked, even if it was added with `git add -f`.

.gitignore lists the private and secret-bearing paths: the private planning repo (docs/),
raw recon captures, HAR files, keys and .env files.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_no_ignored_file_is_tracked() -> None:
    tracked_but_ignored = subprocess.run(
        ["git", "ls-files", "--cached", "--ignored", "--exclude-standard", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")

    assert [path for path in tracked_but_ignored if path] == []


# The test above trusts .gitignore; this pins that .gitignore really covers the private paths.
MUST_BE_IGNORED = [
    "docs/plan.md",
    "recon/raw.json",
    "sub/recon/raw.json",
    ".env",
    "config/.env",
    ".env.local",
    ".envrc",
    ".netrc",
    "cookies.txt",
    "x/capture.har",
    "k.pem",
    "k.key",
    "c.p12",
    "c.pfx",
    ".gitleaks.toml",
    ".gitleaksignore",
]


def test_gitignore_covers_every_private_path() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--", *MUST_BE_IGNORED, ".env.example"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert ignored == MUST_BE_IGNORED  # and .env.example stays trackable
