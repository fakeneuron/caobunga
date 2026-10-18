"""`caobunga fleet`: every enabled project's `run`, side by side (CBN-312).

A bounded pool of child `caobunga run <slug>` processes. Each child is an
ordinary `run` — its own per-repo lock, tier, drain, and exit code — so this
module dispatches nothing itself and never reads a PLAN. It derives no order
beyond the registry's, no failure policy (one child's exit never touches
another), and no resume state: a re-run spawns again and `run`'s own exit 3
skips a repo with nothing eligible. The one file it opens is the invocation
receipt an exit-0 child names, because exit 0 alone cannot tell a park or a red
suite from a finished scope (CBN-372).
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from caobunga.store import Store

DEFAULT_JOBS = 3
# The line `run` echoes after every non-`observe` invocation (`loop.execute_invocation`).
RECEIPT_PREFIX = "invocation-record: "

# `run`'s exit codes (CONTRACT §"Exit codes"). 2 and 3 are skips for the fleet:
# a refusal (dirty tree, held lock, disabled) or nothing eligible is the repo's
# state, not a fleet failure. 5 is not: no installed or pickable provider, or an
# unreadable `git`, is this machine's state and fails the fleet (CBN-EPIC-313.5).
# Any other code — 1 included — reads as "unexpected". A 0 whose receipt says
# `successful: false` (a park, a red suite) is not a skip either (CBN-372).
_MEANINGS = {
    0: "ok",
    2: "refused",
    3: "nothing eligible",
    4: "failed",
    5: "unlaunchable",
}
SKIP_CODES = frozenset({0, 2, 3})

SpawnFn = Callable[[str, Callable[[str], None]], int]


@dataclass(frozen=True)
class FleetLeg:
    slug: str
    exit_code: int
    seconds: float
    # Set only on an exit-0 leg whose receipt says it stopped short (CBN-372).
    stopped_short: str | None = None

    @property
    def meaning(self) -> str:
        return self.stopped_short or _MEANINGS.get(self.exit_code, "unexpected")

    @property
    def passes_fold(self) -> bool:
        """`ok` or a skip: nothing in this leg fails the fleet."""
        return self.exit_code in SKIP_CODES and self.stopped_short is None


def _receipt_meaning(receipt_path: str) -> str | None:
    """`<status> (<stop_reason>)` when the named receipt is unsuccessful, else None.

    The receipt is loaded by its id from this home's store, never from the path
    as printed. A receipt that cannot be loaded, for any reason, is not taken as
    success, and the leg keeps its row.
    """
    try:
        result = Store().load_invocation(Path(receipt_path).stem)
    except Exception:  # noqa: BLE001 — every slug gets a receipt row
        return "receipt unreadable"
    if result.successful:
        return None
    return f"{result.status.value} ({result.stop_reason})"


def spawn_run(slug: str, on_line: Callable[[str], None]) -> int:
    """Run `caobunga run <slug>` as a child, forwarding each output line.

    stdin is closed: N children must never contend for one terminal. `-u`
    unbuffers this child only (an env var would leak into its workers and
    suites); `-P` keeps the cwd off `sys.path`, so the child imports the same
    install the parent runs from. A failing `on_line` (a closed pipe) stops the
    forwarding, never the draining — a child blocked on a full pipe would hold
    its run lock forever. If reading the child's output raises, the child is
    killed and reaped before the error propagates.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-P", "-u", "-m", "caobunga", "run", slug],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except OSError as exc:
        on_line(f"fleet: could not start run: {exc}")
        return 1
    forwarding = True
    try:
        if proc.stdout is not None:  # always set: stdout=PIPE
            for line in proc.stdout:
                if not forwarding:
                    continue
                try:
                    on_line(line.rstrip("\n"))
                except Exception:  # noqa: BLE001 — keep draining whatever the sink did
                    forwarding = False
        return proc.wait()
    finally:
        # Reached with the child still running only when the read raised:
        # nobody is draining the pipe, so kill and reap it, or it blocks on a
        # full pipe holding its run lock forever.
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        finally:
            if proc.stdout is not None:
                proc.stdout.close()


def run_fleet(
    slugs: Sequence[str],
    jobs: int,
    *,
    echo: Callable[[str], None],
    spawn: SpawnFn = spawn_run,
    clock: Callable[[], float] = time.monotonic,
) -> list[FleetLeg]:
    """Run each slug's child with at most `jobs` alive; legs in `slugs` order.

    `echo` is called from worker threads, one whole line per call; the caller
    makes it thread-safe. Every slug gets a leg: a leg that raises is an
    `unexpected` (1) row, not a lost receipt. A child that exits 0 is checked
    against the last receipt it named (CBN-372). On Ctrl-C the queued slugs are
    cancelled — never spawned — and the running children, which got the same
    SIGINT, are waited for before the interrupt propagates.
    """

    def leg(slug: str) -> FleetLeg:
        started = clock()
        receipt: str | None = None
        echoing = True

        def on_line(line: str) -> None:
            # Never raises: `spawn_run` stops calling a sink that raised, and the
            # receipt line comes last, so a broken echo must not hide it.
            nonlocal receipt, echoing
            if line.startswith(RECEIPT_PREFIX):
                receipt = line.removeprefix(RECEIPT_PREFIX)
            if echoing:
                try:
                    echo(f"[{slug}] {line}")
                except Exception:  # noqa: BLE001 — keep reading for the receipt line
                    echoing = False

        try:
            code = spawn(slug, on_line)
        except Exception as exc:  # noqa: BLE001 — every slug gets a receipt row
            with suppress(Exception):
                echo(f"[{slug}] fleet: {exc!r}")
            code = 1
        short = _receipt_meaning(receipt) if code == 0 and receipt is not None else None
        return FleetLeg(slug=slug, exit_code=code, seconds=clock() - started, stopped_short=short)

    pool = ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = [pool.submit(leg, slug) for slug in slugs]
        return [future.result() for future in futures]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def fleet_exit(legs: Sequence[FleetLeg]) -> int:
    """4 when any leg failed, stopped short, or exited outside `run`'s skip codes, else 0."""
    return 0 if all(leg.passes_fold for leg in legs) else 4
