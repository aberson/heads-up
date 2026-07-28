"""Authoritative claim/release event schema for heads-up.

Frozen in build Step 1. This module owns the exact shapes, stable IDs, TTL
rules, validation, lifecycle predicates, and the JSON/text serialization
contracts. Later steps consume these types but never redefine them:

- Step 2 (``identity.py`` / ``conflicts.py``) normalizes resources and adds
  ancestor/descendant overlap. Step 1 freezes only the *exact-resource*
  predicate (:func:`same_resource`).
- Step 3 (``ledger.py``) appends/replays events and *derives* the never-stored
  :class:`ActiveClaim` shape, applying the lifecycle predicates frozen here.
- Step 4 (``cli.py``) wires the verbs and exit codes.

Runtime dependencies: standard library only (no ``portalocker`` etc.).
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Final

__all__ = [
    "EXIT_CONFLICT",
    "EXIT_ERROR",
    "EXIT_SUCCESS",
    "ActiveClaim",
    "ClaimEvent",
    "ClaimPairOutcome",
    "InvalidClaimIdError",
    "InvalidOwnerError",
    "InvalidRepositoryError",
    "InvalidResourceError",
    "InvalidResourceKindError",
    "InvalidSessionError",
    "InvalidTimestampError",
    "MalformedEventError",
    "NonFiniteTTLError",
    "ReleaseEvent",
    "ReleaseOutcome",
    "ResourceKind",
    "SchemaError",
    "canonical_utc_timestamp",
    "classify_claim_pair",
    "classify_release",
    "event_from_dict",
    "event_from_json_line",
    "exit_code_description",
    "exit_code_for_claim_pair",
    "exit_code_for_release",
    "is_conflict",
    "is_supersession",
    "new_claim_id",
    "parse_utc_timestamp",
    "same_resource",
    "utc_now_iso",
    "validate_claim_id",
]


# ---------------------------------------------------------------------------
# Exit codes (plan section 10). Deliberately tool-local.
# ---------------------------------------------------------------------------

EXIT_SUCCESS: Final = 0  # clean claim, clean check, successful/idempotent release
EXIT_CONFLICT: Final = 1  # advisory conflict reported
EXIT_ERROR: Final = 2  # usage or ledger error (bad args, lock exhaustion, unknown claim)

_EXIT_DESCRIPTIONS: Final[dict[int, str]] = {
    EXIT_SUCCESS: "success with no conflict",
    EXIT_CONFLICT: "advisory conflict reported",
    EXIT_ERROR: "usage or ledger error",
}


def exit_code_description(code: int) -> str:
    """Return the human-readable meaning of an exit code, or raise on unknown."""
    description = _EXIT_DESCRIPTIONS.get(code)
    if description is None:
        raise ValueError(f"unknown exit code: {code!r}")
    return description


# ---------------------------------------------------------------------------
# Error hierarchy — every malformed input raises an explicit, specific error.
# ---------------------------------------------------------------------------


class SchemaError(ValueError):
    """Base class for all heads-up schema validation errors."""


class InvalidClaimIdError(SchemaError):
    """claim_id is not a canonical lowercase-hyphenated UUIDv4."""


class InvalidResourceKindError(SchemaError):
    """resource_kind is not one of the allowed enum values."""


class InvalidResourceError(SchemaError):
    """resource is empty or whitespace-only."""


class InvalidRepositoryError(SchemaError):
    """repository is empty or whitespace-only."""


class InvalidOwnerError(SchemaError):
    """owner is empty or whitespace-only."""


class InvalidSessionError(SchemaError):
    """session_id is empty or whitespace-only."""


class InvalidTimestampError(SchemaError):
    """A timestamp is not ISO-8601 or is not explicit UTC."""


class NonFiniteTTLError(SchemaError):
    """expires_at is not strictly after created_at (non-positive TTL)."""


class MalformedEventError(SchemaError):
    """A serialized event object is missing fields, mistyped, or wrong event type."""


# ---------------------------------------------------------------------------
# Resource kinds (plan section 3 in-scope list).
# ---------------------------------------------------------------------------


class ResourceKind(StrEnum):
    """What is being claimed."""

    REPOSITORY = "repository"
    PATH = "path"
    PLAN_STEP = "plan-step"
    ISSUE = "issue"
    NAMED_RESOURCE = "named-resource"

    @classmethod
    def parse(cls, value: str | ResourceKind) -> ResourceKind:
        """Coerce a wire value to a :class:`ResourceKind`, raising on unknowns."""
        if isinstance(value, ResourceKind):
            return value
        try:
            return cls(value)
        except ValueError:
            allowed = ", ".join(kind.value for kind in cls)
            raise InvalidResourceKindError(
                f"resource_kind must be one of: {allowed}; got {value!r}"
            ) from None


# ---------------------------------------------------------------------------
# Stable IDs and timestamps.
# ---------------------------------------------------------------------------


def new_claim_id() -> str:
    """Generate a fresh claim_id: ``str(uuid.uuid4())`` (lowercase hyphenated)."""
    return str(uuid.uuid4())


def validate_claim_id(value: str) -> str:
    """Validate a claim_id is a canonical lowercase-hyphenated UUIDv4.

    Rejects non-UUID strings, non-version-4 UUIDs, and non-canonical spellings
    (uppercase, braces, urn prefix) so the on-disk id shape stays stable.
    """
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise InvalidClaimIdError(f"claim_id is not a valid UUID: {value!r}") from None
    if parsed.version != 4:
        raise InvalidClaimIdError(
            f"claim_id must be a UUIDv4 (version 4); got version {parsed.version}: {value!r}"
        )
    if str(parsed) != value:
        raise InvalidClaimIdError(
            f"claim_id must be canonical lowercase-hyphenated form; got {value!r}"
        )
    return value


def parse_utc_timestamp(value: str, *, field_name: str) -> datetime:
    """Parse an ISO-8601 timestamp, requiring an explicit UTC offset.

    Naive timestamps (no tzinfo) and non-zero offsets both raise
    :class:`InvalidTimestampError` — heads-up stores UTC only.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise InvalidTimestampError(
            f"{field_name} is not a valid ISO-8601 timestamp: {value!r}"
        ) from None
    if parsed.tzinfo is None:
        raise InvalidTimestampError(
            f"{field_name} must be UTC with an explicit offset; got naive timestamp {value!r}"
        )
    if parsed.utcoffset() != timedelta(0):
        raise InvalidTimestampError(f"{field_name} must be UTC (offset +00:00); got {value!r}")
    return parsed


def canonical_utc_timestamp(value: str, *, field_name: str) -> str:
    """Validate and normalize a timestamp to canonical UTC ISO-8601 (``+00:00``).

    ``...Z`` and ``...+00:00`` inputs collapse to the same canonical string, so
    round-trips are deterministic.
    """
    return parse_utc_timestamp(value, field_name=field_name).astimezone(UTC).isoformat()


def utc_now_iso() -> str:
    """Current time as a canonical UTC ISO-8601 string."""
    return datetime.now(UTC).isoformat()


# Unicode categories that would break the one-event-per-line JSONL invariant if
# they reached a caller-supplied field: Cc = C0/C1 controls (\n, \r, \t, DEL, NEL
# U+0085), Zl = line separator (U+2028), Zp = paragraph separator (U+2029). Note
# that ``str.splitlines`` treats U+0085/U+2028/U+2029 as line breaks, so a raw one
# in a field would let a single logical event span multiple physical lines.
_LINE_BREAKING_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Zl", "Zp"})


def _require_nonempty(value: str, error: type[SchemaError], field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field_name} must be a non-empty string; got {value!r}")
    for ch in value:
        if unicodedata.category(ch) in _LINE_BREAKING_CATEGORIES:
            raise error(
                f"{field_name} must not contain control characters or line/paragraph "
                f"separators (would corrupt the one-event-per-line ledger); got {value!r}"
            )


def _safe_text_value(value: str) -> str:
    """Escape control chars / line separators for single-line ``key=value`` text.

    Caller-supplied fields are already rejected at construction (see
    :func:`_require_nonempty`), so for a validly-built event this is a no-op.
    It is defense in depth for the human-readable :meth:`to_text` renderers used
    by the Step-4 verbs, guaranteeing their output can never span lines.
    """
    return "".join(
        ch if unicodedata.category(ch) not in _LINE_BREAKING_CATEGORIES else f"\\u{ord(ch):04x}"
        for ch in value
    )


def _coerce_now(now: datetime | str | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, str):
        return parse_utc_timestamp(now, field_name="now")
    if now.tzinfo is None:
        raise InvalidTimestampError("now must be a timezone-aware UTC datetime")
    return now.astimezone(UTC)


def _field_str(data: Mapping[str, Any], key: str) -> str:
    if key not in data:
        raise MalformedEventError(f"missing required field: {key!r}")
    value = data[key]
    if not isinstance(value, str):
        raise MalformedEventError(f"field {key!r} must be a string; got {type(value).__name__}")
    return value


def _expect_event(data: Mapping[str, Any], expected: str) -> None:
    event = data.get("event")
    if event != expected:
        raise MalformedEventError(f"expected event type {expected!r}; got {event!r}")


def _normalize_claim_fields(obj: ClaimEvent | ActiveClaim) -> None:
    """Validate + normalize the shared claim-shaped fields in place.

    Single source of the field contract for both :class:`ClaimEvent` (the stored
    event) and :class:`ActiveClaim` (the derived, hand-buildable projection), so a
    direct ``ActiveClaim(...)`` construction cannot hold fields a ``ClaimEvent``
    would reject. Mutates the frozen dataclass via ``object.__setattr__`` to
    canonicalize ``claim_id``, ``resource_kind``, and both timestamps.
    """
    object.__setattr__(obj, "claim_id", validate_claim_id(obj.claim_id))
    object.__setattr__(obj, "resource_kind", ResourceKind.parse(obj.resource_kind))
    _require_nonempty(obj.resource, InvalidResourceError, "resource")
    _require_nonempty(obj.repository, InvalidRepositoryError, "repository")
    _require_nonempty(obj.owner, InvalidOwnerError, "owner")
    _require_nonempty(obj.session_id, InvalidSessionError, "session_id")
    object.__setattr__(
        obj, "created_at", canonical_utc_timestamp(obj.created_at, field_name="created_at")
    )
    object.__setattr__(
        obj, "expires_at", canonical_utc_timestamp(obj.expires_at, field_name="expires_at")
    )
    created = parse_utc_timestamp(obj.created_at, field_name="created_at")
    expires = parse_utc_timestamp(obj.expires_at, field_name="expires_at")
    if expires <= created:
        raise NonFiniteTTLError(
            f"expires_at ({obj.expires_at}) must be strictly after "
            f"created_at ({obj.created_at}); claims require a finite positive TTL"
        )


# ---------------------------------------------------------------------------
# ClaimEvent — the append-only claim record.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimEvent:
    """A single advisory claim, validated on construction.

    Every field is required. Timestamps are normalized to canonical UTC. A
    claim with a non-positive TTL (``expires_at <= created_at``) is rejected —
    claims must expire in finite, positive time.
    """

    claim_id: str
    resource_kind: ResourceKind
    resource: str
    repository: str
    owner: str
    session_id: str
    created_at: str
    expires_at: str

    EVENT_TYPE: ClassVar[str] = "claim"

    def __post_init__(self) -> None:
        _normalize_claim_fields(self)

    @classmethod
    def create(
        cls,
        *,
        resource_kind: str | ResourceKind,
        resource: str,
        repository: str,
        owner: str,
        session_id: str,
        expires_at: str,
        created_at: str | None = None,
        claim_id: str | None = None,
    ) -> ClaimEvent:
        """Build a claim, generating ``claim_id`` and ``created_at`` when omitted.

        This is the "claim time" entry point: the UUIDv4 is minted here in
        ``models.py`` (per the frozen schema) unless a caller replays an
        existing id.
        """
        return cls(
            claim_id=new_claim_id() if claim_id is None else claim_id,
            resource_kind=ResourceKind.parse(resource_kind),
            resource=resource,
            repository=repository,
            owner=owner,
            session_id=session_id,
            created_at=utc_now_iso() if created_at is None else created_at,
            expires_at=expires_at,
        )

    def is_expired(self, now: datetime | str | None = None) -> bool:
        """True when ``now`` is at or past ``expires_at`` (default: current UTC time)."""
        return _coerce_now(now) >= parse_utc_timestamp(self.expires_at, field_name="expires_at")

    def to_dict(self) -> dict[str, str]:
        """Deterministic ordered mapping for the JSONL wire format."""
        return {
            "event": self.EVENT_TYPE,
            "claim_id": self.claim_id,
            "resource_kind": self.resource_kind.value,
            "resource": self.resource,
            "repository": self.repository,
            "owner": self.owner,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def to_json_line(self) -> str:
        """Compact, deterministic single-line JSON (one ledger record).

        ``ensure_ascii=True`` guarantees pure-ASCII output, so the record is
        always exactly one physical line even if a field somehow held a raw
        line/paragraph separator (defense in depth atop the field validation).
        """
        return json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":"))

    def to_text(self) -> str:
        """Deterministic human-readable ``key=value`` one-liner."""
        return " ".join(f"{key}={_safe_text_value(value)}" for key, value in self.to_dict().items())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ClaimEvent:
        """Rebuild and re-validate a claim from a parsed JSON object."""
        _expect_event(data, cls.EVENT_TYPE)
        return cls(
            claim_id=_field_str(data, "claim_id"),
            resource_kind=ResourceKind.parse(_field_str(data, "resource_kind")),
            resource=_field_str(data, "resource"),
            repository=_field_str(data, "repository"),
            owner=_field_str(data, "owner"),
            session_id=_field_str(data, "session_id"),
            created_at=_field_str(data, "created_at"),
            expires_at=_field_str(data, "expires_at"),
        )


# ---------------------------------------------------------------------------
# ReleaseEvent — an explicit release record referencing a prior claim.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseEvent:
    """An explicit release of a prior claim.

    Step 1 freezes the *shape* and field validation. The referential check
    (that ``claim_id`` names an existing claim) is applied by the ledger in
    Step 3 — see :func:`classify_release` for the frozen outcome rule.
    """

    claim_id: str
    released_at: str
    owner: str

    EVENT_TYPE: ClassVar[str] = "release"

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", validate_claim_id(self.claim_id))
        _require_nonempty(self.owner, InvalidOwnerError, "owner")
        object.__setattr__(
            self, "released_at", canonical_utc_timestamp(self.released_at, field_name="released_at")
        )

    @classmethod
    def create(
        cls,
        *,
        claim_id: str,
        owner: str,
        released_at: str | None = None,
    ) -> ReleaseEvent:
        """Build a release, defaulting ``released_at`` to the current UTC time."""
        return cls(
            claim_id=claim_id,
            owner=owner,
            released_at=utc_now_iso() if released_at is None else released_at,
        )

    def to_dict(self) -> dict[str, str]:
        """Deterministic ordered mapping for the JSONL wire format."""
        return {
            "event": self.EVENT_TYPE,
            "claim_id": self.claim_id,
            "released_at": self.released_at,
            "owner": self.owner,
        }

    def to_json_line(self) -> str:
        """Compact, deterministic single-line JSON (one ledger record).

        ``ensure_ascii=True`` guarantees the record is always exactly one
        physical line (defense in depth atop the field validation).
        """
        return json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":"))

    def to_text(self) -> str:
        """Deterministic human-readable ``key=value`` one-liner."""
        return " ".join(f"{key}={_safe_text_value(value)}" for key, value in self.to_dict().items())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseEvent:
        """Rebuild and re-validate a release from a parsed JSON object."""
        _expect_event(data, cls.EVENT_TYPE)
        return cls(
            claim_id=_field_str(data, "claim_id"),
            released_at=_field_str(data, "released_at"),
            owner=_field_str(data, "owner"),
        )


# ---------------------------------------------------------------------------
# ActiveClaim — DERIVED state, never stored on disk.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActiveClaim:
    """The derived shape of a currently-active claim.

    NEVER serialized to the ledger. Step 3 computes the active set by replaying
    claim events and dropping any that are released, expired, or superseded.
    Step 1 only freezes the type + the derivation helpers.
    """

    claim_id: str
    resource_kind: ResourceKind
    resource: str
    repository: str
    owner: str
    session_id: str
    created_at: str
    expires_at: str

    def __post_init__(self) -> None:
        # Full field validation (consistent with ClaimEvent) so a derived-but-
        # hand-built ActiveClaim cannot hold invalid fields. from_claim projects
        # from an already-validated ClaimEvent, so normalization is idempotent.
        _normalize_claim_fields(self)

    @classmethod
    def from_claim(cls, claim: ClaimEvent) -> ActiveClaim:
        """Project a validated :class:`ClaimEvent` into an active-claim shape."""
        return cls(
            claim_id=claim.claim_id,
            resource_kind=claim.resource_kind,
            resource=claim.resource,
            repository=claim.repository,
            owner=claim.owner,
            session_id=claim.session_id,
            created_at=claim.created_at,
            expires_at=claim.expires_at,
        )

    def is_expired(self, now: datetime | str | None = None) -> bool:
        """True when ``now`` is at or past ``expires_at`` (default: current UTC time)."""
        return _coerce_now(now) >= parse_utc_timestamp(self.expires_at, field_name="expires_at")

    def to_dict(self) -> dict[str, str]:
        """Deterministic ordered mapping (no ``event`` key — this is derived state)."""
        return {
            "claim_id": self.claim_id,
            "resource_kind": self.resource_kind.value,
            "resource": self.resource,
            "repository": self.repository,
            "owner": self.owner,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def to_text(self) -> str:
        """Deterministic human-readable ``key=value`` one-liner."""
        return " ".join(f"{key}={_safe_text_value(value)}" for key, value in self.to_dict().items())


# ---------------------------------------------------------------------------
# Wire-format dispatch (used by the ledger replay in Step 3).
# ---------------------------------------------------------------------------


def event_from_dict(data: Mapping[str, Any]) -> ClaimEvent | ReleaseEvent:
    """Dispatch a parsed JSON object to the correct event type by ``event`` tag."""
    event = data.get("event")
    if event == ClaimEvent.EVENT_TYPE:
        return ClaimEvent.from_dict(data)
    if event == ReleaseEvent.EVENT_TYPE:
        return ReleaseEvent.from_dict(data)
    raise MalformedEventError(f"unknown or missing event type: {event!r}")


def event_from_json_line(line: str) -> ClaimEvent | ReleaseEvent:
    """Parse one JSONL ledger line into a claim or release event.

    Corruption-tolerant by contract: the append-only ledger must survive a single
    poisoned line (a concurrent tool could append one). ANY parse-time failure is
    re-raised as a clean :class:`MalformedEventError` naming the offending line,
    never allowed to escape as an uncaught crash:

    - ``json.JSONDecodeError`` / other ``ValueError`` — invalid JSON, or an integer
      literal past the int-string-conversion limit (:func:`sys.get_int_max_str_digits`).
    - ``RecursionError`` — a recursion-bomb of deeply-nested arrays/objects.
    - ``TypeError`` — non-``str``/``bytes`` input reaching ``json.loads``.

    Schema validation of a well-formed-JSON-but-invalid event happens after the
    guard (in :func:`event_from_dict`), so those keep their specific error types.
    """
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError, RecursionError, TypeError) as exc:
        snippet = line if isinstance(line, str) else repr(line)
        if len(snippet) > 120:
            snippet = snippet[:120] + "..."
        raise MalformedEventError(
            f"ledger line is not parseable JSON ({type(exc).__name__}): {snippet!r}"
        ) from None
    if not isinstance(data, dict):
        raise MalformedEventError(f"event must be a JSON object; got {type(data).__name__}")
    return event_from_dict(data)


# ---------------------------------------------------------------------------
# Lifecycle predicates (frozen here; applied by the ledger in Step 3).
# ---------------------------------------------------------------------------

# A claim or its derived active projection — anything with resource identity.
ResourceEvent = ClaimEvent | ActiveClaim


def _same_actor(left: ResourceEvent, right: ResourceEvent) -> bool:
    """Same owner AND same session — the ONE frozen self-claim predicate.

    Supersession, self-vs-conflict classification, and the conflict rule all key
    off this single definition so the frozen rule can never drift across copies.
    """
    return left.owner == right.owner and left.session_id == right.session_id


class ClaimPairOutcome(StrEnum):
    """Relationship between an existing claim and an incoming claim on replay."""

    UNRELATED = "unrelated"  # different resource -> no interaction
    SUPERSEDES = "supersedes"  # same owner AND session -> TTL renewal (exit 0)
    CONFLICTS = "conflicts"  # same resource, different owner/session (exit 1)


class ReleaseOutcome(StrEnum):
    """Result of applying a release against ledger state."""

    APPLIED = "applied"  # active claim released -> exit 0
    NOOP_INACTIVE = "noop-inactive"  # already released/expired -> exit 0 + note
    UNKNOWN_CLAIM = "unknown-claim"  # claim_id not in ledger -> exit 2


def same_resource(left: ResourceEvent, right: ResourceEvent) -> bool:
    """Exact-resource identity: same repository, kind, and canonical resource.

    NOTE: ancestor/descendant path overlap is Step 2 (``conflicts.py``). Step 1
    freezes only this exact-match predicate.
    """
    return (
        left.repository == right.repository
        and left.resource_kind == right.resource_kind
        and left.resource == right.resource
    )


def is_supersession(existing: ResourceEvent, incoming: ResourceEvent) -> bool:
    """True when ``incoming`` renews ``existing`` (same resource + owner + session).

    This is TTL renewal by supersession, never a self-conflict.
    """
    return same_resource(existing, incoming) and _same_actor(existing, incoming)


def is_conflict(existing: ResourceEvent, incoming: ResourceEvent) -> bool:
    """True when two claims contend for the same resource across owner/session.

    Same owner with a *different* session_id is still a conflict.
    """
    return same_resource(existing, incoming) and not _same_actor(existing, incoming)


def classify_claim_pair(existing: ResourceEvent, incoming: ResourceEvent) -> ClaimPairOutcome:
    """Classify an incoming claim against an existing active claim."""
    if not same_resource(existing, incoming):
        return ClaimPairOutcome.UNRELATED
    if _same_actor(existing, incoming):
        return ClaimPairOutcome.SUPERSEDES
    return ClaimPairOutcome.CONFLICTS


def classify_release(*, claim_exists: bool, claim_active: bool) -> ReleaseOutcome:
    """Classify a release against ledger state.

    - unknown claim_id -> :attr:`ReleaseOutcome.UNKNOWN_CLAIM` (error, exit 2)
    - known but already released/expired -> :attr:`ReleaseOutcome.NOOP_INACTIVE`
      (idempotent no-op, exit 0 + note)
    - known and active -> :attr:`ReleaseOutcome.APPLIED` (exit 0)
    """
    if not claim_exists:
        return ReleaseOutcome.UNKNOWN_CLAIM
    if not claim_active:
        return ReleaseOutcome.NOOP_INACTIVE
    return ReleaseOutcome.APPLIED


def exit_code_for_claim_pair(outcome: ClaimPairOutcome) -> int:
    """Map a claim-pair outcome to its process exit code."""
    return EXIT_CONFLICT if outcome is ClaimPairOutcome.CONFLICTS else EXIT_SUCCESS


def exit_code_for_release(outcome: ReleaseOutcome) -> int:
    """Map a release outcome to its process exit code."""
    return EXIT_ERROR if outcome is ReleaseOutcome.UNKNOWN_CLAIM else EXIT_SUCCESS
