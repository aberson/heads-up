"""Schema validation tests for heads_up.models (frozen in Step 1)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from heads_up.models import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_SUCCESS,
    ActiveClaim,
    ClaimEvent,
    ClaimPairOutcome,
    InvalidClaimIdError,
    InvalidOwnerError,
    InvalidRepositoryError,
    InvalidResourceError,
    InvalidResourceKindError,
    InvalidSessionError,
    InvalidTimestampError,
    MalformedEventError,
    NonFiniteTTLError,
    ReleaseEvent,
    ReleaseOutcome,
    ResourceKind,
    SchemaError,
    _safe_text_value,
    classify_claim_pair,
    classify_release,
    event_from_dict,
    event_from_json_line,
    exit_code_description,
    exit_code_for_claim_pair,
    exit_code_for_release,
    is_conflict,
    is_supersession,
    new_claim_id,
    same_resource,
    validate_claim_id,
)

FIXTURES = Path(__file__).parent / "fixtures"

VALID_CLAIM_ID = "2f1e9c4a-7b3d-4e8f-9a6c-1d2e3f4a5b6c"
CREATED = "2026-07-28T10:00:00+00:00"
EXPIRES = "2026-07-28T12:00:00+00:00"


def make_claim(**overrides: object) -> ClaimEvent:
    params: dict[str, object] = {
        "resource_kind": "path",
        "resource": "c:/repo/src",
        "repository": "c:/repo",
        "owner": "abraham",
        "session_id": "sess-1",
        "created_at": CREATED,
        "expires_at": EXPIRES,
        "claim_id": VALID_CLAIM_ID,
    }
    params.update(overrides)
    return ClaimEvent.create(**params)  # type: ignore[arg-type]


# --- Happy-path round trips -------------------------------------------------


def test_claim_event_roundtrip_dict() -> None:
    claim = make_claim()
    rebuilt = ClaimEvent.from_dict(claim.to_dict())
    assert rebuilt == claim


def test_claim_event_roundtrip_json_line() -> None:
    claim = make_claim()
    line = claim.to_json_line()
    assert event_from_json_line(line) == claim
    # Compact + deterministic: no spaces after separators.
    assert ", " not in line
    assert '": ' not in line


def test_release_event_roundtrip() -> None:
    release = ReleaseEvent.create(claim_id=VALID_CLAIM_ID, owner="abraham", released_at=CREATED)
    rebuilt = ReleaseEvent.from_dict(release.to_dict())
    assert rebuilt == release
    assert event_from_json_line(release.to_json_line()) == release


def test_active_claim_from_claim_roundtrip() -> None:
    claim = make_claim()
    active = ActiveClaim.from_claim(claim)
    assert active.claim_id == claim.claim_id
    assert active.resource_kind is ResourceKind.PATH
    # Derived shape carries no event discriminator.
    assert "event" not in active.to_dict()


def test_active_claim_is_expired() -> None:
    active = ActiveClaim.from_claim(make_claim())
    assert active.is_expired("2026-07-28T12:00:00+00:00") is True  # boundary is inclusive
    assert active.is_expired("2026-07-28T11:59:59+00:00") is False


def test_claim_is_expired_helper() -> None:
    claim = make_claim()
    assert claim.is_expired("2026-07-28T13:00:00+00:00") is True
    assert claim.is_expired(CREATED) is False


# --- Explicit-failure validation -------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\t"])
def test_empty_resource_rejected(bad: str) -> None:
    with pytest.raises(InvalidResourceError):
        make_claim(resource=bad)


@pytest.mark.parametrize("bad", ["", "  "])
def test_empty_owner_rejected(bad: str) -> None:
    with pytest.raises(InvalidOwnerError):
        make_claim(owner=bad)


@pytest.mark.parametrize("bad", ["", "  "])
def test_empty_session_rejected(bad: str) -> None:
    with pytest.raises(InvalidSessionError):
        make_claim(session_id=bad)


@pytest.mark.parametrize("bad", ["", "  "])
def test_empty_repository_rejected(bad: str) -> None:
    with pytest.raises(InvalidRepositoryError):
        make_claim(repository=bad)


@pytest.mark.parametrize("bad", ["not-a-timestamp", "2026-13-01T00:00:00+00:00", ""])
def test_non_iso_timestamp_rejected(bad: str) -> None:
    with pytest.raises(InvalidTimestampError):
        make_claim(expires_at=bad)


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(InvalidTimestampError):
        make_claim(created_at="2026-07-28T10:00:00")


def test_non_utc_timestamp_rejected() -> None:
    with pytest.raises(InvalidTimestampError):
        make_claim(created_at="2026-07-28T10:00:00+05:00")


def test_non_finite_ttl_equal_rejected() -> None:
    with pytest.raises(NonFiniteTTLError):
        make_claim(created_at=EXPIRES, expires_at=EXPIRES)


def test_non_finite_ttl_negative_rejected() -> None:
    with pytest.raises(NonFiniteTTLError):
        make_claim(created_at=EXPIRES, expires_at=CREATED)


# --- claim_id / UUIDv4 validation ------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-uuid",
        "2f1e9c4a-7b3d-1e8f-9a6c-1d2e3f4a5b6c",  # version 1, not 4
        VALID_CLAIM_ID.upper(),  # non-canonical (uppercase)
        "{2f1e9c4a-7b3d-4e8f-9a6c-1d2e3f4a5b6c}",  # braces, non-canonical
    ],
)
def test_invalid_claim_id_rejected(bad: str) -> None:
    with pytest.raises(InvalidClaimIdError):
        make_claim(claim_id=bad)


def test_new_claim_id_is_valid_v4() -> None:
    generated = new_claim_id()
    assert validate_claim_id(generated) == generated
    assert uuid.UUID(generated).version == 4


# --- resource_kind enum -----------------------------------------------------


@pytest.mark.parametrize("kind", ["repository", "path", "plan-step", "issue", "named-resource"])
def test_all_resource_kinds_accepted(kind: str) -> None:
    claim = make_claim(resource_kind=kind)
    assert claim.resource_kind.value == kind


def test_invalid_resource_kind_rejected() -> None:
    with pytest.raises(InvalidResourceKindError):
        make_claim(resource_kind="worktree")


# --- Timestamp canonicalization --------------------------------------------


def test_timestamp_z_and_offset_canonicalize_equal() -> None:
    with_z = make_claim(created_at="2026-07-28T10:00:00Z")
    with_offset = make_claim(created_at="2026-07-28T10:00:00+00:00")
    assert with_z.created_at == "2026-07-28T10:00:00+00:00"
    assert with_z == with_offset


# --- Lifecycle predicates ---------------------------------------------------


def test_supersession_same_owner_and_session() -> None:
    existing = make_claim(claim_id=None)
    incoming = make_claim(claim_id=None)  # same resource + owner + session
    assert is_supersession(existing, incoming) is True
    assert is_conflict(existing, incoming) is False
    assert classify_claim_pair(existing, incoming) is ClaimPairOutcome.SUPERSEDES
    assert exit_code_for_claim_pair(ClaimPairOutcome.SUPERSEDES) == EXIT_SUCCESS


def test_conflict_same_owner_different_session() -> None:
    existing = make_claim(claim_id=None, session_id="sess-A")
    incoming = make_claim(claim_id=None, session_id="sess-B")
    assert is_conflict(existing, incoming) is True
    assert is_supersession(existing, incoming) is False
    assert classify_claim_pair(existing, incoming) is ClaimPairOutcome.CONFLICTS
    assert exit_code_for_claim_pair(ClaimPairOutcome.CONFLICTS) == EXIT_CONFLICT


def test_conflict_different_owner() -> None:
    existing = make_claim(claim_id=None, owner="alice")
    incoming = make_claim(claim_id=None, owner="bob")
    assert classify_claim_pair(existing, incoming) is ClaimPairOutcome.CONFLICTS


def test_unrelated_different_resource() -> None:
    existing = make_claim(claim_id=None, resource="c:/repo/a")
    incoming = make_claim(claim_id=None, resource="c:/repo/b")
    assert same_resource(existing, incoming) is False
    assert classify_claim_pair(existing, incoming) is ClaimPairOutcome.UNRELATED


def test_unrelated_different_repository() -> None:
    existing = make_claim(claim_id=None, repository="c:/repo-a")
    incoming = make_claim(claim_id=None, repository="c:/repo-b")
    assert classify_claim_pair(existing, incoming) is ClaimPairOutcome.UNRELATED


# --- Release idempotency ----------------------------------------------------


def test_classify_release_applied() -> None:
    outcome = classify_release(claim_exists=True, claim_active=True)
    assert outcome is ReleaseOutcome.APPLIED
    assert exit_code_for_release(outcome) == EXIT_SUCCESS


def test_classify_release_noop_when_inactive() -> None:
    outcome = classify_release(claim_exists=True, claim_active=False)
    assert outcome is ReleaseOutcome.NOOP_INACTIVE
    assert exit_code_for_release(outcome) == EXIT_SUCCESS


def test_classify_release_unknown_is_error() -> None:
    outcome = classify_release(claim_exists=False, claim_active=False)
    assert outcome is ReleaseOutcome.UNKNOWN_CLAIM
    assert exit_code_for_release(outcome) == EXIT_ERROR


# --- Exit codes -------------------------------------------------------------


def test_exit_code_constants() -> None:
    assert (EXIT_SUCCESS, EXIT_CONFLICT, EXIT_ERROR) == (0, 1, 2)


def test_exit_code_description_roundtrip() -> None:
    assert exit_code_description(EXIT_CONFLICT) == "advisory conflict reported"


def test_exit_code_description_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown exit code"):
        exit_code_description(7)


# --- Wire-format dispatch and malformed events ------------------------------


def test_event_from_dict_dispatch() -> None:
    claim = make_claim()
    release = ReleaseEvent.create(claim_id=VALID_CLAIM_ID, owner="abraham", released_at=CREATED)
    assert event_from_dict(claim.to_dict()) == claim
    assert event_from_dict(release.to_dict()) == release


def test_event_from_dict_unknown_event_rejected() -> None:
    with pytest.raises(MalformedEventError):
        event_from_dict({"event": "mystery"})


def test_from_dict_missing_field_rejected() -> None:
    data = make_claim().to_dict()
    del data["owner"]
    with pytest.raises(MalformedEventError):
        ClaimEvent.from_dict(data)


def test_from_dict_wrong_type_rejected() -> None:
    data: dict[str, object] = {**make_claim().to_dict()}
    data["resource"] = 123
    with pytest.raises(MalformedEventError):
        ClaimEvent.from_dict(data)


def test_from_dict_wrong_event_rejected() -> None:
    release_data = ReleaseEvent.create(
        claim_id=VALID_CLAIM_ID, owner="abraham", released_at=CREATED
    ).to_dict()
    with pytest.raises(MalformedEventError):
        ClaimEvent.from_dict(release_data)


def test_event_from_json_line_bad_json_rejected() -> None:
    with pytest.raises(MalformedEventError):
        event_from_json_line("{not json")


# --- Frozen golden fixtures -------------------------------------------------


def test_golden_claim_fixture() -> None:
    line = (FIXTURES / "golden_claim_event.jsonl").read_text(encoding="utf-8").strip()
    claim = event_from_json_line(line)
    assert isinstance(claim, ClaimEvent)
    assert claim.claim_id == VALID_CLAIM_ID
    assert claim.resource_kind is ResourceKind.PATH
    assert claim.owner == "abraham"
    assert claim.session_id == "sess-001"
    # The wire format is frozen: re-serialization must be byte-identical.
    assert claim.to_json_line() == line
    assert json.loads(line)["event"] == "claim"


def test_golden_release_fixture() -> None:
    line = (FIXTURES / "golden_release_event.jsonl").read_text(encoding="utf-8").strip()
    release = event_from_json_line(line)
    assert isinstance(release, ReleaseEvent)
    assert release.claim_id == VALID_CLAIM_ID
    assert release.owner == "abraham"
    assert release.to_json_line() == line


# --- Determinism of text form ----------------------------------------------


def test_to_text_is_deterministic() -> None:
    claim = make_claim()
    assert claim.to_text() == claim.to_text()
    assert claim.to_text().startswith("event=claim claim_id=")


# --- Parser safety: a poisoned ledger line is a clean SchemaError, never a crash ---
# The append-only ledger must tolerate a single corrupt line (a concurrent tool
# could append one). Every parse-time failure re-raises as MalformedEventError.

LINE_BREAKERS = ["\n", "\r", "\u0085", "\u2028", "\u2029"]


def test_deeply_nested_json_line_is_clean_schema_error() -> None:
    # A recursion-bomb of nested arrays raises RecursionError inside json.loads;
    # it must be wrapped, not allowed to escape as an uncaught crash.
    bomb = "[" * 100_000 + "]" * 100_000
    with pytest.raises(MalformedEventError):
        event_from_json_line(bomb)


def test_huge_int_json_line_is_clean_schema_error() -> None:
    # An integer literal past sys.get_int_max_str_digits() raises ValueError.
    huge = "1" + "0" * 6000
    with pytest.raises(MalformedEventError):
        event_from_json_line(huge)


def test_non_str_json_line_is_clean_schema_error() -> None:
    # Non-str/bytes input raises TypeError inside json.loads; must be wrapped.
    with pytest.raises(MalformedEventError):
        event_from_json_line(123)  # type: ignore[arg-type]


def test_truncated_json_line_is_clean_schema_error() -> None:
    with pytest.raises(MalformedEventError):
        event_from_json_line('{"event":"claim","claim_id":')


def test_all_parse_failures_are_catchable_as_schema_error() -> None:
    # No parse failure escapes the single SchemaError base (MalformedEventError).
    poisoned = ["{not json", "1" + "0" * 6000, "[" * 50_000 + "]" * 50_000, "3.14", '"scalar"']
    for bad in poisoned:
        with pytest.raises(SchemaError):
            event_from_json_line(bad)


# --- JSONL integrity: line-breaking chars are rejected + output is one line ---


@pytest.mark.parametrize("bad_char", [*LINE_BREAKERS, "\t", "\x00", "\x7f"])
@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("resource", InvalidResourceError),
        ("repository", InvalidRepositoryError),
        ("owner", InvalidOwnerError),
        ("session_id", InvalidSessionError),
    ],
)
def test_line_breaking_char_in_field_rejected(
    field: str, error: type[SchemaError], bad_char: str
) -> None:
    with pytest.raises(error):
        make_claim(**{field: f"valid{bad_char}value"})


def test_to_json_line_is_always_one_physical_line() -> None:
    line = make_claim().to_json_line()
    assert line.count("\n") == 0
    assert line.count("\r") == 0
    assert len(line.splitlines()) == 1


def test_to_json_line_escapes_non_ascii_to_stay_one_line() -> None:
    # Cyrillic owner is a legit (non-line-breaking) field, but ensure_ascii=True
    # escapes it so the record is guaranteed pure-ASCII / exactly one physical line.
    claim = make_claim(owner="\u0430\u0431\u0440\u0430\u0445\u0430\u043c")  # "абрахам"
    line = claim.to_json_line()
    assert line.isascii()
    assert line.count("\n") == 0
    assert len(line.splitlines()) == 1
    assert event_from_json_line(line) == claim  # round-trips back to the same event


def test_release_to_json_line_is_one_physical_line() -> None:
    release = ReleaseEvent.create(claim_id=VALID_CLAIM_ID, owner="abraham", released_at=CREATED)
    line = release.to_json_line()
    assert line.isascii()
    assert len(line.splitlines()) == 1


# --- ActiveClaim direct construction is validated (not just via from_claim) ---


def _active_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "claim_id": VALID_CLAIM_ID,
        "resource_kind": "path",
        "resource": "c:/repo/src",
        "repository": "c:/repo",
        "owner": "abraham",
        "session_id": "sess-1",
        "created_at": CREATED,
        "expires_at": EXPIRES,
    }
    base.update(overrides)
    return base


def test_active_claim_direct_construction_validates_claim_id() -> None:
    with pytest.raises(InvalidClaimIdError):
        ActiveClaim(**_active_kwargs(claim_id="not-a-uuid"))  # type: ignore[arg-type]


def test_active_claim_direct_construction_rejects_empty_owner() -> None:
    with pytest.raises(InvalidOwnerError):
        ActiveClaim(**_active_kwargs(owner="   "))  # type: ignore[arg-type]


def test_active_claim_direct_construction_rejects_line_breaking_field() -> None:
    with pytest.raises(InvalidResourceError):
        ActiveClaim(**_active_kwargs(resource="a\u2028b"))  # type: ignore[arg-type]


def test_active_claim_direct_construction_rejects_non_finite_ttl() -> None:
    with pytest.raises(NonFiniteTTLError):
        ActiveClaim(**_active_kwargs(created_at=EXPIRES, expires_at=CREATED))  # type: ignore[arg-type]


def test_active_claim_from_valid_claim_unaffected() -> None:
    active = ActiveClaim.from_claim(make_claim())
    assert active.owner == "abraham"
    assert active.resource_kind is ResourceKind.PATH


# --- to_text is single-line + control-char safe (defense in depth) ---


def test_safe_text_value_escapes_line_breakers() -> None:
    assert _safe_text_value("a\nb") == "a\\u000ab"
    assert _safe_text_value("a\u2028b") == "a\\u2028b"
    assert _safe_text_value("plain-value") == "plain-value"


def test_to_text_is_single_line_for_every_shape() -> None:
    claim = make_claim()
    assert len(claim.to_text().splitlines()) == 1
    release = ReleaseEvent.create(claim_id=VALID_CLAIM_ID, owner="abraham", released_at=CREATED)
    assert len(release.to_text().splitlines()) == 1
    assert len(ActiveClaim.from_claim(claim).to_text().splitlines()) == 1


# --- Supersession predicate is single-sourced (finding 5 regression guard) ---


def test_supersession_predicates_stay_mutually_consistent() -> None:
    # is_supersession / is_conflict / classify_claim_pair share ONE self-actor
    # predicate now, so they must never disagree on the same pair.
    existing = make_claim(claim_id=None)
    cases: list[tuple[ClaimEvent, bool]] = [
        (make_claim(claim_id=None), True),  # same owner + session -> supersession
        (make_claim(claim_id=None, session_id="sess-Z"), False),  # diff session -> conflict
        (make_claim(claim_id=None, owner="someone-else"), False),  # diff owner -> conflict
    ]
    for incoming, superseding in cases:
        assert is_supersession(existing, incoming) is superseding
        assert is_conflict(existing, incoming) is (not superseding)
        outcome = classify_claim_pair(existing, incoming)
        assert (outcome is ClaimPairOutcome.SUPERSEDES) is superseding
        assert (outcome is ClaimPairOutcome.CONFLICTS) is (not superseding)
