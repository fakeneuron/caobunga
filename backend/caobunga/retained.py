"""Report-side read of retained isolated work (CBN-EPIC-450.6).

What the read-only surfaces (`status`, the digest, the last-run card, the
Status API) show about `wt-<ID>` leftovers. `report_retained` returns the ids
the drain would deny, so a report's pick can skip them too. `retained_worktrees`
returns one entry per id, with its landing state and an attended recovery
step. Both are read per call from git and the run records, never stored, so a
restart or an orphan reads the same way the drain's own pick does.

A report never refuses. When git cannot say, the answer is `None`, which the
surfaces print as unreadable. Nothing here removes, lands or resumes anything:
the recovery text names the commands, and the operator runs them. A leaf
module: `errors`, `models`, `recovery` and `worktree` cross in.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from caobunga.errors import UserError
from caobunga.models import (
    LandingState,
    RegistryRecord,
    RetainedWorktree,
    RunRecord,
    WorktreeRun,
)
from caobunga.recovery import cleanup_recovery, orphan_recovery, retained_recovery
from caobunga.worktree import (
    branch_name,
    main_checkout,
    retained_ids,
    started_at,
    worktree_path,
)


def drain_retained(record: RegistryRecord) -> frozenset[str]:
    """The ids the drain denies `retained`: strict, so an unreadable read refuses.

    Empty for a project that does not isolate (CBN-EPIC-450.5 scope). The one
    definition of that scope, shared by the drain and `report_retained`.
    """
    if not record.isolate:
        return frozenset()
    return retained_ids(Path(record.path))


def report_retained(record: RegistryRecord) -> frozenset[str] | None:
    """`drain_retained` for a report: `None` when git could not say, never a refusal."""
    try:
        return drain_retained(record)
    except UserError:
        return None


def unlanded(run: RunRecord | None) -> bool:
    """A closure main did not get: the outcome says closed, main does not have it."""
    return (
        run is not None
        and run.worktree is not None
        and run.worktree.landing is LandingState.UNLANDED
    )


def retained_worktrees(
    record: RegistryRecord,
    ids: frozenset[str] | None,
    runs: Callable[[], Sequence[RunRecord]],
) -> list[RetainedWorktree] | None:
    """One entry per retained id, in id order; `None` when git could not say.

    `[]` for a project that does not isolate or has nothing retained, without
    calling `runs`, so a report reads the run records only when it lists any.

    Each entry takes its landing state from the newest run record whose
    `worktree.branch` names it, but only when that branch was created at the
    record's `base_sha` (`worktree.started_at`). Otherwise the record is an
    earlier dispatch's, its branch since removed, and the one there now wrote no
    record. An id with no matching record gets the orphan recovery step.
    """
    if not record.isolate:
        return []
    if ids is None:
        return None
    if not ids:
        return []
    try:
        landing_root = main_checkout(Path(record.path)).toplevel
    except UserError:
        return None
    newest: dict[str, WorktreeRun] = {}
    for run in sorted(runs(), key=lambda item: item.closed_at):
        if run.worktree is not None:
            newest[run.worktree.branch] = run.worktree
    try:
        return [_entry(task_id, landing_root, newest) for task_id in sorted(ids)]
    except UserError:
        return None


def _entry(task_id: str, landing_root: Path, newest: dict[str, WorktreeRun]) -> RetainedWorktree:
    branch = branch_name(task_id)
    state = newest.get(branch)
    if state is not None and not started_at(landing_root, branch, state.base_sha):
        state = None
    path = state.worker_root if state is not None else str(worktree_path(landing_root, task_id))
    main = str(landing_root)
    if state is None:
        recovery = orphan_recovery(
            task_id=task_id, branch=branch, worker_root=path, landing_root=main
        )
    elif state.landing is LandingState.LANDED and state.landed_sha is not None:
        recovery = cleanup_recovery(
            task_id=task_id,
            sha=state.landed_sha,
            branch=branch,
            worker_root=path,
            landing_root=main,
            why=state.detail or "cleanup did not finish",
        )
    else:
        recovery = retained_recovery(
            branch=branch, worker_root=path, landing_root=main, base_sha=state.base_sha
        )
    return RetainedWorktree(
        task_id=task_id,
        branch=branch,
        path=path,
        landing=state.landing if state is not None else None,
        detail=state.detail if state is not None else None,
        recovery=recovery,
    )
