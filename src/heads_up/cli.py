"""Minimal CLI scaffold (Step 1).

The ``claim`` / ``check`` / ``release`` / ``list`` verbs are implemented in
Step 4. This stub only stands up the argparse surface and the console entry
point (``heads-up = heads_up.cli:main``) so the package installs and the exit
codes are wired. Every verb currently reports "not implemented yet" on stderr
and returns :data:`heads_up.models.EXIT_ERROR`.
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser
from collections.abc import Sequence

from heads_up.models import EXIT_ERROR

_VERBS: tuple[tuple[str, str], ...] = (
    ("claim", "Register an advisory claim on a resource (requires a finite TTL)."),
    ("check", "Check whether a resource is claimed."),
    ("release", "Release a claim (idempotent)."),
    ("list", "List active claims."),
)


def build_parser() -> ArgumentParser:
    """Build the top-level argument parser with the four verb subcommands."""
    parser = ArgumentParser(
        prog="heads-up",
        description="Advisory claims for parallel development work.",
    )
    parser.add_argument(
        "--ledger",
        metavar="PATH",
        help="Override the ledger file path (default: %%LOCALAPPDATA%%/heads-up/ledger.jsonl).",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="{claim,check,release,list}")
    for name, help_text in _VERBS:
        subparsers.add_parser(name, help=help_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point. Returns a process exit code (0/1/2)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    if command is None:
        parser.print_help(sys.stderr)
        return EXIT_ERROR
    print(
        f"heads-up: '{command}' is not implemented yet (arrives in Step 4).",
        file=sys.stderr,
    )
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
