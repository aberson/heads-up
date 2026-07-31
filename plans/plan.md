# Seed Plan: heads-up

<!-- decisions-applied: 2026-07-26 -->

## 1. What This Feature Does

Heads Up is a local-first utility project for advisory claims over active development work. It makes
parallel intent visible before two sessions work on the same repository path, plan step, issue, or
generated state resource. Claims expire and never become hard locks, while the append-only history
remains auditable.

## 2. Existing Context

- Parallel development sessions can collide on the same work — wasting time and tokens, and causing
  cross-session commit contamination.
- Worktrees isolate files but do not communicate intent. Heads Up owns coordination state and works
  fully standalone with zero participating callers; any preflight or orchestration tool may later
  consume conflict findings through the CLI/JSON contract only — never by importing Heads Up
  internals — and Heads Up takes no dependency on any consumer.
- V1 is single-machine and one-shot. It has no daemon, distributed lock service, or session control.

## 3. Scope

**In:** Python 3.12+ and uv; append-only JSONL events; claim/check/release/list; finite TTL; repository,
path, plan-step, issue, and named-resource claims; Windows canonicalization; concurrent-write safety.

**Out:** hard locks, distributed coordination, chat/inbox, process termination, mutating another
worktree, automatic merges, and inferred ownership without an explicit claim.

## 4. Impact Analysis

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `plans/plan.md` | add | Canonical project plan | New project |

No existing session-state schema is modified in v1.

## 5. New Components

- `src/heads_up/models.py`: claim/release event and active-claim shapes.
- `src/heads_up/identity.py`: repository, path, resource, owner, and session normalization.
- `src/heads_up/ledger.py`: append, replay, integrity checks, and concurrency handling.
- `src/heads_up/conflicts.py`: exact and ancestor/descendant overlap rules.
- `src/heads_up/cli.py`: `claim`, `check`, `release`, and `list`.
- `docs/integration-contract.md`: optional caller contract — a general interface contract (CLI
  invocation, JSON output, exit codes), not a project binding. Build-orchestration or preflight
  tools are example callers only; any substitute caller speaking the same contract works
  identically, and Heads Up runs fully without any of them.

## 6. Design Decisions

**Advisory leases, not locks.** Claims warn and provide evidence. The caller decides whether to halt,
continue, or inspect; expired claims never block.

**Append-only events.** Claim and release events retain history. Active state is derived by replay,
so an interrupted update cannot silently erase another claim.

**Finite TTL by default.** Every claim expires unless explicitly released sooner. The CLI requires
an expiry and displays both local and UTC time.

**Explicit caller identity.** Owners and session IDs are supplied by callers; Heads Up does not
invent a global agent identity system.

**Append order is authoritative.** Lock-serialized append order decides races: the first-appended
active claim on a resource wins a clean receipt (exit 0); every later conflicting claim is still
appended (auditable) but reports conflict evidence carrying both claim IDs and exits 1. There is no
timestamp tie-break — the write lock makes the order total.

## 7. Build Steps

<!-- autofix-applied: 2026-07-25 -->
### Step 1: Scaffold and freeze the event schema
- **Problem:** Create the uv package, claim/release models, stable IDs, TTL rules, and JSON/text contracts.
- **Type:** code
- **Issue:** #1
- **Flags:** --reviewers deep --isolation worktree
- **Produces:** scaffold, `models.py`, schema reference, validation tests
- **Done when:** malformed resources, owners, timestamps, and non-finite TTLs fail explicitly; quality gates pass
- **Depends on:** none
- **Status:** DONE (2026-07-27)
- **Schema summary** (this step freezes the exact types):

  `ClaimEvent` shape:
  | field | type | note |
  |---|---|---|
  | claim_id | string | UUIDv4 — `str(uuid.uuid4())`, lowercase hyphenated, generated in `models.py` at claim time (e.g. `2f1e9c4a-7b3d-4e8f-9a6c-1d2e3f4a5b6c`) |
  | resource_kind | enum: repository \| path \| plan-step \| issue \| named-resource | what is claimed (plan section 3 In-scope list) |
  | resource | string | canonical resource identifier (normalized per Step 2) |
  | repository | string | canonicalized repository root |
  | owner | string | caller-supplied owner identity (plan section 6) |
  | session_id | string | caller-supplied session identity (plan section 6) |
  | created_at | ISO-8601 UTC timestamp | claim creation time |
  | expires_at | ISO-8601 UTC timestamp | required finite expiry (plan section 6: every claim expires; CLI requires it) |

  `ReleaseEvent` shape: `claim_id` + `released_at` (ISO-8601 UTC) + `owner`; must reference an existing claim.
  `ActiveClaim` is derived, never stored: replay of claim events minus released, expired, and superseded ones (plan section 6, append-only events).

  Lifecycle semantics frozen together with the schema:
  - Re-claim with matching owner AND session_id on the same resource is TTL renewal by supersession: a fresh `ClaimEvent` (new claim_id, new expires_at) is appended, and replay marks the earlier claim superseded-inactive — never a self-conflict (exit 0). Same owner with a different session_id is a normal conflict.
  - Release of an already-released or expired claim is an idempotent no-op (exit 0 plus an informational note); release of an unknown claim_id is an error.

<!-- autofix-applied: 2026-07-25 -->
### Step 2: Build canonical identity and overlap rules
- **Problem:** Normalize repositories and Windows paths and implement exact-resource plus ancestor/descendant conflict detection.
- **Type:** code
- **Issue:** #2
- **Flags:** --reviewers deep --isolation worktree
- **Produces:** `identity.py`, `conflicts.py`
- **Done when:** nested paths conflict both ways, aliases normalize consistently, and different repositories do not conflict
- **Depends on:** 1
- **Status:** DONE (2026-07-27)

<!-- autofix-applied: 2026-07-25 -->
### Step 3: Implement the append-only ledger
- **Problem:** Add atomic append, replay, release, expiry, corruption diagnostics, and stable active-state derivation. Writers serialize via stdlib `msvcrt.locking` (exclusive) on a sidecar `ledger.jsonl.lock`: bounded retry of roughly 5 seconds total, then exit 2 (usage/ledger error); append and flush while the lock is held, then release. No portalocker or other runtime dependency.
- **Type:** code
- **Issue:** #3
- **Flags:** --reviewers deep --isolation worktree
- **Produces:** `ledger.py`, storage tests
- **Done when:** released and expired claims remain auditable but inactive; malformed lines are diagnosed; concurrent writers serialize through the sidecar `msvcrt.locking` lock and do not corrupt records; all sessions and worktrees of a repository resolve the same ledger file
- **Depends on:** 1
- **Status:** DONE (2026-07-27)

<!-- autofix-applied: 2026-07-28 -->
### Step 4: Expose the lifecycle CLI
- **Problem:** Implement `claim/check/release/list` with conflict evidence, JSON output, and meaningful exit codes.
- **Type:** code
- **Issue:** #4
- **Flags:** --reviewers deep --isolation worktree
- **Produces:** `cli.py`, installed command, end-to-end CLI tests
- **Done when:** a second conflicting claim receives both claim IDs and source evidence; clean claims and explicit releases behave deterministically
- **Depends on:** 2, 3
- **Status:** DONE (2026-07-27)

### Step 5: Validate parallel behavior and publish integration contract
- **Problem:** Run simultaneous claim attempts and document the minimal invocation contract for existing orchestrators without editing those projects. `docs/integration-contract.md` cites section 10's canonical cross-repo invocation and specifies the contract as CLI + JSON + exit codes only, so any substitute caller speaking the same formats works identically.
- **Type:** code
- **Issue:** #5
- **Flags:** --reviewers code --isolation worktree
- **Produces:** concurrency stress tests, `docs/integration-contract.md`, findings
- **Done when:** repeated concurrent runs preserve a parseable ledger; on each contested resource the first-appended claim reports clean (exit 0) while every later conflicting claim is still appended, carries both claim IDs in its conflict evidence, and exits 1; and callers can consume JSON without importing Python internals
- **Depends on:** 4
- **Status:** DONE (2026-07-27)

## 8. Risks and Open Questions

| Item | Risk | Mitigation |
|---|---|---|
| Non-participating callers | Collision remains invisible | Integrate at high-risk entry points after v1 |
| Stale claims | Noise and ignored warnings | Required finite TTL and release command |
| Windows append races | Ledger corruption | Exclusive stdlib `msvcrt.locking` on sidecar `ledger.jsonl.lock` plus stress tests |
| Path aliases | Overlap evasion | Resolve, case-normalize, and separator-normalize |

## 9. Testing Strategy

Use path-normalization tables, temporary repositories, replay/corruption fixtures, CLI subprocess
tests, and multi-process append stress tests on Windows. The final step exercises real concurrent
one-shot commands; there is no autonomous background service.

## Appendix: Decision Inventory

| ID | P/D | Choice | Status |
|---|---|---|---|
| P4 | P | Build Heads Up because parallel-work collisions are a demonstrated problem | accepted |
| D1 | D | Use Python 3.12+, uv, argparse, pytest, Ruff, and mypy strict | accepted |
| D3 | D | Initialize a separate nested GitHub repository before build | accepted |
| D6 | D | Use advisory finite-TTL claims rather than hard locks | accepted |

## 10. Build and Run Contract

Bootstrap with Python 3.12+ and `uv sync --extra dev`. Quality gates are `uv run pytest -q`,
`uv run ruff check .`, and `uv run mypy --strict src`. The installed CLI entry point is
`heads-up`.

The ledger is ONE machine-global file at `%LOCALAPPDATA%\heads-up\ledger.jsonl` (directory created
on first claim); every verb accepts a `--ledger <path>` override. Per-repository conflict scoping
comes from the schema's `repository` field — there is no per-repo file-key derivation, so all
sessions and worktrees of a repository resolve the same file unconditionally.

The canonical cross-repo invocation is `uv run --project path/to/heads-up heads-up
<verb> ...`; `uv tool install` and packaging stay out of scope until a distribution need exists.

Exit codes (deliberately tool-local; sibling utilities define their own maps): 0 = success with no
conflict (clean claim, clean check, successful or idempotent release); 1 = advisory conflict
reported; 2 = usage or ledger error (invalid arguments, lock-retry exhaustion, unknown claim_id).
