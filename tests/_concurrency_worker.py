"""Barrier-synchronized claim worker for the Step-5 concurrency stress tests.

Run as a standalone script (``python _concurrency_worker.py <start_epoch> <cli
args...>``), NEVER imported by the suite — so pytest does not collect it (no
``test_`` prefix) and it stays out of the mypy ``src`` gate.

Each worker is a real OS process. It boots the interpreter, imports the
production CLI, then BUSY-WAITS on a shared wall-clock instant (``start_epoch``,
a ``time.time()`` float passed identically to every sibling) before invoking
:func:`heads_up.cli.main`. The barrier collapses interpreter-startup skew so all
siblings arrive at the ledger's ``msvcrt.locking`` write lock at essentially the
same moment — genuine OS-level contention, not a thread race. Correctness never
depends on the barrier (the exclusive lock serializes read-decide-append into a
total order regardless); the barrier only maximizes how simultaneous the race is.

The remaining argv is forwarded verbatim to ``main`` (which prints the ``--json``
payload to stdout and returns the process exit code), so the parent test reads a
pure-JSON stdout plus the real 0/1/2 return code.
"""

from __future__ import annotations

import sys
import time

from heads_up.cli import main


def run(argv: list[str]) -> int:
    """Wait for the shared barrier, then run the CLI verb; return its exit code."""
    start = float(argv[0])
    cli_args = argv[1:]
    # Coarse sleep to just before the barrier (Windows sleep resolution ~15 ms),
    # then a tight spin for the final window so every sibling fires together.
    remaining = start - time.time()
    if remaining > 0.02:
        time.sleep(remaining - 0.02)
    while time.time() < start:
        pass
    return main(cli_args)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
