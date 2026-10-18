"""Invocation accounting and recovery; task evidence stays in the run ledger."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from caobunga.errors import GitUnreadable, UserError
from caobunga.flaitron import ProjectRead, is_descoped, list_open_tasknotes
from caobunga.git import DIRT_SAMPLE, commits_since, git_dirt, git_head
from caobunga.models import (
    CLOSURE_OUTCOMES,
    Escalation,
    ExecutionTier,
    Outcome,
    RegistryRecord,
    RunRecord,
    compact_utc,
    utc_now,
)
from caobunga.suite import suite_stops
from caobunga.text import read_text_or_none

if TYPE_CHECKING:
    from caobunga.store import Store


class InvocationStatus(StrEnum):
    RUNNING = "running"
    OBSERVED = "observed"
    COMPLETE = "complete"
    EMPTY = "empty"
    REFUSED = "refused"
    INCOMPLETE = "incomplete"
    INTERRUPTED = "interrupted"
    ABANDONED = "abandoned"


class InvocationDenial(BaseModel):
    task_id: str
    reason: str
    line: int


class InvocationResult(BaseModel):
    """Separately versioned receipt, including requests with no task record.

    `successful` is for aggregation; `legacy_exit_code` preserves direct-run
    behavior. A running receipt left by a crash is explicitly not success.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    invocation_id: str
    receipt_id: str
    project: str
    started_at: datetime
    pid: int | None = None
    finished_at: datetime | None = None
    status: InvocationStatus = InvocationStatus.RUNNING
    stop_reason: str = "running"
    detail: str = "Invocation has not finished; reconcile committed evidence before resuming."
    successful: bool = False
    approved_scope_finished: bool = False
    legacy_exit_code: int | None = None
    path: str | None = None
    tier: ExecutionTier | None = None
    task_id: str | None = None
    authorized_tasks: list[str] = Field(default_factory=list)
    remaining_tasks: list[str] = Field(default_factory=list)
    run_records: list[str] = Field(default_factory=list)
    denials: list[InvocationDenial] = Field(default_factory=list)
    unparsed_lines: list[int] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    escalation_reason: Escalation | None = None
    last_commit_sha: str | None = None
    head_sha: str | None = None
    tree: list[str] | None = None
    tasknote_path: str | None = None
    tasknote_status: str | None = None
    park_reason: str | None = None
    worker_log: str | None = None
    suite_log: str | None = None
    timeout_seconds: float | None = None
    shutdown: Literal["returned", "failed"] | None = None
    recovery: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _truth_matches_status(self) -> InvocationResult:
        successful = self.status in {
            InvocationStatus.COMPLETE,
            InvocationStatus.EMPTY,
            InvocationStatus.OBSERVED,
        }
        if self.successful != successful:
            raise ValueError("invocation success does not match its status")
        if self.approved_scope_finished != (successful and not self.remaining_tasks):
            raise ValueError("invocation scope completion does not match its evidence")
        if successful and self.remaining_tasks:
            raise ValueError("a successful invocation cannot have remaining approved tasks")
        if self.status is InvocationStatus.ABANDONED:
            # Found dead by a later run (CBN-376): when it died is known only as
            # "by now", and its real exit code not at all.
            if self.finished_at is None or self.legacy_exit_code is not None:
                raise ValueError("an abandoned invocation has a finish time and no legacy exit")
            return self
        terminal = self.status is not InvocationStatus.RUNNING
        if terminal != (self.finished_at is not None and self.legacy_exit_code is not None):
            raise ValueError("invocation terminal status requires finish time and legacy exit")
        return self


def _snapshot(result: InvocationResult, last: RunRecord | None) -> None:
    """Fill the receipt's repo evidence: HEAD, tree, open tasknote, live log pointers.

    Shared by a finishing trace and a stale receipt's reconcile (CBN-376).
    """
    if result.path is None:
        return
    root = Path(result.path)
    # The refusal already waited on this repo's git; re-reading it here would
    # wait `GIT_TIMEOUT` twice more for the same "unknown" (CBN-320).
    if result.stop_reason == "git-unreadable":
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
        self.checkpoint()

    def snapshot(self) -> None:
        """Best-effort diagnostics cannot replace the execution's stop reason."""
        _snapshot(self.result, self.last)

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
        if self.failed_shutdowns:
            result.recovery.append(
                "Inspect and stop sessions with failed shutdowns: "
                f"{', '.join(self.failed_shutdowns)}. "
                "Check their task records and diagnostic logs before any new launch."
            )
        self.checkpoint()
        return result


_RECOVERY = {
    "active-run": (
        "Wait for the active run to finish; inspect its invocation and logs before rerunning."
    ),
    "dirty-tree": (
        "Inspect the listed changes and open tasknote; preserve partial work and resolve it "
        "attended before rerunning from a ready tree."
    ),
    "provider": (
        "Check the named provider/model, CLI installation, authentication and model access; "
        "inspect the diagnostic pointer before rerunning from a ready tree."
    ),
    "suite": (
        "Keep the real closure commit. Fix the failing, timed-out or refused suite verbs and "
        "rerun just test, just lint and just typecheck to green before authorizing continuation; "
        "do not rewrite the historical run record."
    ),
    "unpinned": (
        "Review who changed the deferred row and its approval before a new invocation pins it; "
        "do not automatically relaunch it."
    ),
    "git-unreadable": (
        "Check that git is installed and that no hung git process holds the target repo; "
        "rerun once git answers there."
    ),
    "not-a-repo": (
        "Run git rev-parse HEAD in the registered path: it must be a repository git may read "
        "(ownership, safe.directory, permissions) with at least one commit. Fix that, or "
        "re-register the project at its checkout, then rerun."
    ),
    "head-lost": (
        "HEAD became unreadable after the worker launched. Run git rev-parse HEAD in the "
        "registered path, check for a still-running worker process, and inspect the tree and "
        "tasknote for the worker's partial work before restoring the repository; do not "
        "rerun until HEAD is readable again."
    ),
    "interrupted": (
        "Inspect the invocation's task records, actual commits, partial work and live processes; "
        "reconcile closure evidence and restore readiness before resuming. "
        "Repair the reporting/storage error if present."
    ),
    "process-gone": (
        "The orchestrator died without writing its receipt (an out-of-memory kill or a closed "
        "terminal). Its worker ran in its own session and may still be alive: check for a "
        "worker process in the registered path first. Then inspect the commits that landed "
        "since started_at, the tree and the open tasknote; resume partial work attended and "
        "verify the atomic PLAN/archive/deliverable commit before rerunning."
    ),
}
_RECOVERY["worker-start"] = _RECOVERY["provider"]
_RECOVERY["report"] = _RECOVERY["interrupted"]
_RECOVERY["orchestrator-error"] = _RECOVERY["interrupted"]


def _outcome_recovery(
    last: RunRecord,
    *,
    nothing_ran: bool = False,
    worker_log: str | None = None,
    descoped: bool = False,
) -> str:
    if last.outcome is Outcome.BLOCKED:
        if descoped:
            return (
                f"{last.task_id} parked on a De-scope verdict, so there is no work to resume. "
                "Read the verdict in its tasknote, then confirm and close the row attended "
                "(PLAN flip and archive); do not relaunch the parked row headless."
            )
        return (
            f"Resolve the park reason, then resume {last.task_id} attended with "
            f"/ft-task {last.task_id}; do not relaunch the parked row headless."
        )
    if last.commit_sha is not None:
        return (
            "Inspect the recorded closure outcome and commit before approving the remaining "
            "queue; a committed de-scope or no-tasknote close does not authorize continuation."
        )
    if last.escalation_reason is not None and last.escalation_reason.value == "worker-launch":
        return _RECOVERY["worker-start"]
    if nothing_ran and last.escalation_reason is Escalation.WORKER_EXITED:
        where = f"the worker log ({worker_log})" if worker_log else "the escalation summary"
        return (
            f"Nothing ran for {last.task_id}: the worker exited without touching the tree or "
            f"HEAD (clean tree, HEAD unmoved, no tasknote), so there is no partial work to "
            f"resume. Read {where} for why it stopped before rerunning."
        )
    if nothing_ran:
        return (
            f"Nothing ran for {last.task_id}: the provider refused before any work "
            "(clean tree, HEAD unmoved). Rerunning is safe once the provider has usage."
        )
    return (
        f"Committed closure evidence is missing for {last.task_id}. Inspect HEAD, tree, "
        "tasknote and worker log; resume partial work attended and verify the atomic "
        "PLAN/archive/deliverable commit before rerunning."
    )


def _parked_descoped(last: RunRecord, tasknote_path: str | None) -> bool:
    """A parked note whose Relevance verdict is De-scope has nothing left to resume."""
    if last.outcome is not Outcome.BLOCKED or tasknote_path is None:
        return False
    text = read_text_or_none(Path(tasknote_path))
    return text is not None and is_descoped(text)


def recovery_steps(
    result: InvocationResult, last: RunRecord | None, start_sha: str | None = None
) -> list[str]:
    """Safe operator actions, never automatic cleanup or a generic retry."""
    if result.status is InvocationStatus.OBSERVED:
        return []
    steps: list[str] = []
    reason = result.stop_reason
    if reason == "outcome" and last is not None:
        nothing_ran = (
            (
                last.escalation_reason is Escalation.WORKER_ERROR
                or (
                    last.escalation_reason is Escalation.WORKER_EXITED
                    and result.tasknote_path is None
                )
            )
            and result.tree == []
            and start_sha is not None
            and result.head_sha == start_sha
        )
        steps.append(
            _outcome_recovery(
                last,
                nothing_ran=nothing_ran,
                worker_log=result.worker_log,
                descoped=_parked_descoped(last, result.tasknote_path),
            )
        )
    elif reason in _RECOVERY:
        steps.append(_RECOVERY[reason])
    elif reason not in {"empty", "complete", "incomplete", "gated", "denied"}:
        steps.append(
            "Resolve the reported configuration/readiness error, then inspect caobunga status "
            "for this project before rerunning."
        )
    if result.remaining_tasks or result.denials:
        steps.append(
            "Review remaining tasks and each denial: resolve dependencies and parks, perform "
            "handoffs attended, and review unmarked rows with Flaitron seeding. "
            "Caobunga does not mark or clean the repo."
        )
    if result.unparsed_lines:
        steps.append(
            "Review the listed malformed PLAN rows; fix their syntax and approval explicitly "
            "before rerunning."
        )
    if (
        last is not None
        and last.escalation_reason is not None
        and last.escalation_reason.value == "timeout"
    ):
        steps.append(
            f"Worker exceeded {result.timeout_seconds}s. Shutdown state is "
            f"{result.shutdown or 'unknown'}; verify remaining processes are stopped before "
            "attending to partial work."
        )
    if result.shutdown == "failed":
        steps.append(
            "Worker shutdown failed; confirm its processes are stopped before any new launch."
        )
    return steps


def _pid_alive(pid: int) -> bool:
    """Whether `pid` names a live process. A reused pid reads as alive: the safe way to be wrong."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
        if stale.pid is None:
            if stale.path != record.path:
                continue
        elif _pid_alive(stale.pid):
            continue
        stage = stale.stop_reason
        pid = f"pid {stale.pid}" if stale.pid is not None else "pid unrecorded"
        stale.status = InvocationStatus.ABANDONED
        stale.stop_reason = "process-gone"
        stale.finished_at = utc_now()
        try:
            _snapshot(stale, None)
        except (OSError, UserError):
            stale.head_sha = None
            stale.tree = None
        # The dead run's own repo, the one `_snapshot` read: the slug may have moved since.
        landed = commits_since(Path(stale.path or record.path), stale.started_at)
        landed_text = "unreadable" if landed is None else f"{len(landed)} commit(s)"
        stale.detail = (
            f"Orchestrator gone while running ({pid}, last stage {stage}); found by "
            f"invocation {own_receipt_id}. Landed on HEAD since started_at: {landed_text}."
        )
        stale.recovery = recovery_steps(stale, None)
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
