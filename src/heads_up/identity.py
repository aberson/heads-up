"""Canonical identity for claim fields (build Step 2).

Turns caller-supplied repository roots, resource identifiers, and actor
identities into stable canonical strings so that two spellings of the same
thing compare equal. ``conflicts.py`` compares the canonical strings this
module produces; it never re-normalizes. The CLI (Step 4) canonicalizes at the
input boundary before constructing a :class:`~heads_up.models.ClaimEvent`.

Two aliases of the same repository MUST canonicalize identically; two different
repositories must NOT collide. The same guarantee holds for ``path`` resources.

**Pure-string / disk-free by design.** Canonicalization NEVER touches the
filesystem or the network — no ``realpath``, no ``expanduser``, no ``stat``, no
UNC handshake. Claims are *advisory intent* and routinely name paths that do
not exist yet, so identity must not depend on what happens to be on disk. Two
consequences follow, both deliberate:

- **Deterministic over time.** ``os.path.realpath`` returns a value that depends
  on which files/symlinks currently exist, so the same logical claim would
  canonicalize differently as the tree changes — silently widening or narrowing
  a conflict scope. Pure lexical resolution removes that: the identity of a
  claim depends only on the string the caller wrote.
- **No network / no hang.** A ``\\\\host\\share`` (UNC) claim is treated as a
  plain string; it never triggers an outbound SMB/NTLM handshake that a dead
  share could hang on.

Tradeoff (accepted): because we do NOT follow symlinks, two different symlink
aliases pointing at the same real directory are treated as DISTINCT identities.
That is correct for an advisory tool — it claims the path you name, not whatever
the filesystem currently resolves that name to.

What "resolve" (plan section 8) means here is LEXICAL resolution only:

- **``.`` / ``..`` and redundant/trailing separators** — collapsed lexically
  (``src/../src/foo.py`` -> ``src/foo.py``; ``src//foo.py`` -> ``src/foo.py``),
  never by consulting the filesystem.
- **Separator drift** — ``\\`` vs ``/``. Canonical form is always forward-slash.
- **Case-insensitivity** — ``C:\\Repo`` and ``c:\\repo`` are one directory on
  Windows. Repository roots and ``path`` resources are lower-cased on Windows
  (``os.name == "nt"``, drive letter included) and left untouched elsewhere:
  POSIX filesystems are case-sensitive (``src/Foo.py`` != ``src/foo.py``). We use
  ``str.lower`` rather than ``str.casefold`` on purpose: casefold expands the
  German sharp-s so ``Strasse.py`` and the sharp-s spelling would collapse to one
  identity — two genuinely distinct filenames. ``lower`` keeps them apart.
- **``\\\\?\\`` extended-length and ``\\\\?\\UNC\\`` prefixes** — stripped so an
  extended-length spelling and a normal one canonicalize identically.

A ``path`` resource is repo-root-relative and MUST stay inside its repository: a
resource that lexically escapes the root (normalizes to a leading ``..``) is
rejected as invalid input, not silently retained.

Non-path kinds (``owner``/``session``/``issue``/``plan-step``/``named-resource``)
are trimmed to an exact canonical string and are NEVER path-normalized — an
issue id like ``#42`` is compared as an opaque token, not a filesystem path.
"""

from __future__ import annotations

import os
import unicodedata

from heads_up.models import (
    _LINE_BREAKING_CATEGORIES,
    InvalidOwnerError,
    InvalidRepositoryError,
    InvalidResourceError,
    InvalidSessionError,
    ResourceKind,
    SchemaError,
)

__all__ = [
    "canonicalize_issue",
    "canonicalize_named_resource",
    "canonicalize_owner",
    "canonicalize_path_resource",
    "canonicalize_plan_step",
    "canonicalize_repository",
    "canonicalize_resource",
    "canonicalize_session",
]

# Lower-case path identity only where the filesystem is case-insensitive.
_WINDOWS: bool = os.name == "nt"

_EXTENDED_PREFIX = "\\\\?\\"
_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"


# ---------------------------------------------------------------------------
# Shared helpers.
# ---------------------------------------------------------------------------


def _reject_line_breaking(value: str, error: type[SchemaError], field_name: str) -> None:
    """Reject control chars / line & paragraph separators (NUL included).

    Uses the SAME category set the frozen Step-1 schema rejects at
    ``ClaimEvent`` construction (``models._LINE_BREAKING_CATEGORIES`` — Cc/Zl/Zp,
    which covers NUL). Canonicalization runs at the CLI boundary *before* the
    event is built, so a NUL or newline in a raw path surfaces here as a clean
    schema error instead of a bare ``ValueError`` from a filesystem call, and the
    canonical string can never re-introduce a separator that would break the
    one-event-per-line JSONL invariant.
    """
    for ch in value:
        if unicodedata.category(ch) in _LINE_BREAKING_CATEGORIES:
            raise error(
                f"{field_name} must not contain control characters or line/paragraph "
                f"separators (would corrupt the one-event-per-line ledger); got {value!r}"
            )


def _require_text(raw: str, error: type[SchemaError], field_name: str) -> str:
    """Return ``raw.strip()`` or raise ``error`` for empty/whitespace/non-str.

    Also rejects embedded control characters (NUL, newline, ...) so every
    canonical field is a single clean line.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise error(f"{field_name} must be a non-empty string; got {raw!r}")
    stripped = raw.strip()
    _reject_line_breaking(stripped, error, field_name)
    return stripped


def _strip_extended_prefix(path: str) -> str:
    """Drop a Windows ``\\\\?\\`` / ``\\\\?\\UNC\\`` extended-length prefix.

    ``\\\\?\\UNC\\server\\share`` becomes ``\\\\server\\share`` and
    ``\\\\?\\C:\\x`` becomes ``C:\\x`` so extended and normal spellings of the
    same location canonicalize to one value.
    """
    if path.startswith(_EXTENDED_UNC_PREFIX):
        return "\\\\" + path[len(_EXTENDED_UNC_PREFIX) :]
    if path.startswith(_EXTENDED_PREFIX):
        return path[len(_EXTENDED_PREFIX) :]
    return path


def _fold(value: str) -> str:
    """Lower-case on Windows; identity on case-sensitive filesystems.

    ``str.lower`` (not ``str.casefold``) so the German sharp-s does NOT expand to
    ``ss`` — casefold would collapse two distinct filenames into one identity.
    """
    return value.lower() if _WINDOWS else value


def _split_anchor(slashed: str) -> tuple[str, str]:
    """Split a forward-slashed path into ``(anchor, remainder)`` — disk-free.

    ``anchor`` is ``""`` (relative), ``"/"`` (POSIX root), ``"c:"`` (drive), or
    ``"//host/share"`` (UNC). ``remainder`` is the rest, still forward-slashed.
    """
    if slashed.startswith("//"):
        segs = slashed[2:].split("/")
        host = segs[0] if segs else ""
        share = segs[1] if len(segs) > 1 else ""
        return f"//{host}/{share}", "/".join(segs[2:])
    if slashed.startswith("/"):
        return "/", slashed[1:]
    if len(slashed) >= 2 and slashed[1] == ":" and slashed[0].isascii() and slashed[0].isalpha():
        return slashed[:2], slashed[2:].lstrip("/")
    return "", slashed


def _resolve_components(remainder: str, *, clamp: bool) -> tuple[list[str], bool]:
    """Lexically resolve ``.`` / ``..`` / empty parts in a forward-slashed body.

    Returns ``(components, escaped)``. ``escaped`` is True when a ``..`` rises
    above the start of a RELATIVE path. For an ABSOLUTE path (``clamp=True``) a
    ``..`` at the anchor is a no-op (root-clamped, matching OS semantics) and
    never escapes.
    """
    comps: list[str] = []
    escaped = False
    for part in remainder.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if comps and comps[-1] != "..":
                comps.pop()
            elif clamp:
                continue
            else:
                comps.append("..")
                escaped = True
        else:
            comps.append(part)
    return comps, escaped


def _join_anchor(anchor: str, comps: list[str]) -> str:
    """Rejoin an anchor and its resolved components into a forward-slash path."""
    body = "/".join(comps)
    if anchor == "":
        return body or "."
    if anchor == "/":
        return "/" + body
    if anchor.startswith("//"):
        return f"{anchor}/{body}" if body else anchor
    # Drive anchor ("c:"): keep a root slash even with no body.
    return f"{anchor}/{body}" if body else f"{anchor}/"


def _split_normalize(raw: str) -> tuple[str, list[str]]:
    """Disk-free split of a native path into ``(anchor, resolved-components)``."""
    slashed = _strip_extended_prefix(raw).replace("\\", "/")
    anchor, remainder = _split_anchor(slashed)
    comps, _ = _resolve_components(remainder, clamp=True)
    return anchor, comps


def _normalize_native_path(raw: str) -> str:
    """Absolute, lexically-resolved, forward-slash, case-folded native path."""
    anchor, comps = _split_normalize(raw)
    return _fold(_join_anchor(anchor, comps))


def _lexical_relpath(base: list[str], target: list[str]) -> list[str]:
    """Repo-relative component list from ``base`` (repo) to ``target``.

    Case-insensitive on Windows (via :func:`_fold`) so a repo and an in-repo
    absolute path that differ only in case still relativize. A leading ``..`` in
    the result means ``target`` is outside ``base`` (an escape).
    """
    i = 0
    while i < len(base) and i < len(target) and _fold(base[i]) == _fold(target[i]):
        i += 1
    return [".."] * (len(base) - i) + target[i:]


# ---------------------------------------------------------------------------
# Repository + path resource canonicalization.
# ---------------------------------------------------------------------------


def canonicalize_repository(raw: str) -> str:
    """Canonicalize a repository root to a stable absolute identity.

    Pure lexical resolution: ``.``/``..``/redundant/trailing separators
    collapsed, extended-length prefix stripped, forward-slashed, and
    lower-cased on Windows (drive letter included). No filesystem or network
    access. Two aliases of one repo produce the same string; two different repos
    do not.
    """
    stripped = _require_text(raw, InvalidRepositoryError, "repository")
    return _normalize_native_path(stripped)


def canonicalize_path_resource(raw: str, *, repository: str) -> str:
    """Canonicalize a ``path`` resource to a repo-root-relative POSIX identity.

    ``raw`` may be repo-relative (``Src\\Foo.py``) or an absolute path inside the
    repository; both collapse to the same repo-relative, forward-slash,
    lower-cased (on Windows) string, preserving enough structure for
    ancestor/descendant comparison in :mod:`heads_up.conflicts`. The repository
    root itself canonicalizes to ``"."``.

    Resolution is purely lexical (no ``realpath``/``stat``/network): a
    ``\\\\host\\share`` resource is a string, never an SMB probe.

    A ``path`` claim is repo-scoped, so a resource that lexically escapes the
    repository root (leading ``..``, or an absolute path outside the repo on the
    SAME drive/anchor) is rejected as :class:`InvalidResourceError`. An absolute
    path on a DIFFERENT drive/anchor (no relative path exists) falls back to its
    absolute canonical form so cross-drive claims never raise.
    """
    stripped = _require_text(raw, InvalidResourceError, "resource")
    repo_stripped = _require_text(repository, InvalidRepositoryError, "repository")

    target_slashed = _strip_extended_prefix(stripped).replace("\\", "/")
    target_anchor, target_remainder = _split_anchor(target_slashed)

    if target_anchor == "":
        # Relative resource: already repo-relative. Reject if it climbs out.
        comps, escaped = _resolve_components(target_remainder, clamp=False)
        if escaped:
            raise InvalidResourceError(
                f"path resource {raw!r} escapes its repository root; a path claim "
                f"must stay inside its repository"
            )
        return _fold("/".join(comps) or ".")

    # Absolute resource.
    target_comps, _ = _resolve_components(target_remainder, clamp=True)
    repo_anchor, repo_comps = _split_normalize(repo_stripped)
    if _fold(target_anchor) != _fold(repo_anchor):
        # Cross-drive / cross-anchor: no relative path exists. Keep it absolute
        # and canonical; conflict scoping is by repository anyway.
        return _fold(_join_anchor(target_anchor, target_comps))
    rel = _lexical_relpath(repo_comps, target_comps)
    if rel and rel[0] == "..":
        raise InvalidResourceError(
            f"path resource {raw!r} escapes its repository root; a path claim "
            f"must stay inside its repository"
        )
    return _fold("/".join(rel) or ".")


# ---------------------------------------------------------------------------
# Non-path scalar kinds — trim to an exact canonical token, never path-normalize.
# ---------------------------------------------------------------------------


def canonicalize_owner(raw: str) -> str:
    """Canonical owner identity: surrounding whitespace trimmed (case-preserving)."""
    return _require_text(raw, InvalidOwnerError, "owner")


def canonicalize_session(raw: str) -> str:
    """Canonical session identity: surrounding whitespace trimmed (case-preserving)."""
    return _require_text(raw, InvalidSessionError, "session_id")


def canonicalize_issue(raw: str) -> str:
    """Canonical issue id: trimmed opaque token. NOT path-normalized."""
    return _require_text(raw, InvalidResourceError, "issue")


def canonicalize_plan_step(raw: str) -> str:
    """Canonical plan-step id: trimmed opaque token. NOT path-normalized."""
    return _require_text(raw, InvalidResourceError, "plan-step")


def canonicalize_named_resource(raw: str) -> str:
    """Canonical named-resource id: trimmed opaque token. NOT path-normalized."""
    return _require_text(raw, InvalidResourceError, "named-resource")


# ---------------------------------------------------------------------------
# Dispatch for the resource field by kind.
# ---------------------------------------------------------------------------


def canonicalize_resource(kind: ResourceKind | str, resource: str, *, repository: str) -> str:
    """Canonicalize a claim's ``resource`` field according to its ``resource_kind``.

    - ``path`` -> repo-root-relative POSIX form (ancestor/descendant aware).
    - ``repository`` -> the resource IS a repo root, canonicalized like one so it
      matches the claim's ``repository`` field.
    - ``plan-step`` / ``issue`` / ``named-resource`` -> trimmed opaque token.
    """
    parsed = ResourceKind.parse(kind)
    if parsed is ResourceKind.PATH:
        return canonicalize_path_resource(resource, repository=repository)
    if parsed is ResourceKind.REPOSITORY:
        return canonicalize_repository(resource)
    if parsed is ResourceKind.ISSUE:
        return canonicalize_issue(resource)
    if parsed is ResourceKind.PLAN_STEP:
        return canonicalize_plan_step(resource)
    return canonicalize_named_resource(resource)
