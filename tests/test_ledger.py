"""Storage tests for heads_up.ledger (build Step 3).

Exercises the append-only ledger end to end: path resolution, atomic append,
replay into active state (released / expired / superseded stay auditable but
inactive), release semantics, fail-loud corruption diagnostics, bounded-retry
lock exhaustion, and — the load-bearing check — real concurrent writers that
serialize through the sidecar lock without corrupting a record.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import heads_up
from heads_up import ledger
from heads_up.ledger import (
    LedgerCorruptionError,
    LockTimeoutError,
    UnknownClaimError,
    active_claims,
    append_claim,
    append_release,
    default_ledger_path,
    derive_active_claims,
    read_events,
    resolve_ledger_path,
)
from heads_up.models import ClaimEvent, ReleaseEvent, ReleaseOutcome

FUTURE = "2099-01-01T00:00:00+00:00"
PAST_CREATED = "2020-01-01T00:00:00+00:00"
PAST_EXPIRES = "2020-01-01T01:00:00+00:00"

# Absolute path to the src/ dir so spawned worker processes can import heads_up
# even without relying on an editable install being visible to sys.executable.
SRC_DIR = str(Path(heads_up.__file__).resolve().parents[1])


def make_claim(
    resource: str = "src",
    *,
    kind: str = "path",
    repository: str = "c:/repo",
    owner: str = "alice",
    session: str = "s1",
    created_at: str | None = None,
    expires_at: str = FUTURE,
) -> ClaimEvent:
    return ClaimEvent.create(
        resource_kind=kind,
        resource=resource,
        repository=repository,
        owner=owner,
        session_id=session,
        created_at=created_at,
        expires_at=expires_at,
    )


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.jsonl"


# ---------------------------------------------------------------------------
# Path resolution — one machine-global file, override, no per-repo key.
# ---------------------------------------------------------------------------


def test_resolve_override_wins(tmp_path: Path) -> None:
    override = tmp_path / "custom.jsonl"
    assert resolve_ledger_path(override) == override
    assert resolve_ledger_path(str(override)) == override


def test_default_path_is_single_localappdata_file(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":
        pytest.skip("LOCALAPPDATA layout is Windows-specific")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    path = default_ledger_path()
    assert path == Path(r"C:\Users\someone\AppData\Local") / "heads-up" / "ledger.jsonl"


def test_default_path_has_no_per_repo_key() -> None:
    # All sessions/worktrees resolve the same file: default is repo-independent and
    # resolve_ledger_path(None) is stable across calls.
    assert resolve_ledger_path(None) == default_ledger_path()
    assert default_ledger_path() == default_ledger_path()


# ---------------------------------------------------------------------------
# First-write directory creation + advisory (non-existent) resources.
# ---------------------------------------------------------------------------


def test_ledger_dir_created_on_first_write(tmp_path: Path) -> None:
    nested = tmp_path / "does" / "not" / "exist" / "ledger.jsonl"
    assert not nested.parent.exists()
    result = append_claim(make_claim(), ledger_path=nested)
    assert result.exit_code == 0
    assert nested.is_file()
    assert nested.parent.is_dir()


def test_read_missing_ledger_is_empty_and_creates_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "ledger.jsonl"
    assert read_events(missing) == []
    assert not missing.parent.exists()  # a pure read must not create the dir/sidecar


def test_claim_on_nonexistent_path_is_advisory(ledger_path: Path) -> None:
    # Claims are advisory intent over paths that may not exist yet; nothing touches disk.
    claim = make_claim(resource="totally/made/up/file.py")
    result = append_claim(claim, ledger_path=ledger_path)
    assert result.exit_code == 0
    active = active_claims(ledger_path)
    assert [c.resource for c in active] == ["totally/made/up/file.py"]


# ---------------------------------------------------------------------------
# Replay: released / expired stay auditable-but-inactive.
# ---------------------------------------------------------------------------


def test_clean_claim_becomes_active(ledger_path: Path) -> None:
    result = append_claim(make_claim(), ledger_path=ledger_path)
    assert result.is_clean
    active = active_claims(ledger_path)
    assert len(active) == 1
    assert active[0].claim_id == result.claim.claim_id


def test_released_claim_inactive_but_auditable(ledger_path: Path) -> None:
    claim = make_claim(owner="alice")
    append_claim(claim, ledger_path=ledger_path)
    assert len(active_claims(ledger_path)) == 1

    result = append_release(
        ReleaseEvent.create(claim_id=claim.claim_id, owner="alice"), ledger_path=ledger_path
    )
    assert result.outcome is ReleaseOutcome.APPLIED
    assert result.exit_code == 0

    assert active_claims(ledger_path) == []  # inactive
    events = read_events(ledger_path)
    assert len(events) == 2  # claim + release both remain on disk (auditable)


def test_expired_claim_inactive_but_auditable(ledger_path: Path) -> None:
    expired = make_claim(created_at=PAST_CREATED, expires_at=PAST_EXPIRES)
    append_claim(expired, ledger_path=ledger_path)
    assert active_claims(ledger_path) == []  # expires_at is well in the past
    events = read_events(ledger_path)
    assert len(events) == 1 and isinstance(events[0], ClaimEvent)
    assert events[0].claim_id == expired.claim_id  # still auditable


def test_expiry_boundary_uses_injected_now(ledger_path: Path) -> None:
    claim = make_claim(
        created_at="2026-01-01T00:00:00+00:00", expires_at="2026-01-01T02:00:00+00:00"
    )
    append_claim(claim, ledger_path=ledger_path)
    # Before expiry -> active; at/after expiry -> inactive (auditable).
    assert len(active_claims(ledger_path, now="2026-01-01T01:00:00+00:00")) == 1
    assert active_claims(ledger_path, now="2026-01-01T02:00:00+00:00") == []


# ---------------------------------------------------------------------------
# Supersession renewal (same actor) vs conflict (different actor).
# ---------------------------------------------------------------------------


def test_same_actor_reclaim_is_supersession_renewal(ledger_path: Path) -> None:
    first = make_claim(resource="src", owner="alice", session="s1")
    r1 = append_claim(first, ledger_path=ledger_path)
    assert r1.is_clean

    second = make_claim(resource="src", owner="alice", session="s1")  # same actor + resource
    r2 = append_claim(second, ledger_path=ledger_path)
    assert r2.exit_code == 0  # renewal, not a self-conflict
    assert r2.is_clean
    assert [c.claim_id for c in r2.renewed] == [first.claim_id]

    active = active_claims(ledger_path)
    assert [c.claim_id for c in active] == [second.claim_id]  # first superseded, inactive
    assert len(read_events(ledger_path)) == 2  # both claim events remain auditable


def test_conflicting_claim_appended_with_both_ids(ledger_path: Path) -> None:
    first = make_claim(resource="src", owner="alice", session="s1")
    r1 = append_claim(first, ledger_path=ledger_path)
    assert r1.exit_code == 0  # first-appended wins a clean receipt

    second = make_claim(resource="src", owner="bob", session="s2")  # different actor, same resource
    r2 = append_claim(second, ledger_path=ledger_path)
    assert r2.exit_code == 1  # later conflicting claim
    assert len(r2.conflicts) == 1
    evidence = r2.conflicts[0]
    assert evidence.existing_claim_id == first.claim_id  # both claim IDs carried as evidence
    assert evidence.incoming_claim_id == second.claim_id

    # The conflicting claim is STILL appended (auditable) and BOTH remain active (advisory).
    events = read_events(ledger_path)
    assert len([e for e in events if isinstance(e, ClaimEvent)]) == 2
    active_ids = {c.claim_id for c in derive_active_claims(events)}
    assert active_ids == {first.claim_id, second.claim_id}


def test_ancestor_path_conflict_across_actors(ledger_path: Path) -> None:
    parent = make_claim(resource="src", owner="alice", session="s1")
    append_claim(parent, ledger_path=ledger_path)
    child = make_claim(resource="src/pkg/mod.py", owner="bob", session="s2")
    result = append_claim(child, ledger_path=ledger_path)
    assert result.exit_code == 1  # component-boundary ancestor overlap (Step-2 rule)
    assert result.conflicts[0].existing_claim_id == parent.claim_id


# ---------------------------------------------------------------------------
# Release semantics: unknown -> error; already-inactive -> idempotent no-op.
# ---------------------------------------------------------------------------


def test_release_unknown_claim_raises(ledger_path: Path) -> None:
    append_claim(make_claim(), ledger_path=ledger_path)
    stranger = ReleaseEvent.create(claim_id="00000000-0000-4000-8000-000000000000", owner="alice")
    before = read_events(ledger_path)
    with pytest.raises(UnknownClaimError):
        append_release(stranger, ledger_path=ledger_path)
    assert read_events(ledger_path) == before  # nothing appended on the error path


def test_release_already_released_is_idempotent_noop(ledger_path: Path) -> None:
    claim = make_claim(owner="alice")
    append_claim(claim, ledger_path=ledger_path)
    append_release(
        ReleaseEvent.create(claim_id=claim.claim_id, owner="alice"), ledger_path=ledger_path
    )
    after_first = read_events(ledger_path)

    again = append_release(
        ReleaseEvent.create(claim_id=claim.claim_id, owner="alice"), ledger_path=ledger_path
    )
    assert again.outcome is ReleaseOutcome.NOOP_INACTIVE
    assert again.exit_code == 0
    assert read_events(ledger_path) == after_first  # true no-op: no duplicate release written


def test_release_of_expired_claim_is_noop(ledger_path: Path) -> None:
    expired = make_claim(owner="alice", created_at=PAST_CREATED, expires_at=PAST_EXPIRES)
    append_claim(expired, ledger_path=ledger_path)
    result = append_release(
        ReleaseEvent.create(claim_id=expired.claim_id, owner="alice"), ledger_path=ledger_path
    )
    assert result.outcome is ReleaseOutcome.NOOP_INACTIVE
    assert result.exit_code == 0
    assert len(read_events(ledger_path)) == 1  # only the original claim; no release appended


# ---------------------------------------------------------------------------
# Corruption diagnostics — FAIL LOUD with a file + line number.
# ---------------------------------------------------------------------------


def _good_line() -> str:
    return make_claim().to_json_line()


def test_torn_line_diagnosed_with_line_number(ledger_path: Path) -> None:
    # Line 2 is a truncated (torn) claim record.
    ledger_path.write_text(
        _good_line() + "\n" + '{"event":"claim","claim_id":' + "\n", encoding="utf-8"
    )
    with pytest.raises(LedgerCorruptionError) as exc_info:
        read_events(ledger_path)
    err = exc_info.value
    assert err.line_number == 2
    assert err.path == ledger_path
    assert str(ledger_path) in str(err)  # file-specific diagnostic


def test_non_json_garbage_line_diagnosed(ledger_path: Path) -> None:
    ledger_path.write_text(_good_line() + "\n" + "this is not json at all\n", encoding="utf-8")
    with pytest.raises(LedgerCorruptionError) as exc_info:
        read_events(ledger_path)
    assert exc_info.value.line_number == 2


def test_valid_json_wrong_schema_line_diagnosed(ledger_path: Path) -> None:
    # Well-formed JSON but not a known event -> still corruption, not a silent skip.
    ledger_path.write_text('{"event":"bogus","x":1}\n', encoding="utf-8")
    with pytest.raises(LedgerCorruptionError) as exc_info:
        read_events(ledger_path)
    assert exc_info.value.line_number == 1


def test_blank_lines_are_skipped_not_corrupt(ledger_path: Path) -> None:
    ledger_path.write_text(_good_line() + "\n\n   \n", encoding="utf-8")
    events = read_events(ledger_path)  # trailing blank/whitespace lines carry no event
    assert len(events) == 1


def test_corrupt_ledger_blocks_append(ledger_path: Path) -> None:
    ledger_path.write_text(_good_line() + "\n" + "torn{\n", encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        append_claim(make_claim(resource="other"), ledger_path=ledger_path)


def test_valid_json_schema_invalid_claim_id_diagnosed(ledger_path: Path) -> None:
    # Finding 1: well-formed JSON, correct event tag, all fields present -- but a
    # bad claim_id. That raises InvalidClaimIdError (a SchemaError subclass, NOT a
    # MalformedEventError), yet it must STILL surface as a file+line
    # LedgerCorruptionError, never escape as a raw SchemaError.
    bad = make_claim().to_dict()
    bad["claim_id"] = "not-a-uuid"
    ledger_path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(LedgerCorruptionError) as exc_info:
        read_events(ledger_path)
    assert exc_info.value.line_number == 1
    assert str(ledger_path) in str(exc_info.value)  # file-specific diagnostic


def test_valid_json_empty_resource_diagnosed(ledger_path: Path) -> None:
    # Finding 1 (second corruption class): empty resource -> InvalidResourceError.
    bad = make_claim().to_dict()
    bad["resource"] = ""
    ledger_path.write_text(_good_line() + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(LedgerCorruptionError) as exc_info:
        read_events(ledger_path)
    assert exc_info.value.line_number == 2  # the corrupt line is named, not line 1


def test_schema_invalid_line_blocks_append(ledger_path: Path) -> None:
    # Finding 1: a schema-invalid existing line must also refuse an append (do not
    # build on top of corruption), same as a torn line does.
    bad = make_claim().to_dict()
    bad["expires_at"] = bad["created_at"]  # non-positive TTL -> NonFiniteTTLError
    ledger_path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        append_claim(make_claim(resource="other"), ledger_path=ledger_path)


def test_read_events_at_tolerates_deleted_file(ledger_path: Path) -> None:
    # Finding 3 (TOCTOU): a racing deleter can remove the ledger between the
    # exists() check and the locked read. That degrades to an empty ledger rather
    # than raising FileNotFoundError.
    assert ledger._read_events_at(ledger_path) == []


def test_oversize_line_is_diagnosed_not_oom(
    ledger_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding 4 (OOM guard): a single newline-less giant line is capped and
    # diagnosed as corruption, never read into memory whole.
    monkeypatch.setattr(ledger, "MAX_LINE_BYTES", 64)
    ledger_path.write_text("x" * 500, encoding="utf-8")  # 500 bytes, no newline
    with pytest.raises(LedgerCorruptionError) as exc_info:
        read_events(ledger_path)
    assert exc_info.value.line_number == 1
    assert "cap" in str(exc_info.value)


def test_non_contention_oserror_raises_immediately(
    ledger_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding 2 (lock-retry precision): a genuine non-contention OSError must surface
    # immediately as a clean ledger error -- NOT be retried for ~5s and misreported
    # as a LockTimeoutError.
    def _boom(fd: int) -> None:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(ledger, "_lock_exclusive", _boom)
    start = time.monotonic()
    with pytest.raises(ledger.LedgerError) as exc_info:
        append_claim(make_claim(), ledger_path=ledger_path)
    assert not isinstance(exc_info.value, LockTimeoutError)  # not a timeout misreport
    assert time.monotonic() - start < 1.0  # returned at once, no ~5s retry loop


# ---------------------------------------------------------------------------
# Bounded-retry lock exhaustion -> ledger error (exit 2).
# ---------------------------------------------------------------------------


def test_lock_retry_exhaustion_raises(ledger_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger, "LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(ledger, "LOCK_POLL_INTERVAL", 0.02)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = ledger_path.with_name(ledger_path.name + ".lock")

    # Hold the exclusive lock via a separate handle, mimicking a stuck concurrent writer.
    fd = os.open(str(sidecar), os.O_RDWR | os.O_CREAT, 0o600)
    ledger._lock_exclusive(fd)
    try:
        with pytest.raises(LockTimeoutError):
            append_claim(make_claim(), ledger_path=ledger_path)
    finally:
        ledger._unlock(fd)
        os.close(fd)


# ---------------------------------------------------------------------------
# Load-bearing concurrency: real writers serialize, no corruption.
# ---------------------------------------------------------------------------

_WORKER = """\
import sys

src, ledger, owner, session, prefix, count = sys.argv[1:7]
sys.path.insert(0, src)
from heads_up.ledger import append_claim
from heads_up.models import ClaimEvent

codes = []
try:
    for i in range(int(count)):
        claim = ClaimEvent.create(
            resource_kind="path",
            resource=prefix + "/" + str(i),
            repository="c:/repo",
            owner=owner,
            session_id=session,
            expires_at="2099-01-01T00:00:00+00:00",
        )
        result = append_claim(claim, ledger_path=ledger)
        codes.append(result.exit_code)
        for ev in result.conflicts:
            print("CONFLICT", ev.incoming_claim_id, ev.existing_claim_id)
        if result.is_clean:
            print("CLEAN", claim.claim_id)
except Exception as exc:
    # A LockTimeoutError (or any other failure) exits with a DISTINCT code and no
    # CONFLICT line, so the race test can never mistake a spurious timeout for a
    # genuine conflict -- both would otherwise surface as a bare exit 1.
    print("ERROR", type(exc).__name__, exc)
    sys.exit(3)
sys.exit(max(codes) if codes else 0)
"""


def _spawn_workers(
    worker: Path, ledger_path: Path, specs: list[tuple[str, str, str, int]]
) -> list[subprocess.CompletedProcess[str]]:
    """Launch every worker concurrently (all Popen first), then collect results."""
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                str(worker),
                SRC_DIR,
                str(ledger_path),
                owner,
                session,
                prefix,
                str(n),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        for owner, session, prefix, n in specs
    ]
    results: list[subprocess.CompletedProcess[str]] = []
    for proc in procs:
        out, err = proc.communicate(timeout=90)
        results.append(subprocess.CompletedProcess(proc.args, proc.returncode, out, err))
    return results


def test_concurrent_processes_distinct_no_corruption(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(_WORKER, encoding="utf-8")
    ledger_path = tmp_path / "ledger.jsonl"

    workers = 5
    per_worker = 4
    specs = [(f"owner{k}", f"s{k}", f"owner{k}", per_worker) for k in range(workers)]
    results = _spawn_workers(worker, ledger_path, specs)

    for res in results:
        assert res.returncode == 0, f"worker failed: {res.stderr}"

    # No corruption: every appended line parses cleanly, and the count is exact.
    events = read_events(ledger_path)  # raises LedgerCorruptionError on any torn line
    assert len(events) == workers * per_worker
    # Distinct resources per worker -> every claim is active.
    assert len(active_claims(ledger_path)) == workers * per_worker


def test_concurrent_processes_race_first_appended_wins(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(_WORKER, encoding="utf-8")
    ledger_path = tmp_path / "ledger.jsonl"

    workers = 6
    # Every worker makes ONE claim on the SAME resource ("shared/0") with a distinct owner.
    specs = [(f"owner{k}", f"s{k}", "shared", 1) for k in range(workers)]
    results = _spawn_workers(worker, ledger_path, specs)

    codes = sorted(res.returncode for res in results)
    # Lock-serialized order is total: exactly one clean winner (0), the rest conflict (1).
    # A worker that raised (e.g. a spurious LockTimeout) exits 3, which would fail this.
    assert codes == [0] + [1] * (workers - 1), [r.stderr for r in results]

    # Finding 5: distinguish a real CONFLICT exit-1 from a spurious LockTimeout exit-1.
    # No worker hit the error path...
    assert all("ERROR" not in res.stdout for res in results), [r.stdout for r in results]
    # ...the single exit-0 worker is the clean winner...
    winners = [res for res in results if res.returncode == 0]
    assert len(winners) == 1 and winners[0].stdout.strip().startswith("CLEAN")
    # ...and every exit-1 worker carries CONFLICT evidence naming BOTH distinct claim IDs.
    conflict_procs = [res for res in results if res.returncode == 1]
    assert len(conflict_procs) == workers - 1
    for res in conflict_procs:
        conflict_lines = [ln for ln in res.stdout.splitlines() if ln.startswith("CONFLICT")]
        assert conflict_lines, f"exit-1 worker lacked conflict evidence: {res.stdout!r}"
        for line in conflict_lines:
            _tag, incoming_id, existing_id = line.split()
            assert incoming_id and existing_id and incoming_id != existing_id

    events = read_events(ledger_path)  # all writes landed, ledger fully parseable
    assert len([e for e in events if isinstance(e, ClaimEvent)]) == workers
    assert len(active_claims(ledger_path)) == workers  # conflicts are advisory: all active


def _thread_append(ledger_path: Path, tid: int, count: int) -> list[int]:
    codes = []
    for i in range(count):
        claim = make_claim(resource=f"t{tid}/{i}", owner=f"owner{tid}", session=f"s{tid}")
        codes.append(append_claim(claim, ledger_path=ledger_path).exit_code)
    return codes


def test_concurrent_threads_no_corruption(ledger_path: Path) -> None:
    threads = 8
    per_thread = 5
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [
            pool.submit(_thread_append, ledger_path, tid, per_thread) for tid in range(threads)
        ]
        for future in futures:
            assert all(code == 0 for code in future.result())  # distinct resources -> all clean

    events = read_events(ledger_path)  # raises on any interleaved/torn line
    assert len(events) == threads * per_thread
    assert len(active_claims(ledger_path)) == threads * per_thread
