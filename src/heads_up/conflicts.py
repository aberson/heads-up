"""Overlap rules over canonicalized claims (build Step 2).

Decides whether two claims contend for the same work. Inputs are assumed
already canonical (see :mod:`heads_up.identity`) — this module compares, it
never normalizes.

Rules:

- **Different repository -> never conflict** (repository scoping): canonical
  repository strings are compared for equality, so two aliases of one repo
  contend while two different repos are isolated.
- **Same repository, same ``resource_kind``:**

  - ``path``: an EXACT match conflicts, and an ancestor/descendant overlap
    conflicts BOTH ways (``src`` vs ``src/foo/bar.py`` in either order). Overlap
    is component-boundary aware, not string-prefix: ``src/foo.py`` does NOT
    overlap ``src/foobar.py``.
  - ``repository`` / ``plan-step`` / ``issue`` / ``named-resource``: EXACT
    canonical match only; no ancestor/descendant semantics.

Actor rule (reused from the frozen Step-1 predicate ``models._same_actor``): a
same-actor (same owner AND session) pair is NEVER a conflict, even on an exact
resource — that is TTL renewal by supersession, or the same session extending
its own claim to a nested path. Different actor on any overlap is a conflict.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from heads_up.models import (
    ResourceEvent,
    ResourceKind,
    _same_actor,
    same_resource,
)

__all__ = [
    "ConflictEvidence",
    "OverlapKind",
    "claims_conflict",
    "find_conflicts",
    "resources_overlap",
]


class OverlapKind(StrEnum):
    """Structural relationship between two claims' resources (actor-independent)."""

    NONE = "none"  # different repo/kind, or no path ancestry -> no overlap
    EXACT = "exact"  # identical canonical resource
    ANCESTOR = "ancestor"  # left's path contains right's (left is the parent)
    DESCENDANT = "descendant"  # left's path is contained by right's (left is the child)


def _path_components(resource: str) -> tuple[str, ...]:
    """Split a canonical repo-relative path into comparison components.

    The repo root (``"."`` / empty) yields the empty tuple, which is a prefix of
    every path — i.e. an ancestor of everything under the repo.
    """
    if resource in ("", "."):
        return ()
    return tuple(part for part in resource.split("/") if part not in ("", "."))


def _is_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    """True when ``shorter`` is a whole-component prefix of ``longer``."""
    return len(shorter) <= len(longer) and longer[: len(shorter)] == shorter


def _path_overlap(left: str, right: str) -> OverlapKind:
    """Component-boundary-aware overlap of two canonical repo-relative paths."""
    a = _path_components(left)
    b = _path_components(right)
    if a == b:
        return OverlapKind.EXACT
    if _is_prefix(a, b):
        return OverlapKind.ANCESTOR
    if _is_prefix(b, a):
        return OverlapKind.DESCENDANT
    return OverlapKind.NONE


def resources_overlap(left: ResourceEvent, right: ResourceEvent) -> OverlapKind:
    """Structural overlap of two claims' resources, ignoring actor identity.

    Returns :attr:`OverlapKind.NONE` for a different repository, a different
    resource kind, or (for ``path``) two resources with no ancestor/descendant
    relation. Non-path kinds only ever return EXACT or NONE.
    """
    if left.repository != right.repository:
        return OverlapKind.NONE
    if left.resource_kind != right.resource_kind:
        return OverlapKind.NONE
    if left.resource_kind is ResourceKind.PATH:
        return _path_overlap(left.resource, right.resource)
    # Non-path kinds: exact canonical identity only. Reuse the frozen predicate.
    return OverlapKind.EXACT if same_resource(left, right) else OverlapKind.NONE


@dataclass(frozen=True, slots=True)
class ConflictEvidence:
    """Why two claims conflict — carries both claim IDs and the overlap reason.

    ``overlap`` is never :attr:`OverlapKind.NONE` here; a non-overlapping pair
    yields ``None`` from :func:`claims_conflict` rather than evidence.
    """

    existing: ResourceEvent
    incoming: ResourceEvent
    overlap: OverlapKind

    @property
    def existing_claim_id(self) -> str:
        """The claim ID of the already-present (first-appended) claim."""
        return self.existing.claim_id

    @property
    def incoming_claim_id(self) -> str:
        """The claim ID of the later, conflicting claim."""
        return self.incoming.claim_id

    @property
    def repository(self) -> str:
        """The shared canonical repository both claims target."""
        return self.incoming.repository

    @property
    def resource_kind(self) -> ResourceKind:
        """The shared resource kind of the conflicting pair."""
        return self.incoming.resource_kind

    def reason(self) -> str:
        """Human-readable explanation: exact vs ancestor/descendant overlap."""
        kind = self.incoming.resource_kind.value
        if self.overlap is OverlapKind.EXACT:
            return f"exact {kind} resource match on {self.incoming.resource!r}"
        if self.overlap is OverlapKind.ANCESTOR:
            return (
                f"existing path {self.existing.resource!r} is an ancestor of "
                f"incoming path {self.incoming.resource!r}"
            )
        return (
            f"existing path {self.existing.resource!r} is a descendant of "
            f"incoming path {self.incoming.resource!r}"
        )

    def to_dict(self) -> dict[str, str]:
        """Deterministic mapping for JSON conflict evidence (used by Step 4)."""
        return {
            "overlap": self.overlap.value,
            "reason": self.reason(),
            "existing_claim_id": self.existing_claim_id,
            "incoming_claim_id": self.incoming_claim_id,
            "repository": self.repository,
            "resource_kind": self.resource_kind.value,
            "existing_resource": self.existing.resource,
            "incoming_resource": self.incoming.resource,
        }


def claims_conflict(existing: ResourceEvent, incoming: ResourceEvent) -> ConflictEvidence | None:
    """Full conflict decision for one pair, honoring the frozen actor rule.

    Returns :class:`ConflictEvidence` when the pair is a real advisory conflict;
    ``None`` otherwise — including a same-actor pair (supersession on an exact
    resource, or a session nesting its own claim), which is never a conflict.
    """
    overlap = resources_overlap(existing, incoming)
    if overlap is OverlapKind.NONE:
        return None
    if _same_actor(existing, incoming):
        return None
    return ConflictEvidence(existing=existing, incoming=incoming, overlap=overlap)


def find_conflicts(
    incoming: ResourceEvent, existing: Iterable[ResourceEvent]
) -> list[ConflictEvidence]:
    """Collect every existing claim that conflicts with ``incoming``.

    Preserves the iteration order of ``existing`` so the first-appended
    conflicting claim (plan section 6, append order authoritative) leads.
    """
    conflicts: list[ConflictEvidence] = []
    for candidate in existing:
        evidence = claims_conflict(candidate, incoming)
        if evidence is not None:
            conflicts.append(evidence)
    return conflicts
