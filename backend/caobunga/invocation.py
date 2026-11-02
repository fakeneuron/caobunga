"""Invocation accounting; recovery text lives in recovery.py, task evidence in the run ledger."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import yaml
from pydantic import ValidationError

from caobunga.errors import GitUnreadable, UserError
from caobunga.flaitron import ProjectRead, list_open_tasknotes
from caobunga.git import DIRT_SAMPLE, commits_after, commits_since, git_dirt, git_head
from caobunga.models import (
    CLOSURE_OUTCOMES,
    ExecutionTier,
    InvocationDenial,
    InvocationPark,
    InvocationResult,
    InvocationStatus,
    LandingState,
    Outcome,
    RegistryRecord,
    RunRecord,
    StaleReceipt,
    StopReason,
    Suite,
    SuiteVerdict,
    WorktreeRun,
    compact_utc,
    utc_now,
)
from caobunga.recovery import recovery_steps, registry_changed_recovery, retained_recovery
from caobunga.suite import suite_stops

if TYPE_CHECKING:
    from caobunga.store import Store
    from caobunga.worktree import WorktreeContext


def _snapshot(result: InvocationResult, last: RunRecord | None, root: Path | None = None) -> None:
    """Fill the receipt's repo evidence: HEAD, tree, open tasknote, live log pointers.

    Shared by a finishing trace and a stale receipt's reconcile (CBN-376).
    `root` overrides the registered path: a retained isolated worktree's
    execution root is where the run's evidence is (CBN-EPIC-450.4).
    """
    if result.path is None:
        return
    root = root or Path(result.path)
    # The refusal already waited on this repo's git; re-reading it here would
    # wait `GIT_TIMEOUT` twice more for the same "unknown" (CBN-320).
    if result.stop_reason == StopReason.GIT_UNREADABLE:
        result.head_sha = None
        result.tree = None
    else:
        try:
            result.head_sha = git_head(root)
        except GitUnreadable:
            result.head_sha = None
        result.tree = git_dirt(root)
    notes = list_open_tasknotes(root)
    note = next((n for n in notes if n.task_id == result.task_id), None)
    if note is not None:
        result.tasknote_path = str(root / note.path)
        result.tasknote_status = note.status
        result.park_reason = note.park_reason
    elif last is not None and last.task_id == result.task_id:
        result.tasknote_path = str(root / last.tasknote_path) if last.tasknote_path else None
        result.park_reason = last.park_reason
    for key in ("worker_log", "suite_log"):
        value = getattr(result, key)
        if value is not None and not Path(value).is_file():
            setattr(result, key, None)


def _suite_detail(suite: Suite | None) -> str | None:
    """Why a closure was never offered to land, when its suite says so."""
    if suite is None or suite.verdict is SuiteVerdict.GREEN:
        return None
    return f"its suite was {suite.verdict.value}"


def closed_green(run: RunRecord) -> bool:
    """A committed close (`closed` / `de-scoped` / `no-tasknote`) whose suite passed (CBN-301).

    It is task evidence on every tier; only a drain also stops on the last two.
    """
    return run.outcome in CLOSURE_OUTCOMES and not suite_stops(run)


@dataclass
class InvocationTrace:
    """Mutable execution context. Checkpoints reference records immediately after write."""

    project: str
    store: Store
    result: InvocationResult = field(init=False)
    last: RunRecord | None = None
    runs: list[RunRecord] = field(default_factory=list)
    # HEAD the current task launched from (CBN-284); with a clean `tree`, a final
    # HEAD equal to it means the failed worker left nothing behind.
    start_sha: str | None = None
    completed: set[str] = field(default_factory=set)
    failed_shutdowns: list[str] = field(default_factory=list)
    # Registry names changed by a run that raised after launch (CBN-428); a
    # normal return names them on its task record instead.
    registry_changed: list[str] = field(default_factory=list)
    # The current task's isolated worktree (CBN-EPIC-450.4), from its start
    # until it lands and is removed; still set at `finish`, it is retained.
    worktree: WorktreeContext | None = None
    # Why the current task's green closure did not land, when it did not, and
    # the branch main had checked out when its worktree started.
    unlanded: str | None = None
    landing_ref: str | None = None
    # Recovery for worktrees an earlier task in this invocation left behind.
    retained: list[str] = field(default_factory=list)
    # The main `HEAD` a suite last measured green: the baseline's, then each
    # landed closure's. An isolated dispatch from any other `HEAD` re-measures
    # its baseline (CBN-EPIC-450.5).
    green_sha: str | None = None
    # The current task's receipt entry (CBN-EPIC-450.6). Settled `landed` and
    # checkpointed the moment main is fast-forwarded, so a run that dies before
    # its record is written never reads as holding unlanded work.
    _entry: WorktreeRun | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        now = utc_now()
        self.result = InvocationResult(
            project=self.project,
            invocation_id=compact_utc(now),
            receipt_id=f"{compact_utc(now)}-{uuid4().hex}",
            started_at=now,
            pid=os.getpid(),
        )

    def register(self, record: RegistryRecord) -> None:
        self.result.path = record.path
        self.result.tier = record.tier

    def checkpoint(self) -> None:
        if self.result.tier is not ExecutionTier.OBSERVE:
            self.store.write_invocation(self.result)

    def worker_shutdown(self, session_name: str, *, failed: bool) -> None:
        if failed:
            self.failed_shutdowns.append(session_name)
            self.result.shutdown = "failed"
        elif self.result.shutdown != "failed":
            self.result.shutdown = "returned"

    def saw_read(self, read: ProjectRead, pinned: frozenset[str], *, first: bool) -> None:
        result = self.result
        result.denials = [
            InvocationDenial(task_id=d.task_id, reason=d.reason, line=d.line) for d in read.denied
        ]
        result.unparsed_lines = [row.line for row in read.coverage.unparsed_rows]
        if first:
            if result.tier is ExecutionTier.GATED and read.candidate is not None:
                result.authorized_tasks = [read.candidate.task_id]
            else:
                marked_denied = {
                    task.task_id
                    for task in read.tasks
                    if task.unattended and task.task_id in {d.task_id for d in read.denied}
                }
                result.authorized_tasks = sorted(pinned | marked_denied)
        result.remaining_tasks = [t for t in result.authorized_tasks if t not in self.completed]

    def wrote_run(self, run: RunRecord) -> None:
        self.last = run
        self.runs.append(run)
        result = self.result
        result.run_records.append(str(self.store.run_file(run)))
        if run.outcome in CLOSURE_OUTCOMES:
            result.last_commit_sha = run.commit_sha or result.last_commit_sha
        result.escalation_reason = run.escalation_reason
        result.worker_log = run.log_path
        if run.suite is not None:
            path = self.store.suite_file(run.project, compact_utc(run.closed_at), run.task_id)
            # This pointer is prospective until the loop saves the suite log.
            result.suite_log = str(path)
        if closed_green(run):
            self.completed.add(run.task_id)
        result.remaining_tasks = [t for t in result.authorized_tasks if t not in self.completed]
        for park in result.parked:
            if park.held is None:
                park.continued_with.append(run.task_id)
        self.checkpoint()

    def record_park(self, run: RunRecord, held: str | None, *, descoped: bool) -> None:
        """A drain's park, and whether it held the repo (CBN-443); later runs extend it."""
        self.result.parked.append(
            InvocationPark(
                task_id=run.task_id,
                park_reason=run.park_reason,
                park_prose=run.park_prose,
                held=held,
                descoped=descoped,
            )
        )
        self.checkpoint()

    def start_worktree(self, context: WorktreeContext) -> None:
        """Put a just-started worktree on the trace and the receipt (CBN-EPIC-450.6).

        Checkpointed at once, so a receipt the orchestrator never finishes still
        names the worktree holding its task's work.
        """
        self.worktree = context
        self._entry = WorktreeRun(
            branch=context.branch,
            worker_root=str(context.worker_root),
            execution_root=str(context.execution_root),
            landing_root=str(context.landing_root),
            base_sha=context.base_sha,
        )
        self.result.worktrees.append(self._entry)
        self.checkpoint()

    def mark_landed(self, sha: str) -> None:
        """Main now holds the current task's branch at `sha`; checkpoint at once."""
        if self._entry is None:
            return
        self._entry.landing = LandingState.LANDED
        self._entry.landed_sha = sha
        self.checkpoint()

    def note_landing(self, detail: str) -> None:
        """What a landing left behind (a worktree not removed, submodules not updated)."""
        if self._entry is None:
            return
        self._entry.detail = "; ".join(part for part in (self._entry.detail, detail) if part)
        self.checkpoint()

    def settle_worktree(self, outcome: Outcome, suite: Suite | None) -> WorktreeRun | None:
        """The current task's final worktree state, also set on its receipt entry.

        Landed when its branch fast-forwarded main. Otherwise a closure is
        `unlanded`: its outcome is a close main does not have. Anything else is
        `retained`. `None` when this task started no worktree.
        """
        entry = self._entry
        if entry is None:
            return None
        if entry.landing is None and outcome in CLOSURE_OUTCOMES:
            entry.landing = LandingState.UNLANDED
            entry.detail = self.unlanded or _suite_detail(suite)
        elif entry.landing is None:
            entry.landing = LandingState.RETAINED
        return entry.model_copy()

    def retire_worktree(self) -> None:
        """Move a retained worktree onto the recovery list and off the trace (CBN-EPIC-450.5).

        Called before the next task's read, so a drain that continued past an
        isolated park names every worktree it left, and at `finish`. A receipt
        entry no run record settled (a refusal before launch, a raise) is
        settled `retained` here; a landed one already says so.
        """
        if self._entry is not None and self._entry.landing is None:
            self._entry.landing = LandingState.RETAINED
        self._entry = None
        context = self.worktree
        if context is None:
            return
        self.retained.append(
            retained_recovery(
                branch=context.branch,
                worker_root=str(context.worker_root),
                landing_root=str(context.landing_root),
                base_sha=context.base_sha,
            )
        )
        self.worktree = None

    def snapshot(self) -> None:
        """Best-effort diagnostics cannot replace the execution's stop reason."""
        root = self.worktree.execution_root if self.worktree is not None else None
        _snapshot(self.result, self.last, root)

    def finish(self, *, exit_code: int) -> InvocationResult:
        result = self.result
        result.finished_at = utc_now()
        result.legacy_exit_code = exit_code
        if result.shutdown == "failed":
            result.status = InvocationStatus.INCOMPLETE
        if result.status is InvocationStatus.RUNNING:
            result.status = (
                InvocationStatus.INCOMPLETE
                if result.remaining_tasks or result.unparsed_lines
                else InvocationStatus.COMPLETE
            )
        result.successful = result.status in {
            InvocationStatus.COMPLETE,
            InvocationStatus.EMPTY,
            InvocationStatus.OBSERVED,
        }
        result.approved_scope_finished = result.successful and not result.remaining_tasks
        if result.tier is not ExecutionTier.OBSERVE:
            try:
                self.snapshot()
            except (OSError, UserError):
                # Diagnostics are best effort; the terminal decision stays intact.
                result.head_sha = None
                result.tree = None
        result.recovery = recovery_steps(result, self.last, self.start_sha)
        if self.registry_changed:
            result.recovery.append(registry_changed_recovery(self.registry_changed))
        self.retire_worktree()
        result.recovery.extend(self.retained)
        if self.failed_shutdowns:
            result.recovery.append(
                "Inspect and stop sessions with failed shutdowns: "
                f"{', '.join(self.failed_shutdowns)}. "
                "Check their task records and diagnostic logs before any new launch."
            )
        self.checkpoint()
        return result


def _pid_alive(pid: int) -> bool:
    """Whether `pid` names a live process. A reused pid reads as alive: the safe way to be wrong."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def receipt_alive(receipt: InvocationResult) -> bool | None:
    """A `running` receipt's orchestrator verdict without the run lock (CBN-421).

    `None` when it carries no pid: only `reconcile_stale`, holding the lock, can
    call a legacy receipt dead.
    """
    return None if receipt.pid is None else _pid_alive(receipt.pid)


def running_verdicts(
    store: Store, project_id: str | None
) -> list[tuple[InvocationResult, bool | None]]:
    """Each `running` receipt with its `receipt_alive` verdict, for `status` (CBN-421).

    A dead pid is checked against a re-read: a run that wrote its terminal
    receipt and exited after the scan is finished, not stale.
    """
    verdicts: list[tuple[InvocationResult, bool | None]] = []
    for receipt in store.running_invocations(project_id):
        alive = receipt_alive(receipt)
        if alive is False:
            try:
                current = store.load_invocation(receipt.receipt_id)
            except (OSError, UserError, yaml.YAMLError, ValidationError):
                current = None
            if current is None or current.status is not InvocationStatus.RUNNING:
                continue
        verdicts.append((receipt, alive))
    return verdicts


def _live_worktree(receipt: InvocationResult) -> WorktreeRun | None:
    """The worktree a receipt's unfinished task ran in, when it names one (CBN-EPIC-450.6)."""
    entries = receipt.worktrees
    return entries[-1] if entries and entries[-1].landing is None else None


def evidence_root(receipt: InvocationResult, registered: str) -> Path:
    """Where a dead run's work is: its live worktree's execution root, else its own path.

    An isolated run commits on its `wt-<ID>` branch, so main's `HEAD` would show
    none of it. A worktree removed by hand falls back to the path, which is the
    only place left to read.
    """
    live = _live_worktree(receipt)
    if live is not None and Path(live.execution_root).is_dir():
        return Path(live.execution_root)
    return Path(receipt.path or registered)


def _landed(receipt: InvocationResult, root: Path) -> list[str] | None:
    """A dead run's commits: its branch's own (`base..HEAD`) when read from its
    live worktree, else HEAD's since the receipt started."""
    live = _live_worktree(receipt)
    if live is not None and root == Path(live.execution_root):
        return commits_after(root, live.base_sha)
    return commits_since(root, receipt.started_at)


def stale_receipts(
    record: RegistryRecord, verdicts: Sequence[tuple[InvocationResult, bool | None]]
) -> list[StaleReceipt]:
    """`record`'s proven-dead receipts among `verdicts`, with commits landed since (CBN-444).

    The surfaces' shared read, so `status`, the digest and the Status API name
    the same work: a dead run that committed before dying otherwise shows the
    previous task everywhere until the slug's next `run`. Landed is read from
    the receipt's own path (its live worktree for an isolated run), as
    `reconcile_stale` does, since the slug may have moved. A pid-less receipt
    is not proven dead without the lock, so it is left out, on CBN-421's footer
    rule.
    """
    stale: list[StaleReceipt] = []
    for receipt, alive in verdicts:
        if alive is not False or receipt.project != record.id or receipt.pid is None:
            continue
        stale.append(
            StaleReceipt(
                receipt_id=receipt.receipt_id,
                task_id=receipt.task_id,
                pid=receipt.pid,
                started_at=receipt.started_at,
                last_stage=receipt.stop_reason.value,
                landed=_landed(receipt, evidence_root(receipt, record.path)),
            )
        )
    return stale


def reconcile_stale(
    store: Store,
    record: RegistryRecord,
    own_receipt_id: str,
    echo: Callable[[str], None] | None = None,
) -> list[InvocationResult]:
    """Rewrite this project's dead `running` receipts to `abandoned` (CBN-376).

    The caller holds the repo's run lock, so no other caobunga run is live on
    it. A receipt is still only called dead on its own `pid`: a concurrent run
    writes its checkpoint before it tries the lock, and that racer is alive. A
    receipt from before the pid was recorded falls back on the lock alone, and
    only when it names this registered path, which is the repo the lock covers.
    Report-only on the target repo: reads HEAD, the tree and the open tasknote,
    and never cleans, commits or relaunches anything. A failed rewrite is echoed
    and skipped, never this run's stop: the stale receipt just stays `running`.
    """
    reconciled: list[InvocationResult] = []
    for stale in store.running_invocations(record.id):
        if stale.receipt_id == own_receipt_id:
            continue
        alive = receipt_alive(stale)
        if alive or (alive is None and stale.path != record.path):
            continue
        stage = stale.stop_reason
        pid = f"pid {stale.pid}" if stale.pid is not None else "pid unrecorded"
        stale.status = InvocationStatus.ABANDONED
        stale.stop_reason = StopReason.PROCESS_GONE
        stale.finished_at = utc_now()
        root = evidence_root(stale, record.path)
        # Unsettled means never landed: its branch, if still there, holds the work.
        live = _live_worktree(stale)
        try:
            _snapshot(stale, None, root)
        except (OSError, UserError):
            stale.head_sha = None
            stale.tree = None
        # The dead run's own repo, the one `_snapshot` read: the slug may have moved since.
        landed = _landed(stale, root)
        landed_text = "unreadable" if landed is None else f"{len(landed)} commit(s)"
        on_branch = live is not None and root == Path(live.execution_root)
        where = f"{live.branch} since its base" if on_branch and live else "HEAD since started_at"
        stale.detail = (
            f"Orchestrator gone while running ({pid}, last stage {stage}); found by "
            f"invocation {own_receipt_id}. Landed on {where}: {landed_text}."
        )
        stale.recovery = recovery_steps(stale, None)
        if live is not None:
            live.landing = LandingState.RETAINED
            stale.recovery.append(
                retained_recovery(
                    branch=live.branch,
                    worker_root=live.worker_root,
                    landing_root=live.landing_root,
                    base_sha=live.base_sha,
                )
            )
        try:
            path = store.write_invocation(stale)
        except OSError as exc:
            # Left `running` is still the honest "unknown"; this run is not the dead one's.
            if echo is not None:
                echo(f"stale-receipt: {stale.receipt_id} not rewritten: {exc}")
            continue
        reconciled.append(stale)
        if echo is not None:
            _echo_stale(stale, landed, path, echo, pid=pid, stage=stage)
    return reconciled


def _echo_stale(
    stale: InvocationResult,
    landed: list[str] | None,
    path: Path,
    echo: Callable[[str], None],
    *,
    pid: str,
    stage: str,
) -> None:
    echo(
        f"stale-receipt: {stale.receipt_id} running since {stale.started_at.isoformat()} "
        f"({pid} gone, last stage {stage}) → abandoned"
    )
    if landed is None:
        echo("stale-receipt: landed: unreadable")
    else:
        shown = "; ".join(landed) if landed else "nothing"
        echo(f"stale-receipt: landed since start: {len(landed)} commit(s) — {shown}")
    if stale.tree is None:
        tree = "tree unreadable"
    else:
        sample = "; ".join(line.strip() for line in stale.tree[:DIRT_SAMPLE])
        more = f" (+{len(stale.tree) - DIRT_SAMPLE} more)" if len(stale.tree) > DIRT_SAMPLE else ""
        tree = f"tree {len(stale.tree)} file(s)" + (f" ({sample}{more})" if sample else "")
    if stale.tasknote_status is not None:
        note = f"tasknote {stale.tasknote_path} ({stale.tasknote_status})"
    else:
        note = "no open tasknote"
    echo(f"stale-receipt: left: {tree}; {note}")
    echo(f"stale-receipt-record: {path}")
