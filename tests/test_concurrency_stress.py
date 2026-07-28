"""Multi-process concurrency stress tests for the heads-up ledger (build Step 5).

Spawns MANY real OS processes (via :mod:`subprocess`, never threads) that all
attempt ``claim`` against ONE shared ``--ledger`` at the same wall-clock instant,
REPEATEDLY across rounds, and asserts the plan's append-order contract
(sections 6, 9) survives genuine OS-level lock contention:

- **Ledger stays parseable after every round** — no torn/corrupt lines; every
  physical line is a well-formed claim/release event and ``list --json`` parses.
- **Contested resource -> exactly one clean winner** — the first-appended claim
  reports clean (exit 0); every later conflicting claimant is STILL appended
  (auditable), exits 1, and its conflict evidence carries BOTH claim IDs (its own
  incoming id + the winner's existing id).
- **Winner is deterministic** — the clean claimant is exactly the first-appended
  claim in file order, regardless of interpreter-startup skew (the ``msvcrt``
  exclusive lock makes append order a total order).
- **Distinct resources -> all clean** — simultaneous claims on different resources
  all succeed with no false conflicts.

Robustness note: correctness is asserted from lock INVARIANTS (append order is
total under the exclusive lock), not from timing, so the tests are not flaky. The
wall-clock barrier in ``_concurrency_worker.py`` only maximizes how simultaneous
the race is; it is never load-bearing for the assertions. Every process targets an
isolated ``--ledger`` under ``tmp_path`` so the machine-global file is never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import heads_up
from heads_up.ledger import LedgerEvent
from heads_up.models import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_SUCCESS,
    ClaimEvent,
    ReleaseEvent,
    event_from_json_line,
)

# Absolute path to src/ so every spawned interpreter imports heads_up without an
# editable install (mirrors tests/test_cli.py and tests/test_ledger.py).
SRC_DIR = str(Path(heads_up.__file__).resolve().parents[1])
WORKER = str(Path(__file__).resolve().parent / "_concurrency_worker.py")

# Stress dimensions. "Many" real processes, "repeatedly" across rounds. Kept
# moderate so the suite stays fast while still forcing genuine contention: each
# round's critical section is a tiny read+append, far inside the ledger's ~5 s
# lock-retry budget, so no worker should ever exhaust it (exit 2).
_CONTENDERS = 8  # simultaneous claimants on ONE contested resource, per round
_DISTINCT = 8  # simultaneous claimants on DIFFERENT resources, per round
_ROUNDS = 5  # independent rounds against the same accumulating ledger

# Wall-clock barrier lead time: enough for every worker interpreter to boot and
# import before the shared fire instant. A too-short lead cannot make a test wrong
# (the lock still serializes) — it only reduces simultaneity.
_BARRIER_BASE = 0.6
_BARRIER_PER_PROC = 0.05


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _barrier_start(n: int) -> float:
    import time

    return time.time() + _BARRIER_BASE + _BARRIER_PER_PROC * n


def _spawn(
    start: float,
    ledger: Path,
    *,
    resource: str,
    owner: str,
    session: str,
    kind: str = "path",
    repository: str = "c:/repo",
    ttl: str = "1h",
) -> subprocess.Popen[str]:
    """Launch one barrier-synced claim worker; it fires at ``start``."""
    cmd = [
        sys.executable,
        WORKER,
        repr(start),
        "--ledger",
        str(ledger),
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
        "--json",
    ]
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env()
    )


def _collect(procs: list[subprocess.Popen[str]]) -> list[tuple[int, dict[str, Any], str]]:
    """Wait for every worker; return ``(returncode, parsed JSON payload, stderr)``.

    Asserts each worker emitted a single parseable JSON payload whose
    ``exit_code`` matches the real process return code — the JSON/exit-code
    contract a caller relies on.
    """
    results: list[tuple[int, dict[str, Any], str]] = []
    for proc in procs:
        out, err = proc.communicate(timeout=90)
        assert proc.returncode is not None
        payload = json.loads(out)  # raises if a worker printed anything but pure JSON
        assert payload["exit_code"] == proc.returncode, (proc.returncode, err, out)
        results.append((proc.returncode, payload, err))
    return results


def _read_ledger_events(ledger: Path) -> list[LedgerEvent]:
    """Parse every physical ledger line; fail if any line is torn/corrupt.

    Decodes the whole file as UTF-8 (invalid bytes -> failure), then parses each
    non-blank line with the production event parser: a partially-written or
    interleaved line would raise here, so a clean parse of every line IS the
    "no corruption" proof.
    """
    events: list[LedgerEvent] = []
    text = ledger.read_bytes().decode("utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        events.append(event_from_json_line(line))
    return events


def _ordered_claim_ids(events: list[LedgerEvent]) -> list[str]:
    """claim_ids in ledger append (file) order."""
    return [e.claim_id for e in events if isinstance(e, ClaimEvent)]


def _list_json(ledger: Path) -> dict[str, Any]:
    """Run ``list --json`` in a subprocess; assert it parses and exits 0."""
    proc = subprocess.run(
        [sys.executable, "-m", "heads_up.cli", "--ledger", str(ledger), "list", "--json"],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=60,
    )
    assert proc.returncode == EXIT_SUCCESS, proc.stderr
    payload: dict[str, Any] = json.loads(proc.stdout)
    return payload


def _assert_valid_uuid4(value: str) -> None:
    assert uuid.UUID(value).version == 4


# ---------------------------------------------------------------------------
# Contested resource: exactly one clean winner, every loser carries both IDs.
# ---------------------------------------------------------------------------


def test_contested_claims_one_clean_winner_rest_conflict_both_ids(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    for rnd in range(_ROUNDS):
        # A DISTINCT resource per round (so rounds are independent contests) but the
        # SAME accumulating ledger, so parseability is stressed against a growing file.
        resource = f"src/round{rnd}/contested.py"
        start = _barrier_start(_CONTENDERS)
        procs = [
            _spawn(
                start,
                ledger,
                resource=resource,
                owner=f"actor-{rnd}-{i}",  # distinct actor => genuine conflict, not supersession
                session=f"sess-{rnd}-{i}",
            )
            for i in range(_CONTENDERS)
        ]
        results = _collect(procs)

        # No worker may fail (exit 2 == lock exhaustion / error): the critical
        # section is tiny relative to the retry budget.
        assert all(rc in (EXIT_SUCCESS, EXIT_CONFLICT) for rc, _, _ in results), [
            (rc, err) for rc, _, err in results if rc == EXIT_ERROR
        ]

        clean = [p for rc, p, _ in results if rc == EXIT_SUCCESS]
        conflicting = [p for rc, p, _ in results if rc == EXIT_CONFLICT]
        # EXACTLY one clean winner; every other claimant conflicts.
        assert len(clean) == 1, [p["status"] for _, p, _ in results]
        assert len(conflicting) == _CONTENDERS - 1
        winner_id = clean[0]["claim"]["claim_id"]
        assert clean[0]["status"] == "clean"
        assert clean[0]["conflicts"] == []

        # Ledger parseable + every one of this round's claims was appended (auditable).
        events = _read_ledger_events(ledger)
        ordered = _ordered_claim_ids(events)
        round_ids = {p["claim"]["claim_id"] for _, p, _ in results}
        assert len(round_ids) == _CONTENDERS  # all claim_ids distinct
        assert round_ids <= set(ordered)  # all appended

        # Winner determinism: the clean claimant is the FIRST-appended of this round.
        first_of_round = next(cid for cid in ordered if cid in round_ids)
        assert first_of_round == winner_id

        # Every loser: exit 1, both claim IDs in evidence, winner cited as existing.
        for payload in conflicting:
            assert payload["status"] == "conflict"
            assert payload["exit_code"] == EXIT_CONFLICT
            incoming_id = payload["claim"]["claim_id"]
            assert payload["conflicts"], incoming_id
            existing_ids: set[str] = set()
            for ev in payload["conflicts"]:
                _assert_valid_uuid4(ev["existing_claim_id"])
                _assert_valid_uuid4(ev["incoming_claim_id"])
                # Incoming id is always THIS claimant; both IDs present per section 6.
                assert ev["incoming_claim_id"] == incoming_id
                assert ev["existing_claim_id"] != ev["incoming_claim_id"]
                existing_ids.add(ev["existing_claim_id"])
            # The append-order winner conflicts with everyone after it, so it must
            # appear as an existing claim in every loser's evidence.
            assert winner_id in existing_ids

        # list --json parses at every round (accumulating file stays clean).
        listed = _list_json(ledger)
        active_ids = {c["claim_id"] for c in listed["active_claims"]}
        # Advisory model: ALL contending claims stay active (none silently dropped),
        # and the clean winner is among them.
        assert round_ids <= active_ids
        assert winner_id in active_ids

    # Cumulative integrity: total appended claims == every process across all rounds.
    final_events = _read_ledger_events(ledger)
    assert len(_ordered_claim_ids(final_events)) == _ROUNDS * _CONTENDERS
    assert all(isinstance(e, ClaimEvent | ReleaseEvent) for e in final_events)


# ---------------------------------------------------------------------------
# Distinct resources: all clean, no false conflicts.
# ---------------------------------------------------------------------------


def test_distinct_resource_claims_all_clean_no_false_conflicts(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    for rnd in range(_ROUNDS):
        start = _barrier_start(_DISTINCT)
        procs = [
            _spawn(
                start,
                ledger,
                resource=f"src/round{rnd}/distinct-{i}.py",  # every resource different
                owner=f"actor-{rnd}-{i}",
                session=f"sess-{rnd}-{i}",
            )
            for i in range(_DISTINCT)
        ]
        results = _collect(procs)

        # Distinct resources never contend: every claim is clean (exit 0).
        assert all(rc == EXIT_SUCCESS for rc, _, _ in results), [
            (rc, p["status"]) for rc, p, _ in results if rc != EXIT_SUCCESS
        ]
        for _, payload, _ in results:
            assert payload["status"] == "clean"
            assert payload["conflicts"] == []

        events = _read_ledger_events(ledger)
        round_ids = {p["claim"]["claim_id"] for _, p, _ in results}
        assert round_ids <= set(_ordered_claim_ids(events))
        listed = _list_json(ledger)
        assert round_ids <= {c["claim_id"] for c in listed["active_claims"]}

    final_events = _read_ledger_events(ledger)
    assert len(_ordered_claim_ids(final_events)) == _ROUNDS * _DISTINCT


# ---------------------------------------------------------------------------
# Mixed: contested + distinct in ONE simultaneous batch (cross-talk on the lock).
# ---------------------------------------------------------------------------


def test_mixed_contested_and_distinct_simultaneous(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    contested_resource = "src/mixed/contested.py"
    n = _CONTENDERS + _DISTINCT
    start = _barrier_start(n)

    procs: list[subprocess.Popen[str]] = []
    contested_kids: list[subprocess.Popen[str]] = []
    distinct_kids: list[subprocess.Popen[str]] = []
    # Interleave spawns so the two groups genuinely race on the SAME lock.
    for i in range(max(_CONTENDERS, _DISTINCT)):
        if i < _CONTENDERS:
            p = _spawn(
                start,
                ledger,
                resource=contested_resource,
                owner=f"c-actor-{i}",
                session=f"c-sess-{i}",
            )
            procs.append(p)
            contested_kids.append(p)
        if i < _DISTINCT:
            p = _spawn(
                start,
                ledger,
                resource=f"src/mixed/distinct-{i}.py",
                owner=f"d-actor-{i}",
                session=f"d-sess-{i}",
            )
            procs.append(p)
            distinct_kids.append(p)

    contested = _collect(contested_kids)
    distinct = _collect(distinct_kids)

    # Contested group: one winner, rest conflict — even while distinct claims raced.
    assert sum(1 for rc, _, _ in contested if rc == EXIT_SUCCESS) == 1
    assert sum(1 for rc, _, _ in contested if rc == EXIT_CONFLICT) == _CONTENDERS - 1
    # Distinct group: no false conflict leaked in from the contested contention.
    assert all(rc == EXIT_SUCCESS for rc, _, _ in distinct)

    events = _read_ledger_events(ledger)
    ordered = _ordered_claim_ids(events)
    assert len(ordered) == n  # every process appended exactly one claim
    winner_payload = next(p for rc, p, _ in contested if rc == EXIT_SUCCESS)
    contested_ids = {p["claim"]["claim_id"] for _, p, _ in contested}
    first_contested = next(cid for cid in ordered if cid in contested_ids)
    assert first_contested == winner_payload["claim"]["claim_id"]

    listed = _list_json(ledger)
    assert len(listed["active_claims"]) == n  # all advisory-active, none dropped
