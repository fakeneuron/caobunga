"""Operator recovery steps written into an invocation receipt.

`recovery_steps` turns a finished (or found-dead) receipt and its last run into
safe operator actions — never automatic cleanup or a generic retry. `_RECOVERY`
maps a stop reason to its step. A leaf module: `models`, the flaitron façade and
`text` cross in. Split out of `invocation.py` at CBN-399.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from caobunga.flaitron import is_descoped
from caobunga.models import (
    Escalation,
    InvocationResult,
    InvocationStatus,
    Outcome,
    RunRecord,
    StopReason,
)
from caobunga.text import read_text_or_none

_RECOVERY: dict[StopReason, str] = {
    StopReason.ACTIVE_RUN: (
        "Wait for the active run to finish; inspect its invocation and logs before rerunning."
    ),
    StopReason.DIRTY_TREE: (
        "Inspect the listed changes and open tasknote; preserve partial work and resolve it "
        "attended before rerunning from a ready tree."
    ),
    StopReason.PROVIDER: (
        "Check the named provider/model, CLI installation, authentication and model access; "
        "inspect the diagnostic pointer before rerunning from a ready tree."
    ),
    StopReason.SUITE: (
        "Keep the real closure commit. Fix the failing, timed-out or refused suite verbs and "
        "rerun just test, just lint and just typecheck to green before authorizing continuation; "
        "do not rewrite the historical run record."
    ),
    StopReason.BASELINE: (
        "The suite was already failing on the clean tree before launch, so no worker ran. "
        "Read the suite log, fix the failing, timed-out or refused verbs and rerun just test, "
        "just lint and just typecheck to green, then rerun."
    ),
    StopReason.UNPINNED: (
        "Review who changed the deferred row and its approval before a new invocation pins it; "
        "do not automatically relaunch it."
    ),
    StopReason.GIT_UNREADABLE: (
        "Check that git is installed and that no hung git process holds the target repo; "
        "rerun once git answers there."
    ),
    StopReason.NOT_A_REPO: (
        "Run git rev-parse HEAD in the registered path: it must be a repository git may read "
        "(ownership, safe.directory, permissions) with at least one commit. Fix that, or "
        "re-register the project at its checkout, then rerun."
    ),
    StopReason.HEAD_LOST: (
        "HEAD became unreadable after the worker launched. Run git rev-parse HEAD in the "
        "registered path, check for a still-running worker process, and inspect the tree and "
        "tasknote for the worker's partial work before restoring the repository; do not "
        "rerun until HEAD is readable again."
    ),
    StopReason.INTERRUPTED: (
        "Inspect the invocation's task records, actual commits, partial work and live processes; "
        "reconcile closure evidence and restore readiness before resuming. "
        "Repair the reporting/storage error if present."
    ),
    StopReason.PROCESS_GONE: (
        "The orchestrator died without writing its receipt (SIGKILL, an out-of-memory kill, or "
        "a forced quit). Its worker ran in its own session; its output pipe died with the "
        "orchestrator, so it usually dies at its next write, but a quiet one may still be "
        "alive: check for a worker process in the registered path first. Then inspect the "
        "commits that landed "
        "since started_at, the tree and the open tasknote; resume partial work attended and "
        "verify the atomic PLAN/archive/deliverable commit before rerunning."
    ),
    StopReason.WORKTREE: (
        "The execution worktree could not be readied (a leftover from an earlier dispatch, a "
        "failed bootstrap, or dirt), so no worker ran. Read the refusal and any bootstrap log, "
        "fix the cause (often caobunga register --bootstrap), remove the named branch and "
        "worktree once inspected, then rerun."
    ),
    StopReason.LANDING: (
        "A verified closure did not fast-forward into the main checkout (the detail says why), "
        "so main was not changed and the closure is still only on its branch. Settle main, then "
        "land or integrate the branch attended (see the retained-worktree step) before "
        "authorizing continuation."
    ),
}
_RECOVERY[StopReason.WORKER_START] = _RECOVERY[StopReason.PROVIDER]
_RECOVERY[StopReason.REPORT] = _RECOVERY[StopReason.INTERRUPTED]
_RECOVERY[StopReason.ORCHESTRATOR_ERROR] = _RECOVERY[StopReason.INTERRUPTED]
_NO_RECOVERY_STOPS = frozenset(
    {
        StopReason.EMPTY,
        StopReason.COMPLETE,
        StopReason.INCOMPLETE,
        StopReason.GATED,
        StopReason.DENIED,
    }
)


_RESTORE_REGISTRY = (
    "If the change was not your own, restore each project file with caobunga register "
    "and config.yaml with caobunga providers before rerunning"
)


def registry_changed_recovery(names: list[str]) -> str:
    """Receipt step for a registry change no task record carries: the run raised (CBN-428)."""
    return (
        f"The registry changed during the run and no task record carries it: "
        f"{', '.join(names)}. {_RESTORE_REGISTRY}."
    )


def retained_recovery(*, branch: str, worker_root: str, landing_root: str, base_sha: str) -> str:
    """Receipt step for an isolated worktree left in place (CBN-EPIC-450.4)."""
    main = shlex.quote(landing_root)
    return (
        f"Retained, not landed: branch {branch} at {worker_root}; Caobunga did not land it "
        f"into {landing_root}. Inspect the work there. If it is a verified closure and main "
        f"is still clean at {base_sha[:12]}, land it with "
        f"git -C {main} merge --ff-only {branch}; otherwise integrate or resume it attended. "
        f"Once the work is landed or saved and git -C {shlex.quote(worker_root)} status is "
        f"clean (--force below drops uncommitted files), remove it: git -C {main} worktree "
        f"remove --force {shlex.quote(worker_root)} && git -C {main} branch -d {branch} "
        "(-D only to discard unlanded work deliberately). Caobunga never merges, rebases, "
        "retries or deletes it, and refuses to redispatch that task while the branch exists."
    )


def orphan_recovery(*, task_id: str, branch: str, worker_root: str, landing_root: str) -> str:
    """Report step for a `wt-<ID>` leftover no run record names (CBN-EPIC-450.6).

    A refusal after the worktree started (a failed bootstrap, a red baseline),
    a run that died before writing its record, or a branch made by hand.
    """
    main = shlex.quote(landing_root)
    return (
        f"{branch} has no run record naming it: a refusal after its worktree started "
        "(see that invocation's receipt), a run that died, or a branch made by hand. "
        f"Inspect git -C {main} log {branch} and {worker_root} if it exists. Resume or land "
        f"any work attended; once it is saved (--force below drops uncommitted files, such as "
        f"a failed bootstrap's output), remove it: git -C {main} worktree remove --force "
        f"{shlex.quote(worker_root)} (or git -C {main} worktree prune when the directory is "
        f"already gone) && git -C {main} branch -d {branch} (-D only to discard unlanded "
        f"work deliberately). Caobunga does not redispatch {task_id} while either remains."
    )


def cleanup_recovery(
    *, task_id: str, sha: str, branch: str, worker_root: str, landing_root: str, why: str
) -> str:
    """Receipt step for a landed closure whose worktree was not removed (CBN-EPIC-450.4)."""
    main = shlex.quote(landing_root)
    return (
        f"{task_id} landed on the main checkout at {sha[:12]}, but its worktree was not "
        f"removed ({why}). Inspect it, then remove it: git -C {main} worktree remove --force "
        f"{shlex.quote(worker_root)} && git -C {main} branch -d {branch}."
    )


def _outcome_recovery(
    last: RunRecord,
    *,
    nothing_ran: bool = False,
    worker_log: str | None = None,
    descoped: bool = False,
) -> str:
    if last.escalation_reason is Escalation.REGISTRY_CHANGED:
        return (
            "The registry changed while the worker ran (the files are named in the run "
            f"summary). {_RESTORE_REGISTRY}; "
            "the rest of the summary says how the run itself ended (commit, park, or exit) "
            "— settle that as you would without the registry change."
        )
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
    if last.escalation_reason is Escalation.WORKER_LAUNCH:
        return _RECOVERY[StopReason.WORKER_START]
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
    if reason is StopReason.OUTCOME and last is not None:
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
    elif reason not in _NO_RECOVERY_STOPS:
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
    if last is not None and last.escalation_reason is Escalation.TIMEOUT:
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
