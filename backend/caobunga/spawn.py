"""Detached subprocess spawn shared by the suite verbs and the usage probes.

A leaf: it imports nothing from `caobunga`, so `suite_verbs` and `usage` can
both depend on it without reaching into each other (CBN-405). The helpers were
born in `suite_verbs` (CBN-174, CBN-EPIC-314.2, CBN-349) and `usage` borrowed
them at CBN-398.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path

# How long `reap` waits for EOF / exit after the group kill (CBN-EPIC-314.2).
# Same figure as the worker's `local.TERMINATE_GRACE`, kept local so this
# module does not depend on the worker adapter.
KILL_GRACE = 5.0


def run_detached(
    argv: Sequence[str],
    cwd: Path | None,
    timeout: float,
    *,
    merge_stderr: bool = True,
) -> tuple[int, str]:
    """Run `argv` detached from the caller's own stdio.

    `stdin` is `DEVNULL`, never inherited: a child that reads it (`just` → bash
    → pytest / vitest all will) would take the caller's stdin with it —
    measured live at CBN-174, when that stdin was an MCP transport, as a server
    silently relaunched mid-call while the abandoned call ran out its whole
    timeout with no suite ever started.
    `start_new_session` puts the child in its own process group so a timeout
    kills `just`'s whole tree, not only `just`, leaving no orphaned suite to
    collide with the next call — and so does any other exception mid-run, an
    interrupt included (CBN-EPIC-314.2). `errors="replace"`: a suite printing
    non-UTF-8 bytes is still a result, not a `UnicodeDecodeError`.
    `merge_stderr=False` discards stderr for a caller that parses stdout, and
    `cwd=None` runs in the caller's cwd (CBN-398). The default suits a suite
    verb, whose tail wants both streams from the repo root; a usage probe
    passes `cwd=None, merge_stderr=False`.
    """
    proc = subprocess.Popen(  # noqa: S603
        list(argv),
        cwd=None if cwd is None else str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        output = reap(proc)
        raise subprocess.TimeoutExpired(exc.cmd, timeout, output=output) from None
    except BaseException:
        reap(proc)
        raise
    return proc.returncode, output


def reap(proc: subprocess.Popen[str]) -> str:
    """Kill `proc`'s whole group and collect what it printed, within `KILL_GRACE`.

    The drain is bounded: a grandchild that left the group (`setsid`, a daemon
    double-fork) survives the `killpg` holding the stdout pipe open, and an
    unbounded `communicate()` would wait on that EOF forever. On a second
    timeout the partial output is kept and the pipe closed; the killed leader
    is still reaped. `PermissionError` is macOS's answer for a group whose members
    have all exited unreaped — as gone as `ProcessLookupError` (CBN-349).
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        output, _ = proc.communicate(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired as exc:
        output = as_text(exc.output)
        if proc.stdout is not None:
            proc.stdout.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=KILL_GRACE)
    return output or ""


def as_text(raw: str | bytes | None) -> str:
    """Coerce `TimeoutExpired.output` / `.stderr` (bytes, str, or `None`) to `str`."""
    if raw is None:
        return ""
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
