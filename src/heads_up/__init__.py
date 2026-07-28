"""heads-up — advisory claims for parallel development work.

The authoritative event schema lives in :mod:`heads_up.models`. Later build
steps add identity/conflict rules (Step 2), the append-only ledger (Step 3),
and the CLI verbs (Step 4).
"""

from __future__ import annotations

from heads_up.models import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_SUCCESS,
    ActiveClaim,
    ClaimEvent,
    ClaimPairOutcome,
    ReleaseEvent,
    ReleaseOutcome,
    ResourceKind,
    SchemaError,
)

__version__ = "0.1.0"

__all__ = [
    "EXIT_CONFLICT",
    "EXIT_ERROR",
    "EXIT_SUCCESS",
    "ActiveClaim",
    "ClaimEvent",
    "ClaimPairOutcome",
    "ReleaseEvent",
    "ReleaseOutcome",
    "ResourceKind",
    "SchemaError",
    "__version__",
]
