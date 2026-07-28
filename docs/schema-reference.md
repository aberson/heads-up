# heads-up schema reference

Authoritative, frozen in build Step 1. The types live in
[`src/heads_up/models.py`](../src/heads_up/models.py); later steps consume them
and never redefine them. Runtime code is standard-library only.

## Events (append-only wire format)

The ledger is JSONL — one JSON object per line, compact
(`json.dumps(..., separators=(",", ":"))`), keys in the fixed order below. Each
line carries an `event` discriminator (`claim` or `release`) so replay can
dispatch without positional guessing.

### `ClaimEvent` — `event: "claim"`

| field | type | rule |
|---|---|---|
| `event` | `"claim"` | discriminator (first key) |
| `claim_id` | string | UUIDv4 — `str(uuid.uuid4())`, lowercase-hyphenated, minted in `models.py` at claim time |
| `resource_kind` | enum | one of `repository`, `path`, `plan-step`, `issue`, `named-resource` |
| `resource` | string | canonical resource id (non-empty; normalization is Step 2) |
| `repository` | string | canonicalized repository root (non-empty) |
| `owner` | string | caller-supplied owner identity (non-empty) |
| `session_id` | string | caller-supplied session identity (non-empty) |
| `created_at` | ISO-8601 UTC | claim creation time |
| `expires_at` | ISO-8601 UTC | **required, finite, strictly after `created_at`** |

### `ReleaseEvent` — `event: "release"`

| field | type | rule |
|---|---|---|
| `event` | `"release"` | discriminator (first key) |
| `claim_id` | string | UUIDv4; must reference an existing claim (referential check is Step 3) |
| `released_at` | ISO-8601 UTC | release time |
| `owner` | string | releasing owner (non-empty) |

### `ActiveClaim` — DERIVED, never stored

Same fields as `ClaimEvent` minus `event`. Computed by Step 3's ledger by
replaying claim events and dropping any that are **released, expired, or
superseded**. Step 1 only freezes the type + `from_claim` / `is_expired`.

## Validation — every malformed input raises a specific error

All errors subclass `SchemaError` (which subclasses `ValueError`).

| condition | error |
|---|---|
| empty / whitespace `resource` | `InvalidResourceError` |
| empty / whitespace `repository` | `InvalidRepositoryError` |
| empty / whitespace `owner` | `InvalidOwnerError` |
| empty / whitespace `session_id` | `InvalidSessionError` |
| non-ISO-8601, naive, or non-UTC timestamp | `InvalidTimestampError` |
| `expires_at` missing or `<= created_at` (non-finite/non-positive TTL) | `NonFiniteTTLError` |
| `claim_id` not a canonical lowercase v4 UUID | `InvalidClaimIdError` |
| `resource_kind` outside the enum | `InvalidResourceKindError` |
| missing field, wrong type, or wrong `event` on deserialize | `MalformedEventError` |

Timestamps are normalized to canonical UTC (`...+00:00`); `...Z` and `...+00:00`
collapse to the same string, so round-trips are byte-deterministic.

## Lifecycle semantics (predicates frozen here; the ledger applies them in Step 3)

- **Re-claim, same owner AND session_id, same resource** → TTL **renewal by
  supersession**: a fresh `ClaimEvent` (new `claim_id`, new `expires_at`) is
  appended and the earlier claim is marked superseded-inactive on replay. Not a
  self-conflict → exit `0`. (`classify_claim_pair` → `SUPERSEDES`.)
- **Same owner, different session_id** (or any different owner) on the same
  resource → normal **conflict** → exit `1`. (`classify_claim_pair` →
  `CONFLICTS`.)
- **Different resource** → `UNRELATED` (no interaction).
- **Release of an already-released or expired claim** → idempotent **no-op**,
  exit `0` + informational note. (`classify_release` → `NOOP_INACTIVE`.)
- **Release of an unknown `claim_id`** → **error**, exit `2`.
  (`classify_release` → `UNKNOWN_CLAIM`.)

`same_resource` here is **exact match** on `(repository, resource_kind,
resource)`. Ancestor/descendant path overlap is Step 2 (`conflicts.py`).

## Exit codes (plan section 10)

| code | constant | meaning |
|---|---|---|
| `0` | `EXIT_SUCCESS` | clean claim, clean check, successful or idempotent release |
| `1` | `EXIT_CONFLICT` | advisory conflict reported |
| `2` | `EXIT_ERROR` | usage or ledger error (bad args, lock exhaustion, unknown claim_id) |

`exit_code_for_claim_pair` and `exit_code_for_release` map the lifecycle
outcomes above to these codes; `exit_code_description(code)` returns the text.

## Frozen golden fixtures

- [`tests/fixtures/golden_claim_event.jsonl`](../tests/fixtures/golden_claim_event.jsonl)
- [`tests/fixtures/golden_release_event.jsonl`](../tests/fixtures/golden_release_event.jsonl)

`test_models.py` asserts each fixture parses and re-serializes **byte-identically**,
locking the wire format against silent drift.
