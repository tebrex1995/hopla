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
