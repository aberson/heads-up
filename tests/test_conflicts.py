"""Overlap/conflict rules for heads_up.conflicts (build Step 2).

Claims here carry ALREADY-canonical resource strings (lowercase, forward-slash),
mirroring what heads_up.identity produces at the CLI boundary. The final block
runs identity -> conflicts end to end so the producer/consumer relationship is
exercised, not just each endpoint.
"""

from __future__ import annotations

import os

import pytest

from heads_up.conflicts import (
    ConflictEvidence,
    OverlapKind,
    claims_conflict,
    find_conflicts,
    resources_overlap,
)
from heads_up.identity import canonicalize_path_resource, canonicalize_repository
from heads_up.models import ClaimEvent, classify_claim_pair
from heads_up.models import ClaimPairOutcome as CPO

CREATED = "2026-07-28T10:00:00+00:00"
EXPIRES = "2026-07-28T12:00:00+00:00"

WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="case-fold identity is Windows-only")


def claim(
    resource: str,
    *,
    kind: str = "path",
    repository: str = "c:/repo",
    owner: str = "alice",
    session: str = "s1",
) -> ClaimEvent:
    return ClaimEvent.create(
        resource_kind=kind,
        resource=resource,
        repository=repository,
        owner=owner,
        session_id=session,
        created_at=CREATED,
        expires_at=EXPIRES,
    )


# ---------------------------------------------------------------------------
# Path ancestry — conflicts BOTH ways, component-boundary aware.
# ---------------------------------------------------------------------------


def test_nested_path_conflicts_ancestor_direction() -> None:
    parent = claim("src", owner="alice")
    child = claim("src/foo/bar.py", owner="bob")
    ev = claims_conflict(parent, child)
    assert ev is not None
    assert ev.overlap is OverlapKind.ANCESTOR
    assert resources_overlap(parent, child) is OverlapKind.ANCESTOR


def test_nested_path_conflicts_descendant_direction() -> None:
    child = claim("src/foo/bar.py", owner="alice")
    parent = claim("src", owner="bob")
    ev = claims_conflict(child, parent)
    assert ev is not None
    assert ev.overlap is OverlapKind.DESCENDANT
    assert resources_overlap(child, parent) is OverlapKind.DESCENDANT


def test_nested_paths_conflict_symmetrically() -> None:
    # "both ways": whichever is appended first, the later one still conflicts.
    a = claim("src", owner="alice")
    b = claim("src/foo/bar.py", owner="bob")
    assert claims_conflict(a, b) is not None
    assert claims_conflict(b, a) is not None


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("src/foo.py", "src/foobar.py"),  # string-prefix but different component
        ("src", "srcxtra"),  # component-boundary
        ("a/b", "a/c"),  # divergent sibling
        ("a/b/c", "a/b/d"),  # divergent leaf
    ],
)
def test_component_boundary_no_false_conflict(left: str, right: str) -> None:
    a = claim(left, owner="alice")
    b = claim(right, owner="bob")
    assert resources_overlap(a, b) is OverlapKind.NONE
    assert claims_conflict(a, b) is None
    assert claims_conflict(b, a) is None


def test_exact_path_conflicts() -> None:
    a = claim("src/foo.py", owner="alice")
    b = claim("src/foo.py", owner="bob")
    ev = claims_conflict(a, b)
    assert ev is not None
    assert ev.overlap is OverlapKind.EXACT


def test_repo_root_path_is_ancestor_of_everything() -> None:
    root = claim(".", owner="alice")
    leaf = claim("src/foo.py", owner="bob")
    assert resources_overlap(root, leaf) is OverlapKind.ANCESTOR
    assert claims_conflict(root, leaf) is not None


# ---------------------------------------------------------------------------
# Repository scoping — different repositories NEVER conflict.
# ---------------------------------------------------------------------------


def test_different_repository_never_conflicts_even_on_identical_resource() -> None:
    a = claim("src/foo.py", repository="c:/repo-a", owner="alice")
    b = claim("src/foo.py", repository="c:/repo-b", owner="bob")
    assert resources_overlap(a, b) is OverlapKind.NONE
    assert claims_conflict(a, b) is None


def test_different_repository_never_conflicts_on_nested_paths() -> None:
    a = claim("src", repository="c:/repo-a", owner="alice")
    b = claim("src/foo/bar.py", repository="c:/repo-b", owner="bob")
    assert claims_conflict(a, b) is None


# ---------------------------------------------------------------------------
# Different resource kinds never overlap.
# ---------------------------------------------------------------------------


def test_different_kind_same_string_no_conflict() -> None:
    a = claim("src", kind="path", owner="alice")
    b = claim("src", kind="named-resource", owner="bob")
    assert resources_overlap(a, b) is OverlapKind.NONE
    assert claims_conflict(a, b) is None


# ---------------------------------------------------------------------------
# Non-path kinds — EXACT match only, NO ancestor/descendant semantics.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["issue", "plan-step", "named-resource", "repository"])
def test_non_path_exact_conflicts(kind: str) -> None:
    value = "c:/repo" if kind == "repository" else "token-1"
    a = claim(value, kind=kind, owner="alice")
    b = claim(value, kind=kind, owner="bob")
    ev = claims_conflict(a, b)
    assert ev is not None
    assert ev.overlap is OverlapKind.EXACT


@pytest.mark.parametrize("kind", ["issue", "plan-step", "named-resource"])
def test_non_path_distinct_no_conflict(kind: str) -> None:
    a = claim("token-1", kind=kind, owner="alice")
    b = claim("token-2", kind=kind, owner="bob")
    assert claims_conflict(a, b) is None


def test_non_path_no_ancestor_semantics() -> None:
    # A path-shaped value under a non-path kind gets NO ancestry treatment.
    a = claim("a/b", kind="named-resource", owner="alice")
    b = claim("a/b/c", kind="named-resource", owner="bob")
    assert resources_overlap(a, b) is OverlapKind.NONE
    assert claims_conflict(a, b) is None


# ---------------------------------------------------------------------------
# Actor rule — same actor is never a conflict (reused frozen predicate).
# ---------------------------------------------------------------------------


def test_same_actor_exact_is_not_a_conflict() -> None:
    a = claim("src/foo.py", owner="alice", session="s1")
    b = claim("src/foo.py", owner="alice", session="s1")  # supersession
    assert resources_overlap(a, b) is OverlapKind.EXACT
    assert claims_conflict(a, b) is None


def test_same_actor_nested_is_not_a_conflict() -> None:
    # A session extending its own claim to a sub-path does not self-conflict.
    a = claim("src", owner="alice", session="s1")
    b = claim("src/foo/bar.py", owner="alice", session="s1")
    assert resources_overlap(a, b) is OverlapKind.ANCESTOR
    assert claims_conflict(a, b) is None
    assert claims_conflict(b, a) is None


def test_same_owner_different_session_exact_conflicts() -> None:
    a = claim("src/foo.py", owner="alice", session="s1")
    b = claim("src/foo.py", owner="alice", session="s2")
    assert claims_conflict(a, b) is not None


def test_same_owner_different_session_nested_conflicts() -> None:
    a = claim("src", owner="alice", session="s1")
    b = claim("src/foo.py", owner="alice", session="s2")
    assert claims_conflict(a, b) is not None


def test_exact_verdict_matches_models_classification() -> None:
    # For EXACT overlaps, conflicts.py must agree with the frozen models rule.
    cases = [
        (claim("src/x.py", owner="alice", session="s1"), claim("src/x.py", owner="bob")),
        (
            claim("src/x.py", owner="alice", session="s1"),
            claim("src/x.py", owner="alice", session="s1"),
        ),
        (
            claim("src/x.py", owner="alice", session="s1"),
            claim("src/x.py", owner="alice", session="s2"),
        ),
    ]
    for existing, incoming in cases:
        conflicts_says = claims_conflict(existing, incoming) is not None
        models_says = classify_claim_pair(existing, incoming) is CPO.CONFLICTS
        assert conflicts_says == models_says


# ---------------------------------------------------------------------------
# Conflict evidence — both claim IDs + a reason string.
# ---------------------------------------------------------------------------


def test_evidence_carries_both_claim_ids_and_reason() -> None:
    existing = claim("src", owner="alice")
    incoming = claim("src/foo/bar.py", owner="bob")
    ev = claims_conflict(existing, incoming)
    assert isinstance(ev, ConflictEvidence)
    assert ev.existing_claim_id == existing.claim_id
    assert ev.incoming_claim_id == incoming.claim_id
    assert ev.existing_claim_id != ev.incoming_claim_id
    body = ev.to_dict()
    assert body["overlap"] == "ancestor"
    assert body["existing_claim_id"] == existing.claim_id
    assert body["incoming_claim_id"] == incoming.claim_id
    assert "ancestor" in ev.reason()


def test_evidence_exact_reason_names_resource() -> None:
    ev = claims_conflict(claim("src/foo.py", owner="alice"), claim("src/foo.py", owner="bob"))
    assert ev is not None
    assert "exact" in ev.reason()
    assert "src/foo.py" in ev.reason()


# ---------------------------------------------------------------------------
# find_conflicts over a candidate set.
# ---------------------------------------------------------------------------


def test_find_conflicts_returns_only_overlaps_in_order() -> None:
    incoming = claim("src/foo/bar.py", owner="bob")
    existing = [
        claim("src", owner="alice"),  # ancestor -> conflict
        claim("docs/readme.md", owner="carol"),  # unrelated
        claim("src/foo/bar.py", owner="dave"),  # exact -> conflict
        claim("src/foo/bar.py", owner="bob"),  # same actor -> NOT a conflict
    ]
    found = find_conflicts(incoming, existing)
    assert [ev.existing_claim_id for ev in found] == [existing[0].claim_id, existing[2].claim_id]


def test_find_conflicts_empty_when_none_overlap() -> None:
    incoming = claim("src/only.py", owner="bob")
    existing = [claim("docs/a.md", owner="alice"), claim("tests/b.py", owner="carol")]
    assert find_conflicts(incoming, existing) == []


# ---------------------------------------------------------------------------
# Integration: identity (producer) -> conflicts (consumer) round trip.
# ---------------------------------------------------------------------------


@windows_only
def test_identity_then_conflicts_exact_from_aliases(tmp_path: object) -> None:
    # Case-fold aliasing ("Src\\Foo.py" == "src/foo.py") is Windows-only; on a
    # case-sensitive runner these are two different files, so gate the test.
    repo_raw = str(tmp_path)
    repo = canonicalize_repository(repo_raw)
    left = canonicalize_path_resource("Src\\Foo.py", repository=repo_raw)
    right = canonicalize_path_resource("src/foo.py", repository=repo_raw)
    a = claim(left, repository=repo, owner="alice")
    b = claim(right, repository=repo, owner="bob")
    ev = claims_conflict(a, b)
    assert ev is not None
    assert ev.overlap is OverlapKind.EXACT


def test_identity_then_conflicts_ancestor(tmp_path: object) -> None:
    repo_raw = str(tmp_path)
    repo = canonicalize_repository(repo_raw)
    parent = canonicalize_path_resource("src", repository=repo_raw)
    child = canonicalize_path_resource("src/foo/bar.py", repository=repo_raw)
    a = claim(parent, repository=repo, owner="alice")
    b = claim(child, repository=repo, owner="bob")
    assert claims_conflict(a, b) is not None


def test_identity_repository_aliases_scope_together(tmp_path: object) -> None:
    # Two spellings of ONE repo canonicalize equal, so their claims share scope.
    repo_a = canonicalize_repository(str(tmp_path))
    repo_b = canonicalize_repository(str(tmp_path) + "/sub/..")
    assert repo_a == repo_b
    a = claim("src/foo.py", repository=repo_a, owner="alice")
    b = claim("src/foo.py", repository=repo_b, owner="bob")
    assert claims_conflict(a, b) is not None
