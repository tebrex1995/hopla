"""The `hopla` command line.

Subcommands arrive with the tasks that implement them (`tick` in T-R0-06,
then `ingest`, `reparse`, `replay`, ...). Until then the CLI only offers
`--help` and `--version`, so an unknown command fails with exit code 2.
"""

import argparse
from collections.abc import Sequence
from importlib.metadata import version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hopla",
        description="Serbian train + bus planner with change alerts.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('hopla')}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)  # --help/--version exit 0; unknown arguments exit 2.
    parser.print_help()
    return 0
