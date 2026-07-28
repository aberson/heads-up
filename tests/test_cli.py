"""Smoke tests for the Step 1 CLI scaffold (verbs land in Step 4)."""

from __future__ import annotations

import pytest

from heads_up.cli import build_parser, main
from heads_up.models import EXIT_ERROR


def test_parser_exposes_all_verbs() -> None:
    parser = build_parser()
    # argparse raises SystemExit(2) on an unknown subcommand; a known one parses.
    for verb in ("claim", "check", "release", "list"):
        args = parser.parse_args([verb])
        assert args.command == verb


def test_ledger_override_flag_parses() -> None:
    args = build_parser().parse_args(["--ledger", "c:/tmp/x.jsonl", "list"])
    assert args.ledger == "c:/tmp/x.jsonl"


def test_main_without_command_returns_error() -> None:
    assert main([]) == EXIT_ERROR


def test_main_verb_stub_returns_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["claim"])
    assert code == EXIT_ERROR
    assert "not implemented yet" in capsys.readouterr().err


def test_unknown_command_exits_usage() -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["bogus"])
    assert excinfo.value.code == EXIT_ERROR  # argparse usage error == 2
