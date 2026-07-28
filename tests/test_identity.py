"""Canonicalization tables for heads_up.identity (build Step 2).

Two aliases of the same repository/path MUST canonicalize equal; two distinct
ones must NOT. Case-alias assertions are gated to Windows (``os.name == "nt"``),
because POSIX filesystems are case-sensitive and folding there would be wrong.
"""

from __future__ import annotations

import os

import pytest

from heads_up.identity import (
    _strip_extended_prefix,
    canonicalize_issue,
    canonicalize_named_resource,
    canonicalize_owner,
    canonicalize_path_resource,
    canonicalize_plan_step,
    canonicalize_repository,
    canonicalize_resource,
    canonicalize_session,
)
from heads_up.models import (
    InvalidOwnerError,
    InvalidRepositoryError,
    InvalidResourceError,
    InvalidSessionError,
    ResourceKind,
)

WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="case-fold identity is Windows-only")

REPO = "c:/repo"


# ---------------------------------------------------------------------------
# Repository canonicalization — aliases of one real dir collapse; distinct don't.
# ---------------------------------------------------------------------------


def test_repository_aliases_separator_and_trailing_slash_equal(tmp_path: object) -> None:
    base = str(tmp_path)
    forward = canonicalize_repository(base.replace("\\", "/"))
    back = canonicalize_repository(base.replace("/", "\\"))
    trailing = canonicalize_repository(base + os.sep)
    dotted = canonicalize_repository(os.path.join(base, "sub", ".."))
    assert forward == back == trailing == dotted


@windows_only
def test_repository_case_and_drive_letter_aliases_equal(tmp_path: object) -> None:
    base = str(tmp_path)
    lower = canonicalize_repository(base)
    upper = canonicalize_repository(base.upper())
    assert lower == upper
    # Drive letter is folded, and the form is forward-slash lowercase.
    assert lower == lower.casefold()
    assert "\\" not in lower


def test_two_distinct_repositories_do_not_collide(tmp_path: object) -> None:
    a = tmp_path / "repo-a"  # type: ignore[operator]
    b = tmp_path / "repo-b"  # type: ignore[operator]
    a.mkdir()
    b.mkdir()
    assert canonicalize_repository(str(a)) != canonicalize_repository(str(b))


def test_extended_length_prefix_stripped() -> None:
    assert _strip_extended_prefix("\\\\?\\C:\\x\\y") == "C:\\x\\y"
    assert _strip_extended_prefix("\\\\?\\UNC\\server\\share") == "\\\\server\\share"
    assert _strip_extended_prefix("C:\\already\\normal") == "C:\\already\\normal"


def test_empty_repository_rejected() -> None:
    with pytest.raises(InvalidRepositoryError):
        canonicalize_repository("   ")


# ---------------------------------------------------------------------------
# Path resource canonicalization TABLE — alias pairs that must be EQUAL.
# All relative to REPO; realpath resolves . / .. lexically for these synthetic
# (non-existent) paths, and the forward-slash + fold pass handles the rest.
# ---------------------------------------------------------------------------

PATH_ALIAS_PAIRS: list[tuple[str, str]] = [
    ("src/foo.py", "./src/foo.py"),  # leading ./
    ("src/foo.py", "src\\foo.py"),  # backslash separator
    ("src/foo.py", "src/../src/foo.py"),  # .. round-trip
    ("src/foo.py", "src//foo.py"),  # doubled separator
    ("src", "src/"),  # trailing slash
    ("a/b/c", "a\\b\\c"),  # nested backslashes
]


@pytest.mark.parametrize(("left", "right"), PATH_ALIAS_PAIRS)
def test_path_aliases_normalize_equal(left: str, right: str) -> None:
    assert canonicalize_path_resource(left, repository=REPO) == canonicalize_path_resource(
        right, repository=REPO
    )


PATH_CASE_ALIAS_PAIRS: list[tuple[str, str]] = [
    ("Src/Foo.py", "src/foo.py"),  # component case
    ("SRC\\FOO.PY", "src/foo.py"),  # all caps + backslash
]


@windows_only
@pytest.mark.parametrize(("left", "right"), PATH_CASE_ALIAS_PAIRS)
def test_path_case_aliases_normalize_equal_on_windows(left: str, right: str) -> None:
    assert canonicalize_path_resource(left, repository=REPO) == canonicalize_path_resource(
        right, repository=REPO
    )


def test_absolute_path_inside_repo_matches_relative(tmp_path: object) -> None:
    repo = str(tmp_path)
    absolute = os.path.join(repo, "src", "foo.py")
    assert canonicalize_path_resource(absolute, repository=repo) == canonicalize_path_resource(
        "src/foo.py", repository=repo
    )


# ---------------------------------------------------------------------------
# Path resource canonicalization TABLE — distinct paths that must NOT be equal.
# ---------------------------------------------------------------------------

PATH_DISTINCT_PAIRS: list[tuple[str, str]] = [
    ("src/foo.py", "src/foobar.py"),  # component-boundary, not string prefix
    ("src/foo.py", "src/bar.py"),  # sibling files
    ("src", "srcxtra"),  # prefix-string but different component
    ("a/b", "a/c"),  # divergent leaf
]


@pytest.mark.parametrize(("left", "right"), PATH_DISTINCT_PAIRS)
def test_distinct_paths_do_not_normalize_equal(left: str, right: str) -> None:
    assert canonicalize_path_resource(left, repository=REPO) != canonicalize_path_resource(
        right, repository=REPO
    )


def test_path_form_is_forward_slash_and_relative() -> None:
    canon = canonicalize_path_resource("Src\\Foo\\bar.py", repository=REPO)
    assert "\\" not in canon
    assert not os.path.isabs(canon)
    if WINDOWS:
        assert canon == "src/foo/bar.py"


def test_repo_root_path_resource_is_dot() -> None:
    assert canonicalize_path_resource(".", repository=REPO) == "."


def test_empty_path_resource_rejected() -> None:
    with pytest.raises(InvalidResourceError):
        canonicalize_path_resource("   ", repository=REPO)


def test_path_resource_requires_repository() -> None:
    with pytest.raises(InvalidRepositoryError):
        canonicalize_path_resource("src/foo.py", repository="")


# ---------------------------------------------------------------------------
# Non-path scalar kinds — trim to an exact token, never path-normalize.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canon",
    [canonicalize_issue, canonicalize_plan_step, canonicalize_named_resource],
)
def test_scalar_kinds_trim_only(canon: object) -> None:
    fn = canon  # type: ignore[assignment]
    assert fn("  #42  ") == "#42"  # type: ignore[operator]
    # NOT path-normalized: separators and case survive untouched.
    assert fn("Feature/Login") == "Feature/Login"  # type: ignore[operator]
    assert fn("a/../b") == "a/../b"  # type: ignore[operator]


def test_issue_is_not_path_normalized() -> None:
    # A path-shaped issue id keeps its literal form (no . / .. collapse, no fold).
    assert canonicalize_issue("ORG/Repo#7") == "ORG/Repo#7"


def test_owner_and_session_trim_and_preserve_case() -> None:
    assert canonicalize_owner("  Abraham  ") == "Abraham"
    assert canonicalize_session("  Sess-A  ") == "Sess-A"


@pytest.mark.parametrize(
    ("fn", "error"),
    [
        (canonicalize_owner, InvalidOwnerError),
        (canonicalize_session, InvalidSessionError),
        (canonicalize_issue, InvalidResourceError),
        (canonicalize_plan_step, InvalidResourceError),
        (canonicalize_named_resource, InvalidResourceError),
    ],
)
def test_empty_scalar_rejected(fn: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        fn("   ")  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Dispatch by kind.
# ---------------------------------------------------------------------------


def test_canonicalize_resource_dispatches_path() -> None:
    got = canonicalize_resource(ResourceKind.PATH, "Src\\Foo.py", repository=REPO)
    other = canonicalize_path_resource("Src\\Foo.py", repository=REPO)
    assert got == other


def test_canonicalize_resource_dispatches_repository(tmp_path: object) -> None:
    repo = str(tmp_path)
    got = canonicalize_resource(ResourceKind.REPOSITORY, repo, repository=repo)
    assert got == canonicalize_repository(repo)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (ResourceKind.ISSUE, "#42"),
        (ResourceKind.PLAN_STEP, "Step 2"),
        (ResourceKind.NAMED_RESOURCE, "gpu-lease"),
    ],
)
def test_canonicalize_resource_scalar_kinds_trim_only(kind: ResourceKind, value: str) -> None:
    assert canonicalize_resource(kind, f"  {value}  ", repository=REPO) == value


def test_canonicalize_resource_accepts_string_kind() -> None:
    assert canonicalize_resource("issue", "  #9  ", repository=REPO) == "#9"


# ---------------------------------------------------------------------------
# Disk-free + deterministic (no realpath/expanduser/stat/network).
# ---------------------------------------------------------------------------


def test_canonicalization_is_disk_free_and_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canonicalization must not consult the filesystem or the network.

    Patches the two on-disk resolvers the old implementation used to blow up on
    call, then proves both repository and path canonicalization still work on a
    NON-EXISTENT tree and that the same logical path is deterministic.
    """

    def boom(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("canonicalization touched the filesystem")

    monkeypatch.setattr(os.path, "realpath", boom)
    monkeypatch.setattr(os.path, "expanduser", boom)

    # A path that does not exist on disk canonicalizes identically to its
    # already-collapsed form -> identity depends on the string, not the tree.
    with_dotdot = canonicalize_repository("c:/Nonexistent/Repo/../Repo")
    plain = canonicalize_repository("c:/Nonexistent/Repo")
    assert with_dotdot == plain
    # Determinism across repeated calls (no on-disk state consulted).
    assert canonicalize_repository("c:/Nonexistent/Repo") == plain


def test_unc_resource_is_pure_string_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``\\\\host\\share`` (UNC) resource is a string, never an SMB probe.

    A dead share would hang realpath; here canonicalization returns immediately
    with a pure-string result and never calls the on-disk resolvers.
    """

    def boom(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("UNC canonicalization touched the network/filesystem")

    monkeypatch.setattr(os.path, "realpath", boom)
    monkeypatch.setattr(os.path, "expanduser", boom)

    canon = canonicalize_path_resource("\\\\deadhost\\share\\x", repository="\\\\deadhost\\share")
    assert canon == "x"


# ---------------------------------------------------------------------------
# str.lower (not casefold): the German sharp-s must NOT expand to "ss".
# ---------------------------------------------------------------------------


@windows_only
def test_sharp_s_not_overfolded_to_ss() -> None:
    # "Strasse.py" and the sharp-s spelling are DISTINCT files; casefold would
    # collapse them (both -> "strasse.py"). str.lower keeps them apart.
    sharp = canonicalize_path_resource("Stra\u00dfe.py", repository=REPO)
    double_s = canonicalize_path_resource("Strasse.py", repository=REPO)
    assert sharp != double_s


# ---------------------------------------------------------------------------
# Repo-escaping path resources are rejected (a path claim stays in its repo).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "escaping",
    [
        "../outside",  # climbs above the repo root
        "src/../../evil",  # net-escapes after a cancel
        "../../../etc/passwd",  # deep escape
    ],
)
def test_relative_escaping_path_rejected(escaping: str) -> None:
    with pytest.raises(InvalidResourceError):
        canonicalize_path_resource(escaping, repository=REPO)


def test_absolute_sibling_path_rejected() -> None:
    # Absolute, same drive, but OUTSIDE the repo -> would need "../" -> rejected.
    with pytest.raises(InvalidResourceError):
        canonicalize_path_resource("c:/other/foo.py", repository="c:/repo")


def test_in_repo_dotdot_roundtrip_still_allowed() -> None:
    # ".." that nets out INSIDE the repo is fine and equals the direct form.
    assert canonicalize_path_resource(
        "src/../src/foo.py", repository=REPO
    ) == canonicalize_path_resource("src/foo.py", repository=REPO)


def test_cross_drive_path_falls_back_to_absolute() -> None:
    # Mismatched drive letters: no relative path exists -> absolute canonical
    # form (the previously-untested cross-anchor fallback branch). Lowercase
    # input so the assertion holds on both Windows (folds) and POSIX (identity).
    canon = canonicalize_path_resource("d:/data/x.py", repository="c:/repo")
    assert canon == "d:/data/x.py"


# ---------------------------------------------------------------------------
# NUL / control chars -> clean schema error at the identity boundary.
# ---------------------------------------------------------------------------


def test_nul_byte_in_path_raises_clean_schema_error() -> None:
    with pytest.raises(InvalidResourceError):
        canonicalize_path_resource("src/\x00/foo.py", repository=REPO)


def test_nul_byte_in_repository_raises_clean_schema_error() -> None:
    with pytest.raises(InvalidRepositoryError):
        canonicalize_repository("c:/re\x00po")


def test_newline_in_owner_rejected() -> None:
    # Control-char rejection holds for scalar kinds too (would corrupt JSONL).
    with pytest.raises(InvalidOwnerError):
        canonicalize_owner("ali\nce")
