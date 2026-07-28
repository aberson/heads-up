"""Lifecycle CLI for heads-up (build Step 4).

Wires the frozen schema (:mod:`heads_up.models`), canonical identity
(:mod:`heads_up.identity`), overlap rules (:mod:`heads_up.conflicts`), and the
append-only ledger (:mod:`heads_up.ledger`) into the four operator verbs:

- ``claim`` — canonicalize the resource/repository, build + validate a
  :class:`~heads_up.models.ClaimEvent` with a REQUIRED finite expiry, append it
  under the ledger's write lock, and report a clean receipt (exit 0), a
  supersession renewal (exit 0), or conflict evidence carrying BOTH claim IDs
  (exit 1).
- ``check`` — report whether a resource is actively claimed and whether the
  caller's prospective claim would conflict, WITHOUT appending anything.
- ``release`` — append a :class:`~heads_up.models.ReleaseEvent` (idempotent
  no-op on an already-released/expired claim; unknown claim_id is a usage error).
  The release ``--owner`` MUST match the claim's owner — releasing another actor's
  claim is a usage error (exit 2), never a silent success.
- ``list`` — print the derived active-claim set.

Every verb accepts ``--json`` (deterministic, schema-versioned output) and
``--ledger PATH`` (override the machine-global ledger). Exit codes follow plan
section 10: 0 = success with no conflict, 1 = advisory conflict, 2 = usage or
ledger error.

**Canonicalize BEFORE constructing an event.** ``conflicts.py`` does raw equality
on canonical inputs and ``ClaimEvent`` does not self-canonicalize, so this module
is the ONE place that turns caller-supplied strings into canonical identity — an
un-canonicalized event would silently mis-scope conflicts. See
:func:`_canonical_identity`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from argparse import ArgumentParser, Namespace
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from heads_up.conflicts import (
    ConflictEvidence,
    OverlapKind,
    find_conflicts,
    resources_overlap,
)
from heads_up.identity import (
    canonicalize_owner,
    canonicalize_repository,
    canonicalize_resource,
    canonicalize_session,
)
from heads_up.ledger import (
    ClaimResult,
    LedgerError,
    ReleaseResult,
    active_claims,
    append_claim,
    append_release,
    read_events,
)
from heads_up.models import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_SUCCESS,
    ActiveClaim,
    ClaimEvent,
    ReleaseEvent,
    ReleaseOutcome,
    ResourceKind,
    SchemaError,
)

# Bumped when the JSON output shape changes; callers pin against it.
SCHEMA_VERSION: Final[int] = 1

_RESOURCE_KIND_CHOICES: Final[tuple[str, ...]] = tuple(kind.value for kind in ResourceKind)

# --ttl duration units, in seconds.
_TTL_UNITS: Final[dict[str, int]] = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_TTL_TOKEN: Final[re.Pattern[str]] = re.compile(r"(\d+)([smhdw])")


class _UsageError(ValueError):
    """A caller-input problem this module diagnoses itself (bad --ttl) -> exit 2.

    Argparse handles the structural usage errors (missing required option,
    unknown verb) with its own ``SystemExit(2)``; this covers the value-level
    ones that argparse cannot express as a simple type.
    """


# ---------------------------------------------------------------------------
# Small pure helpers.
# ---------------------------------------------------------------------------


def _parse_ttl_seconds(raw: str) -> int:
    """Parse a relative TTL like ``30m`` / ``2h`` / ``1d`` / ``1h30m`` into seconds.

    Accepts one or more ``<int><unit>`` tokens (units ``s``/``m``/``h``/``d``/``w``)
    with no gaps and nothing left over. A non-positive or unparseable duration
    raises :class:`_UsageError` (exit 2); the strictly-positive result is fed to
    ``created_at + delta`` so the resulting ``ClaimEvent`` has a finite TTL.
    """
    text = raw.strip().lower()
    if not text:
        raise _UsageError("--ttl must be a non-empty duration, e.g. 30m, 2h, 1d")
    pos = 0
    total = 0
    for match in _TTL_TOKEN.finditer(text):
        if match.start() != pos:
            break
        try:
            magnitude = int(match.group(1))
        except ValueError as exc:
            # A digit run past Python's int-string-conversion limit
            # (sys.get_int_max_str_digits) is malformed input, not a conflict.
            raise _UsageError(
                f"--ttl duration {raw!r} has an out-of-range numeric component"
            ) from exc
        total += magnitude * _TTL_UNITS[match.group(2)]
        pos = match.end()
    if pos != len(text):
        raise _UsageError(
            f"invalid --ttl duration {raw!r}; use <int><unit> tokens (s/m/h/d/w), e.g. 90m or 1h30m"
        )
    if total <= 0:
        raise _UsageError(f"--ttl duration {raw!r} must be strictly positive")
    return total


def _format_dual_time(iso_utc: str) -> str:
    """Render a canonical UTC ISO timestamp as ``<utc> (UTC) / <local> (local)``.

    ``iso_utc`` is a canonical string produced by :mod:`heads_up.models`, so
    ``fromisoformat`` always parses it. The finite-TTL rule (plan section 6)
    requires the CLI to display expiry in BOTH local and UTC time; this helper is
    used for ``created_at`` and ``expires_at`` alike.

    An extreme far-future/past ``expires_at`` (a distant absolute ``--expires-at``)
    can overflow when shifted into the local zone. Because a claim receipt is only
    rendered AFTER the claim is already persisted, rendering must never crash a
    successful claim: on overflow we fall back to a UTC-only display rather than
    raise (which would surface a traceback + exit 1 for a claim that succeeded).
    """
    dt_utc = datetime.fromisoformat(iso_utc)
    try:
        dt_local = dt_utc.astimezone()
    except (OverflowError, OSError, ValueError):
        return f"{dt_utc.isoformat()} (UTC)"
    return f"{dt_utc.isoformat()} (UTC) / {dt_local.strftime('%Y-%m-%d %H:%M:%S %Z')} (local)"


def _canonical_identity(args: Namespace) -> tuple[ResourceKind, str, str, str, str]:
    """Canonicalize resource/repository/owner/session at the input boundary.

    Returns ``(kind, canonical_resource, canonical_repository, owner, session)``.
    The repository is canonicalized first, then handed to
    :func:`canonicalize_resource` so a ``path`` resource relativizes against the
    canonical root. This is the load-bearing "canonicalize BEFORE constructing an
    event" step: two alias spellings of one resource collapse to the same
    canonical string here, so ``conflicts.py`` raw-equality sees them as one.
    Any malformed field raises a :class:`SchemaError` (caught in :func:`main` ->
    exit 2).
    """
    kind = ResourceKind.parse(args.resource_kind)
    repository = canonicalize_repository(args.repository)
    resource = canonicalize_resource(kind, args.resource, repository=repository)
    owner = canonicalize_owner(args.owner)
    session = canonicalize_session(args.session_id)
    return kind, resource, repository, owner, session


def _resolve_expiry(args: Namespace) -> tuple[str, str]:
    """Return ``(created_at_iso, expires_at_iso)`` for a claim (both UTC ISO).

    The mutually-exclusive-required TTL group guarantees exactly one of ``--ttl``
    / ``--expires-at`` is present. ``--expires-at`` is passed through verbatim for
    :class:`ClaimEvent` to validate/canonicalize (a naive or non-UTC value raises
    there); ``--ttl`` is added to ``created_at`` here.
    """
    created = datetime.now(UTC)
    created_iso = created.isoformat()
    expires_at: str | None = args.expires_at
    if expires_at is not None:
        return created_iso, expires_at
    seconds = _parse_ttl_seconds(args.ttl)
    try:
        expires = created + timedelta(seconds=seconds)
    except (OverflowError, ValueError) as exc:
        # An over-range TTL overflows timedelta/datetime; that is malformed input
        # (clean exit 2), NOT an advisory conflict (exit 1).
        raise _UsageError(
            f"--ttl duration {args.ttl!r} is too large to represent a finite expiry"
        ) from exc
    return created_iso, expires.isoformat()


def _emit(payload: dict[str, object], lines: Sequence[str], *, as_json: bool) -> None:
    """Write JSON (deterministic: sorted keys, ASCII) or the text lines to stdout."""
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True))
    else:
        for line in lines:
            print(line)


def _claim_field_lines(claim: ClaimEvent) -> list[str]:
    """Indented ``key: value`` receipt lines for a claim, with dual-time stamps."""
    return [
        f"  claim_id:      {claim.claim_id}",
        f"  resource_kind: {claim.resource_kind.value}",
        f"  resource:      {claim.resource}",
        f"  repository:    {claim.repository}",
        f"  owner:         {claim.owner}",
        f"  session_id:    {claim.session_id}",
        f"  created_at:    {_format_dual_time(claim.created_at)}",
        f"  expires_at:    {_format_dual_time(claim.expires_at)}",
    ]


def _conflict_lines(conflicts: Sequence[ConflictEvidence]) -> list[str]:
    """Text block naming each conflict's reason and BOTH claim IDs."""
    lines = [f"CONFLICTS ({len(conflicts)}):"]
    for index, evidence in enumerate(conflicts, start=1):
        lines.append(f"  [{index}] {evidence.reason()}")
        lines.append(f"      existing_claim_id: {evidence.existing_claim_id}")
        lines.append(f"      incoming_claim_id: {evidence.incoming_claim_id}")
        lines.append(
            f"      held by:           owner={evidence.existing.owner} "
            f"session={evidence.existing.session_id}"
        )
    return lines


# ---------------------------------------------------------------------------
# Verb handlers — each returns a process exit code.
# ---------------------------------------------------------------------------


def _cmd_claim(args: Namespace, *, ledger_path: str | None, as_json: bool) -> int:
    """``claim``: canonicalize, build+validate, append, report conflict evidence."""
    kind, resource, repository, owner, session = _canonical_identity(args)
    created_iso, expires_iso = _resolve_expiry(args)
    claim = ClaimEvent.create(
        resource_kind=kind,
        resource=resource,
        repository=repository,
        owner=owner,
        session_id=session,
        created_at=created_iso,
        expires_at=expires_iso,
    )
    result: ClaimResult = append_claim(claim, ledger_path)

    if result.conflicts:
        status, header = "conflict", "CLAIM registered WITH CONFLICT (advisory, exit 1)"
    elif result.renewed:
        status, header = "renewed", "CLAIM renewed by supersession (you already held this, exit 0)"
    else:
        status, header = "clean", "CLAIM registered (clean, no conflict, exit 0)"

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "verb": "claim",
        "status": status,
        "exit_code": result.exit_code,
        "claim": result.claim.to_dict(),
        "conflicts": [evidence.to_dict() for evidence in result.conflicts],
        "renewed": [renewed.to_dict() for renewed in result.renewed],
    }
    lines = [header, *_claim_field_lines(result.claim)]
    if result.conflicts:
        lines += _conflict_lines(result.conflicts)
    elif result.renewed:
        lines.append(f"RENEWED ({len(result.renewed)}):")
        lines += [f"  superseded_claim_id: {renewed.claim_id}" for renewed in result.renewed]
    _emit(payload, lines, as_json=as_json)
    return result.exit_code


def _cmd_check(args: Namespace, *, ledger_path: str | None, as_json: bool) -> int:
    """``check``: report active-claim status for a resource without appending."""
    kind, resource, repository, owner, session = _canonical_identity(args)
    now = datetime.now(UTC)
    # An ephemeral probe claim (never persisted) so the same overlap + actor rules
    # the ledger applies decide whether the caller WOULD conflict. Its synthetic
    # 1-hour TTL only satisfies ClaimEvent's finite-TTL validation.
    probe = ClaimEvent.create(
        resource_kind=kind,
        resource=resource,
        repository=repository,
        owner=owner,
        session_id=session,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    active = active_claims(ledger_path)
    conflicts = find_conflicts(probe, active)
    held = [
        claim
        for claim in active
        if resources_overlap(probe, claim) is not OverlapKind.NONE
        and claim.owner == owner
        and claim.session_id == session
    ]
    exit_code = EXIT_CONFLICT if conflicts else EXIT_SUCCESS

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "verb": "check",
        "exit_code": exit_code,
        "claimed": bool(conflicts or held),
        "would_conflict": bool(conflicts),
        "probe": {
            "resource_kind": kind.value,
            "resource": resource,
            "repository": repository,
            "owner": owner,
            "session_id": session,
        },
        "conflicts": [evidence.to_dict() for evidence in conflicts],
        "held_by_you": [claim.to_dict() for claim in held],
    }
    if conflicts:
        lines = ["CHECK: WOULD CONFLICT (resource actively claimed by another, exit 1)"]
        lines += _conflict_lines(conflicts)
    elif held:
        lines = [
            "CHECK: you already hold an active claim on this resource (exit 0)",
            *(f"  claim_id: {claim.claim_id}  resource: {claim.resource}" for claim in held),
        ]
    else:
        lines = ["CHECK: clean (resource is not actively claimed, exit 0)"]
    _emit(payload, lines, as_json=as_json)
    return exit_code


def _enforce_release_owner(release: ReleaseEvent, ledger_path: str | None) -> None:
    """Reject releasing another actor's claim (mismatched owner -> exit 2).

    ``ReleaseEvent`` records who issued the release, but the ledger applies a
    release purely by ``claim_id`` and never checks the actor — so without this
    guard anyone could release anyone else's claim by accident. We look up the
    referenced claim's recorded (already-canonical) owner and reject a mismatch as
    a :class:`_UsageError` (clean exit 2). ``claim_id`` is a unique UUIDv4, so at
    most one claim event matches; a genuinely unknown ``claim_id`` is left absent
    here and diagnosed by :func:`append_release` (``UnknownClaimError`` -> exit 2).
    """
    for event in read_events(ledger_path):
        if isinstance(event, ClaimEvent) and event.claim_id == release.claim_id:
            if event.owner != release.owner:
                raise _UsageError(
                    f"release owner {release.owner!r} does not match claim owner "
                    f"{event.owner!r} for claim {release.claim_id}"
                )
            return


def _cmd_release(args: Namespace, *, ledger_path: str | None, as_json: bool) -> int:
    """``release``: append a release; idempotent no-op / unknown claim -> exit 2."""
    owner = canonicalize_owner(args.owner)
    release = ReleaseEvent.create(claim_id=args.claim_id, owner=owner)
    _enforce_release_owner(release, ledger_path)
    result: ReleaseResult = append_release(release, ledger_path)

    if result.outcome is ReleaseOutcome.APPLIED:
        header = f"RELEASE applied (claim {release.claim_id} released, exit 0)"
    else:  # noop-inactive
        header = (
            f"RELEASE no-op (claim {release.claim_id} was already released or expired; "
            f"idempotent, exit 0)"
        )
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "verb": "release",
        "status": result.outcome.value,
        "exit_code": result.exit_code,
        "claim_id": release.claim_id,
        "owner": release.owner,
        "released_at": release.released_at,
    }
    _emit(payload, [header], as_json=as_json)
    return result.exit_code


def _cmd_list(*, ledger_path: str | None, as_json: bool) -> int:
    """``list``: print the derived active-claim set (always exit 0)."""
    active: list[ActiveClaim] = active_claims(ledger_path)
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "verb": "list",
        "exit_code": EXIT_SUCCESS,
        "count": len(active),
        "active_claims": [claim.to_dict() for claim in active],
    }
    if active:
        lines = [f"ACTIVE CLAIMS ({len(active)}):"]
        for claim in active:
            lines.append(
                f"  {claim.claim_id}  {claim.resource_kind.value}:{claim.resource}  "
                f"repo={claim.repository}  owner={claim.owner}  session={claim.session_id}"
            )
            lines.append(f"      expires_at: {_format_dual_time(claim.expires_at)}")
    else:
        lines = ["No active claims."]
    _emit(payload, lines, as_json=as_json)
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Parser construction.
# ---------------------------------------------------------------------------


def _add_identity_args(parser: ArgumentParser, *, require_actor: bool) -> None:
    """Add the shared resource-identity options to ``claim`` / ``check``."""
    parser.add_argument(
        "--resource-kind",
        required=True,
        choices=_RESOURCE_KIND_CHOICES,
        help="What is being claimed.",
    )
    parser.add_argument("--resource", required=True, help="The resource identifier to claim.")
    parser.add_argument(
        "--repository", required=True, help="Repository root the claim is scoped to."
    )
    parser.add_argument("--owner", required=require_actor, help="Caller-supplied owner identity.")
    parser.add_argument(
        "--session-id", required=require_actor, help="Caller-supplied session identity."
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
        default=None,
        help="Override the ledger file path (default: %%LOCALAPPDATA%%/heads-up/ledger.jsonl).",
    )

    # Shared options every verb accepts. --ledger uses SUPPRESS so a post-verb
    # `list --ledger X` sets it while a pre-verb `--ledger X list` still wins.
    common = ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", help="Emit deterministic JSON instead of text."
    )
    common.add_argument(
        "--ledger",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="Override the ledger file path (accepted after the verb too).",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="{claim,check,release,list}")

    claim_p = subparsers.add_parser(
        "claim", parents=[common], help="Register an advisory claim (requires a finite TTL)."
    )
    _add_identity_args(claim_p, require_actor=True)
    ttl_group = claim_p.add_mutually_exclusive_group(required=True)
    ttl_group.add_argument(
        "--ttl", metavar="DURATION", help="Relative expiry, e.g. 30m, 2h, 1d, 1h30m."
    )
    ttl_group.add_argument(
        "--expires-at",
        metavar="ISO8601",
        help="Absolute UTC expiry, e.g. 2026-07-29T00:00:00+00:00.",
    )

    check_p = subparsers.add_parser(
        "check", parents=[common], help="Check whether a resource is actively claimed."
    )
    _add_identity_args(check_p, require_actor=True)

    release_p = subparsers.add_parser(
        "release", parents=[common], help="Release a claim by id (idempotent)."
    )
    release_p.add_argument("--claim-id", required=True, help="The claim_id to release (UUIDv4).")
    release_p.add_argument("--owner", required=True, help="Caller-supplied owner identity.")

    subparsers.add_parser("list", parents=[common], help="List active claims.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point. Returns a process exit code (0/1/2)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    command: str | None = args.command
    if command is None:
        parser.print_help(sys.stderr)
        return EXIT_ERROR

    ledger_path: str | None = args.ledger
    as_json: bool = args.json
    try:
        if command == "claim":
            return _cmd_claim(args, ledger_path=ledger_path, as_json=as_json)
        if command == "check":
            return _cmd_check(args, ledger_path=ledger_path, as_json=as_json)
        if command == "release":
            return _cmd_release(args, ledger_path=ledger_path, as_json=as_json)
        if command == "list":
            return _cmd_list(ledger_path=ledger_path, as_json=as_json)
    except (SchemaError, LedgerError, _UsageError) as exc:
        print(f"heads-up {command}: error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    parser.print_help(sys.stderr)  # pragma: no cover - unreachable: command is validated above
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
