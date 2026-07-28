"""Append-only event ledger for heads-up (build Step 3).

Owns the on-disk side of the advisory-claim model: atomic append, concurrent-write
serialization, replay into derived active state, release, expiry, and corruption
diagnostics. It reuses the frozen Step-1 schema (:mod:`heads_up.models`) and the
Step-2 overlap rules (:mod:`heads_up.conflicts`) verbatim — it never redefines a
shape or a conflict rule. The Step-4 CLI wires these functions to the verbs.

Design (plan sections 6, 8, 10):

- **One machine-global file.** The ledger is a single file at
  ``%LOCALAPPDATA%\\heads-up\\ledger.jsonl`` (Windows) with a POSIX fallback so the
  module imports and tests run cross-platform. There is NO per-repository file key:
  per-repository conflict scoping lives entirely in each event's ``repository``
  field, so every session and worktree of a repository resolves the SAME file
  unconditionally. A ``ledger_path`` argument (the CLI's ``--ledger``) overrides it.

- **Write serialization via an exclusive sidecar lock.** Every mutating operation
  acquires an exclusive OS lock on a sidecar ``ledger.jsonl.lock`` — stdlib
  ``msvcrt.locking`` on Windows (the primary path), ``fcntl.flock`` on POSIX — with a
  bounded ~5-second retry. Exhausting the budget raises :class:`LockTimeoutError`
  (a :class:`LedgerError` -> exit 2). No third-party locking dependency.

- **Atomic append.** One event is exactly one physical JSONL line, appended and
  ``fsync``-flushed while the lock is held. The one-line invariant is guaranteed
  upstream by the schema's control-character rejection, so a record can never span
  lines and a crash cannot interleave two writers' bytes.

- **Append order is authoritative.** The read-decide-append of a claim is one
  lock-serialized critical section, so the lock makes the append order a total
  order with no timestamp tie-break: the first writer on a resource sees an empty
  active set and gets a clean receipt (exit 0); every later conflicting writer
  still appends its claim (auditable) but its result carries both claim IDs as
  conflict evidence (exit 1). A same-actor re-claim renews by supersession (exit 0).

- **Corruption policy: FAIL LOUD.** Any unparseable JSONL line halts replay with a
  file-specific :class:`LedgerCorruptionError` naming the 1-based line number and the
  reason — never a silent skip (which could hide a lost claim) and never an uncaught
  crash. This covers every malformation class: a torn line, non-JSON garbage, invalid
  UTF-8, a well-formed-JSON but schema-invalid event (bad claim_id, empty resource,
  non-UTC timestamp, non-positive TTL, unknown resource_kind), and a line that blows
  the per-line byte cap. Blank/whitespace-only lines carry no event and are the only
  lines skipped.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Final

from heads_up.conflicts import ConflictEvidence, find_conflicts
from heads_up.models import (
    EXIT_CONFLICT,
    EXIT_SUCCESS,
    ActiveClaim,
    ClaimEvent,
    ReleaseEvent,
    ReleaseOutcome,
    SchemaError,
    _coerce_now,
    classify_release,
    event_from_json_line,
    exit_code_for_release,
    is_supersession,
)

__all__ = [
    "LOCK_POLL_INTERVAL",
    "LOCK_TIMEOUT_SECONDS",
    "ClaimResult",
    "LedgerCorruptionError",
    "LedgerError",
    "LockTimeoutError",
    "ReleaseResult",
    "UnknownClaimError",
    "active_claims",
    "append_claim",
    "append_release",
    "default_ledger_path",
    "derive_active_claims",
    "read_events",
    "resolve_ledger_path",
]

# A single ledger record is a claim or a release.
LedgerEvent = ClaimEvent | ReleaseEvent

# Bounded write-lock retry budget (plan Step 3: ~5 seconds total, then exit 2).
# Module-level so a test can monkeypatch them to keep the exhaustion test fast.
LOCK_TIMEOUT_SECONDS: float = 5.0
LOCK_POLL_INTERVAL: float = 0.05


# ---------------------------------------------------------------------------
# Error hierarchy — every ledger operational failure maps to exit 2.
# ---------------------------------------------------------------------------


class LedgerError(RuntimeError):
    """Base for operational ledger failures (plan section 10: exit 2)."""


class LockTimeoutError(LedgerError):
    """The bounded write-lock retry budget was exhausted (lock contention)."""


class UnknownClaimError(LedgerError):
    """A release referenced a claim_id that is absent from the ledger."""


class LedgerCorruptionError(LedgerError):
    """A ledger line failed to parse — carries the 1-based line number + reason.

    Fail-loud by design: replay stops here rather than silently dropping a line,
    because a dropped line could be a lost claim.
    """

    def __init__(self, *, path: Path, line_number: int, reason: str) -> None:
        self.path = path
        self.line_number = line_number
        self.reason = reason
        super().__init__(f"{path}:{line_number}: corrupt ledger line: {reason}")


# ---------------------------------------------------------------------------
# Platform-specific exclusive lock (msvcrt primary; fcntl fallback).
# ---------------------------------------------------------------------------


if sys.platform == "win32":
    import msvcrt

    def _lock_exclusive(fd: int) -> None:
        """Non-blocking exclusive lock of one byte; raises OSError if held."""
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - POSIX fallback; Windows msvcrt is the primary path
    import fcntl

    def _lock_exclusive(fd: int) -> None:
        """Non-blocking exclusive lock; raises OSError (EWOULDBLOCK) if held."""
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Ledger path resolution — one machine-global file, no per-repo key.
# ---------------------------------------------------------------------------


def default_ledger_path() -> Path:
    """The single machine-global ledger file (plan section 10).

    Windows: ``%LOCALAPPDATA%\\heads-up\\ledger.jsonl``. Elsewhere: an XDG-style
    fallback so the module imports and its tests run cross-platform. There is NO
    per-repository key — repository scoping lives in each event's ``repository``
    field, so every session and worktree resolves this exact same file.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "heads-up" / "ledger.jsonl"


def resolve_ledger_path(ledger_path: str | os.PathLike[str] | None) -> Path:
    """Resolve the effective ledger path: an explicit override wins, else default."""
    if ledger_path is None:
        return default_ledger_path()
    return Path(ledger_path)


def _lock_sidecar_path(ledger_path: Path) -> Path:
    """The exclusive-lock sidecar next to the ledger (``<name>.lock``)."""
    return ledger_path.with_name(ledger_path.name + ".lock")


# ---------------------------------------------------------------------------
# Locking + atomic append primitives.
# ---------------------------------------------------------------------------


# errnos that mean "another writer already holds the lock" (retry-worthy
# contention), as opposed to a genuine I/O failure. Windows ``msvcrt.locking``
# with ``LK_NBLCK`` raises EACCES on a held region (EDEADLK on some CRTs);
# POSIX ``fcntl.flock`` with ``LOCK_NB`` raises EACCES / EAGAIN / EWOULDBLOCK.
# Anything OUTSIDE this set is a real error and must not be retried for ~5s.
_LOCK_CONTENTION_ERRNOS: Final[frozenset[int]] = frozenset(
    {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}
)


def _acquire_with_retry(fd: int, sidecar: Path) -> None:
    """Poll for the exclusive lock until the bounded budget is exhausted.

    ONLY a lock-contention ``OSError`` (another writer holds the lock) is retried;
    a genuine non-contention ``OSError`` (a real I/O/permission failure) is
    re-raised immediately as a clean :class:`LedgerError` — no 5-second wait and
    no :class:`LockTimeoutError` misreport for a fault that isn't contention.
    """
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            _lock_exclusive(fd)
            return
        except OSError as exc:
            if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                raise LedgerError(f"failed to acquire exclusive lock on {sidecar}: {exc}") from exc
            if time.monotonic() >= deadline:
                raise LockTimeoutError(
                    f"could not acquire exclusive lock on {sidecar} within "
                    f"{LOCK_TIMEOUT_SECONDS:.1f}s; another writer holds it"
                ) from None
            time.sleep(LOCK_POLL_INTERVAL)


@contextmanager
def _write_lock(ledger_path: Path) -> Iterator[None]:
    """Hold an exclusive lock on the sidecar for the duration of the block.

    Creates the ledger directory on first use (plan section 10: created on first
    claim), opens the sidecar, and acquires the exclusive lock with a bounded
    retry. Always releases and closes, even on error.
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = _lock_sidecar_path(ledger_path)
    fd = os.open(str(sidecar), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _acquire_with_retry(fd, sidecar)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _append_line(ledger_path: Path, line: str) -> None:
    """Append one JSONL record and flush it to disk while the lock is held.

    ``newline=""`` writes exactly one ``\\n`` (no platform translation), so the
    record is one physical line. ``flush`` + ``fsync`` push it to stable storage
    before the lock is released, so a crash cannot leave a torn line.
    """
    with ledger_path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


# ---------------------------------------------------------------------------
# Replay — read every event, then derive stable active state.
# ---------------------------------------------------------------------------


# Per-physical-line byte cap for replay. A well-formed claim/release record is a
# few hundred bytes; 1 MiB is comfortably above any legitimate line. A single
# newline-less line beyond this is diagnosed as corruption rather than read into
# memory unbounded (OOM guard). Read as a module global so a test can shrink it.
MAX_LINE_BYTES: Final[int] = 1 << 20  # 1 MiB


def _iter_capped_lines(handle: IO[bytes], ledger_path: Path) -> Iterator[tuple[int, bytes]]:
    """Yield ``(1-based line_number, raw bytes)`` for each physical line.

    Uses a bounded ``readline`` so a single giant newline-less line is capped at
    ``MAX_LINE_BYTES`` rather than pulled into memory whole: a line that exceeds
    the cap without terminating raises :class:`LedgerCorruptionError` (OOM guard),
    while a legitimately-terminated line at the boundary passes through.
    """
    line_number = 0
    while True:
        raw = handle.readline(MAX_LINE_BYTES + 1)
        if not raw:
            return
        line_number += 1
        if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
            raise LedgerCorruptionError(
                path=ledger_path,
                line_number=line_number,
                reason=f"line exceeds the {MAX_LINE_BYTES}-byte per-line cap",
            )
        yield line_number, raw


def _read_events_at(ledger_path: Path) -> list[LedgerEvent]:
    """Parse every record in the file; fail loud on the first bad line.

    Blank/whitespace-only lines carry no event and are skipped. Any non-blank line
    that will not parse — invalid UTF-8, unparseable JSON, a well-formed-JSON but
    schema-invalid event (bad claim_id, empty resource, non-UTC timestamp, ...), or
    one exceeding the per-line byte cap — raises :class:`LedgerCorruptionError` with
    its 1-based line number (fail-loud policy). A ledger deleted out from under the
    read (a racing deleter after the exists() check) degrades to an empty list.
    """
    events: list[LedgerEvent] = []
    try:
        handle = ledger_path.open("rb")
    except FileNotFoundError:
        return []  # concurrently deleted between the exists() check and this open
    with handle:
        for line_number, raw in _iter_capped_lines(handle, ledger_path):
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LedgerCorruptionError(
                    path=ledger_path, line_number=line_number, reason=f"invalid UTF-8: {exc}"
                ) from exc
            line = decoded.strip()
            if not line:
                continue
            try:
                events.append(event_from_json_line(line))
            except SchemaError as exc:
                # SchemaError is the base of BOTH MalformedEventError (bad JSON /
                # unknown event) AND the constructor validation errors
                # (InvalidClaimIdError, InvalidResourceError, ...), so a schema-
                # invalid-but-valid-JSON line gets the file:line diagnostic too.
                raise LedgerCorruptionError(
                    path=ledger_path, line_number=line_number, reason=str(exc)
                ) from exc
    return events


def _read_if_exists(ledger_path: Path) -> list[LedgerEvent]:
    """Read a resolved path under an already-held lock; missing file -> empty."""
    if not ledger_path.exists():
        return []
    return _read_events_at(ledger_path)


def read_events(ledger_path: str | os.PathLike[str] | None = None) -> list[LedgerEvent]:
    """Read every event, locked for read-consistency. Missing file -> empty list.

    Reads take the same exclusive lock as writes so a reader never observes a
    half-written final line from an in-flight append. A missing ledger returns an
    empty list without creating the directory or the sidecar.
    """
    path = resolve_ledger_path(ledger_path)
    if not path.exists():
        return []
    with _write_lock(path):
        return _read_events_at(path)


def derive_active_claims(
    events: Iterable[LedgerEvent], *, now: datetime | str | None = None
) -> list[ActiveClaim]:
    """Derive the active-claim set from a replayed event stream (deterministic).

    A claim is ACTIVE unless it was:

    - **released** — a later :class:`ReleaseEvent` names its ``claim_id``;
    - **expired** — ``expires_at <= now`` (default: current UTC time); or
    - **superseded** — a LATER same-actor claim on the same resource renewed it
      (``models.is_supersession``).

    Released, expired, and superseded events remain in the ledger (auditable) but
    are omitted from the active set. Active claims are returned in append order.
    Advisory model: two different-actor claims on one resource are BOTH active and
    mutually conflicting — a conflict is reported, never silently resolved.
    """
    now_dt = _coerce_now(now)
    claim_events: list[ClaimEvent] = []
    released_ids: set[str] = set()
    for event in events:
        if isinstance(event, ClaimEvent):
            claim_events.append(event)
        else:
            released_ids.add(event.claim_id)

    active: list[ActiveClaim] = []
    for index, claim in enumerate(claim_events):
        if claim.claim_id in released_ids:
            continue
        if claim.is_expired(now_dt):
            continue
        if any(is_supersession(claim, later) for later in claim_events[index + 1 :]):
            continue
        active.append(ActiveClaim.from_claim(claim))
    return active


def active_claims(
    ledger_path: str | os.PathLike[str] | None = None,
    *,
    now: datetime | str | None = None,
) -> list[ActiveClaim]:
    """Convenience: read the ledger and derive its current active-claim set."""
    return derive_active_claims(read_events(ledger_path), now=now)


# ---------------------------------------------------------------------------
# Structured operation results.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Outcome of appending a claim.

    ``conflicts`` is empty for a clean receipt (fresh claim or supersession
    renewal) and holds one :class:`ConflictEvidence` per active claim the new one
    contends with (each carrying both claim IDs). ``renewed`` lists the same-actor
    active claims this claim supersedes. The claim is ALWAYS appended regardless of
    conflict — the conflict is advisory evidence, not a rejection.
    """

    claim: ClaimEvent
    conflicts: tuple[ConflictEvidence, ...]
    renewed: tuple[ActiveClaim, ...]

    @property
    def is_clean(self) -> bool:
        """True when the claim contends with no active claim (exit 0)."""
        return not self.conflicts

    @property
    def exit_code(self) -> int:
        """0 for a clean receipt, 1 when conflict evidence is present."""
        return EXIT_CONFLICT if self.conflicts else EXIT_SUCCESS


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    """Outcome of applying a release (APPLIED or an idempotent inactive no-op)."""

    release: ReleaseEvent
    outcome: ReleaseOutcome

    @property
    def exit_code(self) -> int:
        """0 for applied/idempotent no-op (unknown claim raises before this)."""
        return exit_code_for_release(self.outcome)


# ---------------------------------------------------------------------------
# Mutating operations — all read-decide-append under one lock.
# ---------------------------------------------------------------------------


def append_claim(
    claim: ClaimEvent,
    ledger_path: str | os.PathLike[str] | None = None,
    *,
    now: datetime | str | None = None,
) -> ClaimResult:
    """Atomically append a claim and report conflict evidence against active state.

    The replay-decide-append runs inside one exclusive critical section, so append
    order is authoritative: the first writer on a resource sees no active conflict
    (clean, exit 0); every later different-actor writer still appends (auditable)
    but gets both claim IDs as evidence (exit 1). A same-actor re-claim renews by
    supersession (clean, exit 0). Raises :class:`LedgerCorruptionError` if the
    existing ledger is corrupt (refuses to append on top of a corrupt file).
    """
    path = resolve_ledger_path(ledger_path)
    with _write_lock(path):
        existing_active = derive_active_claims(_read_if_exists(path), now=now)
        conflicts = tuple(find_conflicts(claim, existing_active))
        renewed = tuple(active for active in existing_active if is_supersession(active, claim))
        _append_line(path, claim.to_json_line())
    return ClaimResult(claim=claim, conflicts=conflicts, renewed=renewed)


def append_release(
    release: ReleaseEvent,
    ledger_path: str | os.PathLike[str] | None = None,
    *,
    now: datetime | str | None = None,
) -> ReleaseResult:
    """Atomically apply a release against current ledger state.

    - Unknown ``claim_id`` -> :class:`UnknownClaimError` (exit 2); nothing written.
    - Already released or expired (known but inactive) -> idempotent no-op: nothing
      is appended and the outcome is :attr:`ReleaseOutcome.NOOP_INACTIVE` (exit 0).
    - Active claim -> the :class:`ReleaseEvent` is appended and the claim becomes
      inactive on the next replay (:attr:`ReleaseOutcome.APPLIED`, exit 0).
    """
    path = resolve_ledger_path(ledger_path)
    with _write_lock(path):
        events = _read_if_exists(path)
        claim_exists = any(
            isinstance(event, ClaimEvent) and event.claim_id == release.claim_id for event in events
        )
        active_ids = {active.claim_id for active in derive_active_claims(events, now=now)}
        outcome = classify_release(
            claim_exists=claim_exists, claim_active=release.claim_id in active_ids
        )
        if outcome is ReleaseOutcome.UNKNOWN_CLAIM:
            raise UnknownClaimError(
                f"cannot release unknown claim_id {release.claim_id!r}; "
                f"it is not present in the ledger"
            )
        if outcome is ReleaseOutcome.APPLIED:
            _append_line(path, release.to_json_line())
    return ReleaseResult(release=release, outcome=outcome)
