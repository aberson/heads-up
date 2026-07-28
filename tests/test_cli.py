"""End-to-end CLI tests for heads_up.cli (build Step 4).

Drives the four verbs through the PRODUCTION entry point (``python -m
heads_up.cli``) in a real subprocess so the whole path — argparse, boundary
canonicalization, event construction, ledger append under the write lock, and
exit-code mapping — is exercised exactly as an operator (or an orchestrator
speaking the CLI/JSON contract) would. Every call targets an isolated
``--ledger`` under ``tmp_path`` so tests never touch the machine-global file.

The load-bearing check is ``test_canonicalize_before_construct_*``: two claims
whose resource strings are DIFFERENT raw text but the SAME canonical identity
must conflict when driven through the real CLI. Without the CLI canonicalizing
before constructing the ClaimEvent, conflicts.py's raw equality would miss it
(code-quality.md: integration test through the production caller).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import heads_up
from heads_up.cli import build_parser, main
from heads_up.models import EXIT_CONFLICT, EXIT_ERROR, EXIT_SUCCESS

# Absolute path to src/ so the spawned interpreter imports heads_up without
# relying on an editable install being visible (mirrors test_ledger.py).
SRC_DIR = str(Path(heads_up.__file__).resolve().parents[1])


def run_cli(
    *args: str, ledger: Path | None = None, ledger_after: bool = False
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m heads_up.cli`` in a subprocess with an isolated ledger.

    ``ledger`` is injected as ``--ledger`` before the verb by default (the
    canonical top-level position); ``ledger_after=True`` appends the verb's args
    verbatim so a caller can place ``--ledger`` after the verb to exercise the
    post-verb form.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "heads_up.cli"]
    if ledger is not None and not ledger_after:
        cmd += ["--ledger", str(ledger)]
    cmd += [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)


def claim(
    ledger: Path,
    *,
    resource: str = "src/foo.py",
    kind: str = "path",
    repository: str = "c:/repo",
    owner: str = "alice",
    session: str = "s1",
    ttl: str = "1h",
    as_json: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one ``claim`` verb (JSON by default) against ``ledger``."""
    args = [
        "claim",
        "--resource-kind",
        kind,
        "--resource",
        resource,
        "--repository",
        repository,
        "--owner",
        owner,
        "--session-id",
        session,
        "--ttl",
        ttl,
    ]
    if as_json:
        args.append("--json")
    return run_cli(*args, ledger=ledger)


def claim_json(ledger: Path, **kwargs: Any) -> dict[str, Any]:
    """Run a claim and return its parsed JSON payload (asserting it parsed)."""
    proc = claim(ledger, **kwargs)
    payload: dict[str, Any] = json.loads(proc.stdout)
    return payload


# ---------------------------------------------------------------------------
# claim — clean, conflict (both IDs), supersession renewal.
# ---------------------------------------------------------------------------


def test_clean_claim_exits_zero(tmp_path: Path) -> None:
    proc = claim(tmp_path / "l.jsonl")
    assert proc.returncode == EXIT_SUCCESS, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == 1
    assert payload["verb"] == "claim"
    assert payload["status"] == "clean"
    assert payload["exit_code"] == 0
    assert payload["conflicts"] == []
    assert uuid.UUID(payload["claim"]["claim_id"]).version == 4


def test_second_conflicting_claim_gets_both_ids_and_evidence(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    first = claim_json(ledger, owner="alice", session="s1")
    id_first = first["claim"]["claim_id"]

    proc = claim(ledger, owner="bob", session="s2")
    assert proc.returncode == EXIT_CONFLICT, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "conflict"
    assert payload["exit_code"] == 1
    id_second = payload["claim"]["claim_id"]

    assert len(payload["conflicts"]) == 1
    evidence = payload["conflicts"][0]
    # BOTH claim IDs are present, correctly oriented (first-appended = existing).
    assert evidence["existing_claim_id"] == id_first
    assert evidence["incoming_claim_id"] == id_second
    assert id_first != id_second
    assert evidence["reason"]  # source evidence: a non-empty overlap reason
    assert evidence["overlap"] == "exact"

    # The conflicting claim is STILL appended (advisory, auditable): both active.
    listing = json.loads(run_cli("list", "--json", ledger=ledger).stdout)
    assert listing["count"] == 2


def test_conflict_evidence_in_text_output_names_both_ids(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    first = claim_json(ledger, owner="alice", session="s1")
    id_first = first["claim"]["claim_id"]
    proc = claim(ledger, owner="bob", session="s2", as_json=False)
    assert proc.returncode == EXIT_CONFLICT, proc.stderr
    assert "CONFLICTS (1)" in proc.stdout
    assert id_first in proc.stdout
    assert "existing_claim_id" in proc.stdout
    assert "incoming_claim_id" in proc.stdout


def test_same_actor_reclaim_is_supersession_renewal(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    first = claim_json(ledger, owner="alice", session="s1")
    proc = claim(ledger, owner="alice", session="s1")
    assert proc.returncode == EXIT_SUCCESS, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "renewed"
    assert len(payload["renewed"]) == 1
    assert payload["renewed"][0]["claim_id"] == first["claim"]["claim_id"]
    # Renewal supersedes the old claim: exactly one active claim remains.
    listing = json.loads(run_cli("list", "--json", ledger=ledger).stdout)
    assert listing["count"] == 1


def test_same_owner_different_session_conflicts(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    claim_json(ledger, owner="alice", session="s1")
    proc = claim(ledger, owner="alice", session="s2")
    assert proc.returncode == EXIT_CONFLICT, proc.stderr


# ---------------------------------------------------------------------------
# Canonicalize-before-construct — the load-bearing integration test.
# ---------------------------------------------------------------------------


def test_canonicalize_before_construct_separator_and_dotdot(tmp_path: Path) -> None:
    """Alias resources (separator + ``..``) conflict through the real CLI.

    Raw strings differ (``src/lib/mod.py`` vs ``src\\lib\\..\\lib\\mod.py``); both
    canonicalize to ``src/lib/mod.py``. Platform-independent: backslash->slash and
    ``..`` collapse happen on every OS, so this proves the CLI canonicalizes the
    resource BEFORE constructing the ClaimEvent regardless of the fold rule.
    """
    ledger = tmp_path / "l.jsonl"
    first = claim_json(ledger, resource="src/lib/mod.py", owner="alice", session="s1")
    proc = claim(ledger, resource="src\\lib\\..\\lib\\mod.py", owner="bob", session="s2")
    assert proc.returncode == EXIT_CONFLICT, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    evidence = payload["conflicts"][0]
    assert evidence["existing_claim_id"] == first["claim"]["claim_id"]
    assert evidence["incoming_claim_id"] == payload["claim"]["claim_id"]
    # Both stored resources are the SAME canonical string (proof of canonicalization).
    assert evidence["existing_resource"] == evidence["incoming_resource"] == "src/lib/mod.py"


@pytest.mark.skipif(os.name != "nt", reason="case-fold identity only holds on Windows")
def test_canonicalize_before_construct_case_and_separator_windows(tmp_path: Path) -> None:
    """The named example: ``Src\\Foo.py`` vs ``src/foo.py`` conflict on Windows."""
    ledger = tmp_path / "l.jsonl"
    claim_json(ledger, resource="Src\\Foo.py", owner="alice", session="s1")
    proc = claim(ledger, resource="src/foo.py", owner="bob", session="s2")
    assert proc.returncode == EXIT_CONFLICT, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["conflicts"][0]["existing_resource"] == "src/foo.py"


def test_ancestor_descendant_paths_conflict_through_cli(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    claim_json(ledger, resource="src", owner="alice", session="s1")
    proc = claim(ledger, resource="src/deep/mod.py", owner="bob", session="s2")
    assert proc.returncode == EXIT_CONFLICT, proc.stderr
    assert json.loads(proc.stdout)["conflicts"][0]["overlap"] in ("ancestor", "descendant")


def test_different_repository_does_not_conflict(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    claim_json(ledger, repository="c:/repo-a", owner="alice", session="s1")
    proc = claim(ledger, repository="c:/repo-b", owner="bob", session="s2")
    assert proc.returncode == EXIT_SUCCESS, proc.stderr


def test_expired_claim_does_not_block_new_claim_through_cli(tmp_path: Path) -> None:
    """An expired active claim never blocks a new claim on the same resource.

    Plan section 6: expired claims never block. Driven end-to-end: alice takes a
    1-second claim, it lapses, then bob (a different actor) claims the SAME
    resource and gets a CLEAN receipt (exit 0) instead of a conflict (exit 1),
    leaving exactly one active claim.
    """
    ledger = tmp_path / "l.jsonl"
    first = claim_json(ledger, resource="src/foo.py", owner="alice", session="s1", ttl="1s")
    assert first["status"] == "clean"

    time.sleep(1.5)  # let alice's 1s claim expire

    proc = claim(ledger, resource="src/foo.py", owner="bob", session="s2", ttl="1h")
    assert proc.returncode == EXIT_SUCCESS, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["status"] == "clean"
    assert payload["conflicts"] == []
    # Only bob's claim is active; alice's expired one is inactive (but auditable).
    listing = json.loads(run_cli("list", "--json", ledger=ledger).stdout)
    assert listing["count"] == 1
    assert listing["active_claims"][0]["owner"] == "bob"


# ---------------------------------------------------------------------------
# check — no append; clean / would-conflict / already-held.
# ---------------------------------------------------------------------------


def check(
    ledger: Path,
    *,
    resource: str = "src/foo.py",
    owner: str = "carol",
    session: str = "s9",
    kind: str = "path",
    repository: str = "c:/repo",
) -> subprocess.CompletedProcess[str]:
    return run_cli(
        "check",
        "--resource-kind",
        kind,
        "--resource",
        resource,
        "--repository",
        repository,
        "--owner",
        owner,
        "--session-id",
        session,
        "--json",
        ledger=ledger,
    )


def test_check_clean_when_unclaimed(tmp_path: Path) -> None:
    proc = check(tmp_path / "l.jsonl")
    assert proc.returncode == EXIT_SUCCESS, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["verb"] == "check"
    assert payload["claimed"] is False
    assert payload["would_conflict"] is False


def test_check_would_conflict_reports_evidence_without_appending(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    first = claim_json(ledger, owner="alice", session="s1")
    proc = check(ledger, owner="bob", session="s2")
    assert proc.returncode == EXIT_CONFLICT, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["would_conflict"] is True
    assert payload["conflicts"][0]["existing_claim_id"] == first["claim"]["claim_id"]
    # check did NOT append: still exactly one active claim.
    listing = json.loads(run_cli("list", "--json", ledger=ledger).stdout)
    assert listing["count"] == 1


def test_check_reports_own_active_claim_as_held(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    claim_json(ledger, owner="alice", session="s1")
    proc = check(ledger, owner="alice", session="s1")
    assert proc.returncode == EXIT_SUCCESS, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["claimed"] is True
    assert payload["would_conflict"] is False
    assert len(payload["held_by_you"]) == 1


# ---------------------------------------------------------------------------
# release — deterministic apply, idempotent no-op, unknown/malformed = exit 2.
# ---------------------------------------------------------------------------


def test_release_is_deterministic(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    made = claim_json(ledger, owner="alice", session="s1")
    claim_id = made["claim"]["claim_id"]

    first = run_cli("release", "--claim-id", claim_id, "--owner", "alice", "--json", ledger=ledger)
    assert first.returncode == EXIT_SUCCESS, first.stderr
    assert json.loads(first.stdout)["status"] == "applied"

    # Released -> gone from the active set.
    assert json.loads(run_cli("list", "--json", ledger=ledger).stdout)["count"] == 0

    # Idempotent: releasing again is a no-op, still exit 0.
    second = run_cli("release", "--claim-id", claim_id, "--owner", "alice", "--json", ledger=ledger)
    assert second.returncode == EXIT_SUCCESS, second.stderr
    assert json.loads(second.stdout)["status"] == "noop-inactive"


def test_release_unknown_claim_id_is_error(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    unknown = str(uuid.uuid4())  # valid UUIDv4 shape, absent from the ledger
    proc = run_cli("release", "--claim-id", unknown, "--owner", "alice", ledger=ledger)
    assert proc.returncode == EXIT_ERROR
    assert "unknown" in proc.stderr.lower()


def test_release_malformed_claim_id_is_error(tmp_path: Path) -> None:
    proc = run_cli(
        "release", "--claim-id", "not-a-uuid", "--owner", "alice", ledger=tmp_path / "l.jsonl"
    )
    assert proc.returncode == EXIT_ERROR


def test_release_owner_must_match_claim_owner(tmp_path: Path) -> None:
    """Releasing another actor's claim is a usage error (exit 2), never a success.

    ReleaseEvent records the owner but the ledger applies releases by claim_id
    alone; the CLI enforces that the release --owner matches the claim's owner so
    one session cannot accidentally release another's claim.
    """
    ledger = tmp_path / "l.jsonl"
    made = claim_json(ledger, owner="alice", session="s1")
    claim_id = made["claim"]["claim_id"]

    # bob tries to release alice's claim -> rejected, and the claim stays active.
    proc = run_cli("release", "--claim-id", claim_id, "--owner", "bob", ledger=ledger)
    assert proc.returncode == EXIT_ERROR, proc.stdout
    assert "does not match" in proc.stderr.lower()
    assert json.loads(run_cli("list", "--json", ledger=ledger).stdout)["count"] == 1

    # The rightful owner can still release it (exit 0).
    ok = run_cli("release", "--claim-id", claim_id, "--owner", "alice", "--json", ledger=ledger)
    assert ok.returncode == EXIT_SUCCESS, ok.stderr
    assert json.loads(ok.stdout)["status"] == "applied"


# ---------------------------------------------------------------------------
# list + JSON shape + --ledger placement.
# ---------------------------------------------------------------------------


def test_list_empty_and_populated(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    empty = run_cli("list", "--json", ledger=ledger)
    assert empty.returncode == EXIT_SUCCESS
    assert json.loads(empty.stdout)["count"] == 0

    claim_json(ledger, resource="a.py", owner="alice", session="s1")
    claim_json(ledger, resource="b.py", owner="bob", session="s2")
    populated = run_cli("list", "--json", ledger=ledger)
    assert json.loads(populated.stdout)["count"] == 2


def test_list_text_output(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    claim_json(ledger, resource="a.py", owner="alice", session="s1")
    proc = run_cli("list", ledger=ledger)
    assert proc.returncode == EXIT_SUCCESS
    assert "ACTIVE CLAIMS (1)" in proc.stdout


def test_ledger_flag_accepted_after_verb(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    claim_json(ledger, owner="alice", session="s1")
    # --ledger AFTER the verb must resolve the same isolated file.
    proc = run_cli("list", "--json", "--ledger", str(ledger), ledger_after=True)
    assert proc.returncode == EXIT_SUCCESS, proc.stderr
    assert json.loads(proc.stdout)["count"] == 1


def test_claim_text_shows_local_and_utc(tmp_path: Path) -> None:
    proc = claim(tmp_path / "l.jsonl", as_json=False)
    assert proc.returncode == EXIT_SUCCESS, proc.stderr
    assert "(UTC)" in proc.stdout
    assert "(local)" in proc.stdout
    assert "expires_at:" in proc.stdout


# ---------------------------------------------------------------------------
# Exit-code discipline: usage errors (missing/bad args) -> exit 2.
# ---------------------------------------------------------------------------


def test_claim_requires_expiry(tmp_path: Path) -> None:
    """A claim with neither --ttl nor --expires-at is a usage error (exit 2)."""
    proc = run_cli(
        "claim",
        "--resource-kind",
        "path",
        "--resource",
        "src/foo.py",
        "--repository",
        "c:/repo",
        "--owner",
        "alice",
        "--session-id",
        "s1",
        ledger=tmp_path / "l.jsonl",
    )
    assert proc.returncode == EXIT_ERROR


def test_claim_bad_ttl_is_usage_error(tmp_path: Path) -> None:
    proc = claim(tmp_path / "l.jsonl", ttl="banana")
    assert proc.returncode == EXIT_ERROR
    assert "ttl" in proc.stderr.lower()


def test_claim_missing_required_resource_is_usage_error(tmp_path: Path) -> None:
    proc = run_cli(
        "claim",
        "--resource-kind",
        "path",
        "--repository",
        "c:/repo",
        "--owner",
        "alice",
        "--session-id",
        "s1",
        "--ttl",
        "1h",
        ledger=tmp_path / "l.jsonl",
    )
    assert proc.returncode == EXIT_ERROR


def test_path_escaping_repository_is_error(tmp_path: Path) -> None:
    proc = claim(tmp_path / "l.jsonl", resource="../outside.py")
    assert proc.returncode == EXIT_ERROR


def test_expires_at_absolute_iso_accepted(tmp_path: Path) -> None:
    proc = run_cli(
        "claim",
        "--resource-kind",
        "path",
        "--resource",
        "src/foo.py",
        "--repository",
        "c:/repo",
        "--owner",
        "alice",
        "--session-id",
        "s1",
        "--expires-at",
        "2099-01-01T00:00:00+00:00",
        "--json",
        ledger=tmp_path / "l.jsonl",
    )
    assert proc.returncode == EXIT_SUCCESS, proc.stderr
    assert json.loads(proc.stdout)["claim"]["expires_at"] == "2099-01-01T00:00:00+00:00"


def test_ttl_and_expires_at_are_mutually_exclusive(tmp_path: Path) -> None:
    proc = run_cli(
        "claim",
        "--resource-kind",
        "path",
        "--resource",
        "src/foo.py",
        "--repository",
        "c:/repo",
        "--owner",
        "alice",
        "--session-id",
        "s1",
        "--ttl",
        "1h",
        "--expires-at",
        "2099-01-01T00:00:00+00:00",
        ledger=tmp_path / "l.jsonl",
    )
    assert proc.returncode == EXIT_ERROR


def test_over_range_ttl_is_usage_error_not_conflict(tmp_path: Path) -> None:
    """An over-range --ttl overflows timedelta -> clean exit 2, NOT exit 1.

    Exit 1 is the advisory-conflict code; a malformed-input crash must never
    masquerade as a resource conflict to an orchestrator dispatching on exit
    codes. The failure must be a clean usage error with no traceback.
    """
    proc = claim(tmp_path / "l.jsonl", ttl="9999999999999d")
    assert proc.returncode == EXIT_ERROR, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.returncode != EXIT_CONFLICT
    assert "Traceback" not in proc.stderr
    assert "ttl" in proc.stderr.lower()


def test_huge_int_ttl_is_usage_error_not_conflict(tmp_path: Path) -> None:
    """A TTL digit run past Python's int-string limit is exit 2, not a traceback."""
    huge = "9" * 5000 + "s"  # exceeds sys.get_int_max_str_digits (default 4300)
    proc = claim(tmp_path / "l.jsonl", ttl=huge)
    assert proc.returncode == EXIT_ERROR, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.returncode != EXIT_CONFLICT
    assert "Traceback" not in proc.stderr
    assert "ttl" in proc.stderr.lower()


def test_extreme_far_future_expires_at_does_not_crash(tmp_path: Path) -> None:
    """A max-range --expires-at succeeds and renders without a traceback.

    The claim is persisted BEFORE its receipt is rendered, so an overflow while
    shifting the far-future stamp into local time must fall back to a UTC-only
    display rather than crash a claim that already succeeded (exit 1 + traceback).
    """
    proc = run_cli(
        "claim",
        "--resource-kind",
        "path",
        "--resource",
        "src/foo.py",
        "--repository",
        "c:/repo",
        "--owner",
        "alice",
        "--session-id",
        "s1",
        "--expires-at",
        "9999-12-31T23:59:59+00:00",
        ledger=tmp_path / "l.jsonl",
    )
    assert proc.returncode == EXIT_SUCCESS, (proc.returncode, proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr
    assert "expires_at:" in proc.stdout
    assert "(UTC)" in proc.stdout


# ---------------------------------------------------------------------------
# In-process parser smoke (no subprocess) — cheap structural checks.
# ---------------------------------------------------------------------------


def test_main_without_command_returns_error() -> None:
    assert main([]) == EXIT_ERROR


def test_ledger_override_flag_parses() -> None:
    args = build_parser().parse_args(["--ledger", "c:/tmp/x.jsonl", "list"])
    assert args.ledger == "c:/tmp/x.jsonl"


def test_unknown_command_exits_usage() -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["bogus"])
    assert excinfo.value.code == EXIT_ERROR


def test_no_command_subprocess_exits_error(tmp_path: Path) -> None:
    proc = run_cli(ledger=tmp_path / "l.jsonl")
    assert proc.returncode == EXIT_ERROR


# ---------------------------------------------------------------------------
# Installed console-script entry point (`heads-up`) — resolves + runs a verb.
# ---------------------------------------------------------------------------


def _installed_console_script() -> str | None:
    """Locate the installed ``heads-up`` console script, or None if not installed.

    Looks next to the running interpreter first (the venv Scripts/bin dir, where
    `project.scripts` lands it), then falls back to PATH.
    """
    bindir = Path(sys.executable).parent
    for name in ("heads-up.exe", "heads-up"):
        candidate = bindir / name
        if candidate.exists():
            return str(candidate)
    return shutil.which("heads-up")


def test_installed_console_script_resolves_and_runs(tmp_path: Path) -> None:
    """The `project.scripts` entry point resolves and runs a verb end-to-end.

    Exercises the ACTUAL installed `heads-up` command (not `python -m
    heads_up.cli`), proving `heads_up.cli:main` is wired as the console script.
    """
    script = _installed_console_script()
    if script is None:
        pytest.skip("heads-up console script is not installed in this environment")
    proc = subprocess.run(
        [script, "list", "--json", "--ledger", str(tmp_path / "l.jsonl")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == EXIT_SUCCESS, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["verb"] == "list"
    assert payload["count"] == 0
