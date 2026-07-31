# heads-up integration contract

The minimal interface an orchestrator uses to consult heads-up before doing
parallel work. This is a **general CLI + JSON + exit-code contract**, not a
project binding: any build-orchestration or preflight tool is an *example*
caller only — do not edit a caller to add heads-up, and any substitute caller
speaking the same three formats works identically. heads-up takes **no**
dependency on any consumer and runs fully standalone.

**Consume heads-up through this contract only — the CLI stdout (JSON) and the
process exit code. Never import `heads_up` Python internals.** The Python modules
(`heads_up.ledger`, `heads_up.conflicts`, …) are private implementation and may
change shape between versions; the JSON keys documented here are the stable
surface (pinned by `schema_version`).

## 1. Invocation (plan section 10)

The canonical cross-repo invocation is:

```
uv run --project path/to/heads-up heads-up <verb> [options] --json
```

`uv run --project <path>` runs the installed `heads-up` console entry point from
any working directory without activating a venv. Add `--json` for machine output
(this document). Verbs: `claim`, `check`, `release`, `list`.

**Ledger location.** One machine-global file at
`%LOCALAPPDATA%\heads-up\ledger.jsonl` (created on first claim). Per-repository
scoping comes entirely from each claim's `repository` field, so every session and
worktree of a repository resolves the same file. Every verb accepts
`--ledger <path>` to override it (used by tests and for isolated ledgers).

**Identity is caller-supplied.** `claim` and `check` require
`--resource-kind {repository|path|plan-step|issue|named-resource}`, `--resource`,
`--repository`, `--owner`, and `--session-id`. `claim` additionally requires a
finite TTL: exactly one of `--ttl <30m|2h|1d|1h30m>` or
`--expires-at <ISO-8601 UTC>`. heads-up does not invent a global identity — the
caller passes stable `owner` / `session-id` strings.

## 2. Exit-code contract (plan section 10)

Exit codes are the primary signal — a caller can gate on them without parsing
JSON at all.

| Exit | Meaning | When |
|---|---|---|
| `0` | success, no conflict | clean `claim`, clean `check`, supersession renewal, successful or idempotent `release`, any `list` |
| `1` | advisory conflict reported | `claim` appended but contends with an existing active claim; `check` would conflict |
| `2` | usage or ledger error | invalid arguments, bad `--ttl`, unknown `claim_id`, owner mismatch on release, lock-retry exhaustion, corrupt ledger |

Exit `1` is **advisory**, not a failure: the claim was still recorded. The caller
decides whether to halt, continue, or inspect. These codes are deliberately
tool-local; sibling utilities define their own maps.

## 3. JSON output shapes (pinned by `schema_version`)

Every `--json` payload is deterministic (sorted keys, ASCII) and starts with
`"schema_version": 1` and `"verb"`. Pin against `schema_version`; it is bumped
only when a shape changes.

### `claim`

```json
{
  "schema_version": 1,
  "verb": "claim",
  "status": "clean",            // "clean" | "conflict" | "renewed"
  "exit_code": 0,               // mirrors the process exit code
  "claim": { "event": "claim", "claim_id": "…", "resource_kind": "path",
             "resource": "…", "repository": "…", "owner": "…",
             "session_id": "…", "created_at": "…", "expires_at": "…" },
  "conflicts": [],              // one entry per contended active claim
  "renewed": []                 // same-actor claims this one superseded (TTL renew)
}
```

On a conflict the `conflicts` array is non-empty and `exit_code` is `1`. **Each
conflict-evidence object carries BOTH claim IDs** plus the overlap reason:

```json
{
  "existing_claim_id": "2b749370-1705-4109-b229-22664abf74c3",  // the winner already on record
  "incoming_claim_id": "72fd2303-bc5f-45c3-8d6c-87659d5aec1e",  // this claimant
  "overlap": "ancestor",        // "exact" | "ancestor" | "descendant"
  "reason": "existing path 'src/app' is an ancestor of incoming path 'src/app/models.py'",
  "repository": "c:/repo",
  "resource_kind": "path",
  "existing_resource": "src/app",
  "incoming_resource": "src/app/models.py"
}
```

### `check`

Read-only probe — appends nothing. Same conflict-evidence objects under
`conflicts`; `would_conflict` (bool) and `claimed` (bool) summarize; `held_by_you`
lists active claims the same owner+session already holds. `exit_code` is `1` when
`would_conflict` is true, else `0`.

### `release`

`{ "verb": "release", "status": "applied" | "noop-inactive", "exit_code": 0,
"claim_id": "…", "owner": "…", "released_at": "…" }`. Releasing an
already-released/expired claim is an idempotent no-op (exit `0`); an unknown
`claim_id` or an owner that does not match the claim's owner is exit `2`.

### `list`

`{ "verb": "list", "exit_code": 0, "count": N, "active_claims": [ … ] }` — each
entry is a claim object without the `event` key. Always exit `0`.

## 4. Consuming a conflict without importing internals

The whole integration is: **run the CLI, read the exit code, optionally parse
stdout JSON.** Python example (no `heads_up` import):

```python
import json, subprocess

proc = subprocess.run(
    [
        "uv",
        "run",
        "--project",
        "path/to/heads-up",
        "heads-up",
        "claim",
        "--resource-kind",
        "path",
        "--resource",
        "src/app/models.py",
        "--repository",
        repo_root,
        "--owner",
        agent_id,
        "--session-id",
        session_id,
        "--ttl",
        "2h",
        "--json",
    ],
    capture_output=True,
    text=True,
)

if proc.returncode == 2:
    ...  # usage/ledger error — surface stderr, do not treat as a conflict
elif proc.returncode == 1:
    payload = json.loads(proc.stdout)
    for ev in payload["conflicts"]:
        # both IDs are always present on a conflict finding
        warn(
            f"{ev['reason']} — winner {ev['existing_claim_id']}, "
            f"yours {ev['incoming_claim_id']}, held by another session"
        )
    #  advisory: the caller decides to halt / continue / inspect
else:
    ...  # exit 0 — clean claim recorded
```

Any language works the same way: exec the command, branch on the exit code, and
(for exit `1`) read `stdout` as JSON and walk `conflicts[]` for
`existing_claim_id` + `incoming_claim_id` + `reason`. No shared library, no
in-process state, no `heads_up` import.

**Advisory model.** heads-up never silently resolves a race. When several
different-actor claims target one resource, **all of them stay active** (visible
in `list`) and each later one reports conflict evidence — exactly one claimant
(the first appended) gets the clean exit `0`. Coordination is the caller's
decision, informed by the evidence.

## 5. Concurrency guarantee (validated in Step 5)

The ledger append is serialized by an exclusive OS lock (`msvcrt.locking` on
Windows), making **append order a total order with no timestamp tie-break**
(plan section 6). Validated by `tests/test_concurrency_stress.py` with many real
OS processes racing a shared ledger, repeatedly:

- The ledger stays parseable after every round — no torn or interleaved lines.
- On each contested resource, **exactly one** claim wins clean (exit `0`) and it
  is deterministically the first-appended one; every later conflicting claimant
  is still appended (auditable), exits `1`, and carries both claim IDs.
- Concurrent claims on distinct resources all succeed with no false conflicts.
