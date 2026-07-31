# heads-up

A local-first utility for **advisory claims** over active development work. It makes parallel
intent visible *before* two sessions work on the same repository path, plan step, issue, or
generated state resource. Claims expire and never become hard locks; the append-only history stays
auditable.

## Stack

| Tool | Why |
|---|---|
| Python 3.12+ | Implementation language |
| uv | Environment + dependency management |
| argparse | CLI |
| pytest | Path-normalization, replay, subprocess, and multi-process stress tests |
| Ruff | Lint + format |
| mypy (strict) | Static typing |

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run mypy --strict src
```

## Usage

```bash
heads-up claim    ...   # register an advisory claim on a resource (requires a finite TTL)
heads-up check    ...   # check whether a resource is claimed
heads-up release  ...   # release a claim (idempotent)
heads-up list           # list active claims
```

Cross-repo form (no `cd` required):

```bash
uv run --project path/to/heads-up heads-up <verb> ...
```

## Design decisions

- **Advisory leases, not locks.** Claims warn and provide evidence; the caller decides whether to
  halt, continue, or inspect. Expired claims never block.
- **Append-only events.** Claim/release events retain history; active state is derived by replay, so
  an interrupted update cannot silently erase another claim.
- **Finite TTL by default.** Every claim expires unless explicitly released sooner; the CLI requires
  an expiry and displays both local and UTC time.
- **Append order is authoritative.** Lock-serialized append order decides races: the first-appended
  active claim on a resource wins a clean receipt (exit 0); every later conflicting claim is still
  appended (auditable) but reports conflict evidence carrying both claim IDs and exits 1. No
  timestamp tie-break.

## Ledger + concurrency

One machine-global ledger at `%LOCALAPPDATA%\heads-up\ledger.jsonl` (created on first claim); every
verb accepts a `--ledger <path>` override. Per-repository scoping comes from the schema's
`repository` field, so all sessions and worktrees of a repository resolve the same file. Writers
serialize via stdlib `msvcrt.locking` on a sidecar `ledger.jsonl.lock` (bounded ~5 s retry, then
exit 2) — no third-party locking dependency.

## Exit codes

`0` success with no conflict (clean claim/check, successful or idempotent release); `1` advisory
conflict reported; `2` usage or ledger error (invalid arguments, lock-retry exhaustion, unknown
claim_id).

## Project structure

```
src/heads_up/
  models.py      claim/release event + active-claim shapes (claim_id = UUIDv4)
  identity.py    repository/path/resource/owner/session normalization
  ledger.py      append, replay, integrity checks, concurrency handling
  conflicts.py   exact + ancestor/descendant overlap rules
  cli.py         claim / check / release / list
docs/integration-contract.md   optional caller contract (CLI + JSON + exit codes only)
```

See [plans/plan.md](plans/plan.md) for the frozen event schema and lifecycle semantics.
