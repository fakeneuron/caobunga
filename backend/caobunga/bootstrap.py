"""Prepare a fresh execution worktree before its baseline suite (CBN-EPIC-450.3).

A `wt-<ID>` worktree (`worktree.start`) is a clean checkout with nothing that
`.gitignore` covers: no `.venv`, no `node_modules`, no `.env`. Nothing is
copied or linked in from the main checkout, because a shared dependency tree is
state two checkouts would race on, and an ignored file may be a secret. So a
project that needs installs before `just test` can pass names its own command
at register time (`bootstrap`, usually `just setup`), and nothing is inferred
from the package managers present.

Readiness runs in this order, and the first failure stops the launch:

1. `worktree.start` — branch, worktree, and pinned submodules.
2. `run_bootstrap` — the registered argv at the execution root, detached and
   under its timeout, with no shell. Absent configuration skips only this step:
   such a project's suite has to prepare itself.
3. `refuse_unready` — a failed bootstrap refuses, and so does any non-ignored
   dirt left in the worktree, configured or not.
4. The CBN-420 baseline (`loop._refuse_if_red_baseline`) on the execution root,
   which the suite runner accepts as registered (`worktree.origin`).

Nothing here retries, launches a worker, or removes anything: a refused
worktree and its branch stay for the operator to inspect. The loop runs this
order for a record with `isolate` set (CBN-EPIC-450.4, `loop._ready_worktree`).
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from caobunga.errors import UserError
from caobunga.git import git_porcelain
from caobunga.models import RegistryRecord
from caobunga.spawn import as_text, run_detached
from caobunga.suite_verbs import TAIL_CHARS, RunFn, tail_output
from caobunga.worktree import WorktreeContext

# Ceiling when the record names none: a cold `uv sync` plus `npm install`
# can take minutes on a slow network, and a hang must still end.
DEFAULT_TIMEOUT = 600.0
# Porcelain lines named in a dirt refusal; the rest are counted.
_DIRT_SHOWN = 5


@dataclass(frozen=True)
class BootstrapResult:
    """One bootstrap run. Command failure is a result here, never a raise."""

    argv: tuple[str, ...]
    cwd: Path
    exit_code: int | None
    duration: float
    output: str
    truncated: bool = False
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0

    @property
    def failure(self) -> str | None:
        """Why it did not pass, in a phrase; `None` when it did."""
        if self.error is not None:
            return f"could not start: {self.error}"
        if self.timed_out:
            return f"timed out after {self.duration:.0f}s"
        if self.exit_code != 0:
            return f"exited {self.exit_code}"
        return None

    def render(self) -> str:
        """The log body a caller saves. Raw output goes here, never in a refusal."""
        lines = [
            f"bootstrap: `{shlex.join(self.argv)}` in {self.cwd}",
            f"result: {self.failure or 'ok'}",
            f"duration: {self.duration:.1f}s",
            "output"
            + (f" (last {TAIL_CHARS} chars; head dropped)" if self.truncated else "")
            + ":",
            self.output.rstrip() or "(no output)",
        ]
        return "\n".join(lines)


def run_bootstrap(
    context: WorktreeContext, record: RegistryRecord, *, run: RunFn | None = None
) -> BootstrapResult | None:
    """Run `record.bootstrap` at `context.execution_root`; `None` when none is registered."""
    if record.bootstrap is None:
        return None
    argv: Sequence[str] = tuple(record.bootstrap)
    timeout = record.bootstrap_timeout or DEFAULT_TIMEOUT
    cwd = context.execution_root
    started = time.monotonic()
    exit_code: int | None = None
    timed_out = False
    error: str | None = None
    try:
        exit_code, output = (run or run_detached)(argv, cwd, timeout)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        output = as_text(exc.output)
    except OSError as exc:
        error = str(exc)
        output = ""
    tail, truncated = tail_output(output)
    return BootstrapResult(
        argv=tuple(argv),
        cwd=cwd,
        exit_code=exit_code,
        duration=time.monotonic() - started,
        output=tail,
        truncated=truncated,
        timed_out=timed_out,
        error=error,
    )


def refuse_unready(context: WorktreeContext, result: BootstrapResult | None) -> None:
    """Refuse (`UserError`, exit 2) unless the bootstrap passed and the worktree is clean.

    `result` is `None` when no bootstrap is registered; the dirt check still
    runs. A hung or missing `git` raises `GitUnreadable` (exit 5) from the
    strict porcelain read, so an unreadable tree never passes for a clean one.
    """
    if result is not None and result.failure is not None:
        raise UserError(
            f"{context.branch}: bootstrap `{shlex.join(result.argv)}` {result.failure} "
            f"in {result.cwd}; not launching. {_retained(context)}"
        )
    dirt = git_porcelain(context.worker_root)
    if dirt is None:
        raise UserError(
            f"{context.branch}: cannot read git status in {context.worker_root}; "
            f"not launching. {_retained(context)}"
        )
    if dirt:
        shown = "; ".join(line.strip() for line in dirt[:_DIRT_SHOWN])
        more = f" (+{len(dirt) - _DIRT_SHOWN} more)" if len(dirt) > _DIRT_SHOWN else ""
        cause = "after bootstrap" if result is not None else "with no bootstrap registered"
        raise UserError(
            f"{context.branch}: {len(dirt)} uncommitted change(s) in {context.worker_root} "
            f"{cause}: {shown}{more} — a launch needs the base commit's tree, and a "
            f"bootstrap may write only ignored paths; not launching. {_retained(context)}"
        )


def _retained(context: WorktreeContext) -> str:
    return (
        f"Retained, not removed: branch {context.branch} and worktree {context.worker_root} "
        "— inspect and clean up before redispatching."
    )
