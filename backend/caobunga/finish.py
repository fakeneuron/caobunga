"""The finish: settle the worker, measure the closed tree, and write the run record.

`caobunga.finish` owns the record-producing half of a run, the helpers
`_run_gated` calls once a close is detected: the settle and suite measure
(`settle_and_measure`, whose `measure_suite` the pre-launch baseline reuses),
the `tree:` refresh applied after `loop` retires the
session and re-reads the close (`refresh_tree`), and the record write with its
`suite:` / `foreign:` / `filed:` echoes and the `report` call (`finish`). A
leaf module: `close`, the flaitron façade, `git`, `invocation`, `launch`, `models`,
`report`, `store`, `suite`, `wait`, and `worker` cross in, and nothing calls back into
`loop.py`. Split out of `loop.py` at CBN-EPIC-363.4.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime
from functools import partial
from pathlib import Path

from caobunga.close import CloseSnapshot, diff_new_rows, reinspect_settled
from caobunga.errors import GitUnreadable
from caobunga.flaitron import NextTask, is_descoped, try_read_project
from caobunga.git import git_head, tree_after
from caobunga.invocation import InvocationTrace
from caobunga.launch import Posture, RunDeps
from caobunga.models import (
    CLOSURE_OUTCOMES,
    DRAIN_TIERS,
    Escalation,
    Outcome,
    RegistryRecord,
    RunRecord,
    WorktreeRun,
    compact_utc,
    utc_now,
)
from caobunga.presence import main_hold, park_hold
from caobunga.report import report
from caobunga.store import Store
from caobunga.suite import SuiteRun, echo_line
from caobunga.suite import run_suite as run_fleet_suite
from caobunga.taskmap import MODELS, resolve_effort
from caobunga.text import read_text_or_none
from caobunga.wait import SuiteHeartbeat, settle_worker
from caobunga.worker import Session, Worker
from caobunga.worktree import WorktreeContext


def refresh_tree(
    snap: CloseSnapshot,
    root: Path,
    task_id: str,
    *,
    baseline: Collection[str] | None = None,
    since: str | None = None,
) -> CloseSnapshot:
    """`snap` re-read once the worker has stopped.

    The trailing `tree:` line is swapped when this outcome folds one — always
    last, so the swap is the final ` — tree: ` segment. Any other summary is
    free text a worker wrote, which may contain that separator by chance
    (CBN-352). The same object comes back when nothing folded. `since` is the
    run's start sha, passed through to `tree_after`'s footprint split (CBN-452).

    A failure's `new_rows` is measured again here when `baseline` is passed
    (CBN-436). The first measure, in `launch._failed_from_worker`, lands before
    the settle grace, and a timeout's worker can still file during it. An
    unreadable PLAN keeps the first measure: losing a diff already taken is the
    worse trade. Omitting `baseline` leaves `new_rows` alone.
    """
    folds = snap.outcome in (Outcome.CLOSED, Outcome.DE_SCOPED) or snap.escalation in (
        Escalation.WORKER_EXITED,
        Escalation.TIMEOUT,
    )
    if folds:
        head, sep, _ = snap.summary.rpartition(" — tree: ")
        if sep:
            foreign = [commit.sha for commit in snap.foreign_commits or []]
            tree = tree_after(root, task_id, since=since, foreign=foreign)
            snap = snap.model_copy(update={"summary": f"{head} — {tree}"})
    if snap.outcome is not Outcome.FAILED or baseline is None:
        return snap
    project = try_read_project(root)
    if project is None:
        return snap
    rows = diff_new_rows(project.tasks, baseline)
    if rows == snap.new_rows:
        return snap
    return snap.model_copy(update={"new_rows": rows})


def settle_and_measure(
    client: Worker,
    session: Session,
    snap: CloseSnapshot,
    store: Store,
    root: Path,
    *,
    task_id: str,
    start_sha: str,
    posture: Posture,
    baseline: list[str],
    deps: RunDeps,
    started: float | None = None,
) -> tuple[CloseSnapshot, str | None, str | None]:
    """Settle, re-read, and measure a closure: `(snap, suite log, measured HEAD)`.

    The HEAD is read just before the suite starts, so an isolated landing can
    tell whether the worker moved it during the suite (CBN-EPIC-450.4); `None`
    when nothing was measured or git could not say. `started`, the close wait's
    start, carries the `progress:` heartbeat through all of it (CBN-EPIC-449.5).
    """
    with SuiteHeartbeat(deps.echo, started) as beat:
        # Close detection fires on the worker's commit, but the worker is
        # still running its own post-closure git work. Let it go quiet before
        # the report steps touch the same index (CBN-EPIC-9.5 F1).
        settle_worker(
            client, session, poll_interval=deps.poll_interval, sleep=deps.sleep, grace=deps.grace
        )
        # That work can amend the detected commit off-branch; re-read before the
        # suite so it measures, and the record names, the final tree (CBN-275).
        snap = reinspect_settled(
            root,
            task_id,
            start_sha,
            snap,
            unattended=posture.unattended_pick,
            drain=posture.drain_pick,
            baseline=baseline,
            retained=posture.retained,
        )
        # The deterministic suite (CBN-EPIC-202.3): the three fleet verbs
        # against the tree the worker just closed, on every launching tier
        # — only the *continue* is `proceed-on-green`'s (CBN-EPIC-202.4).
        # After the settle, so it measures a quiet tree; only on a closure
        # outcome, since a park or a failure left nothing closed to
        # measure and the record model forbids the field there. The result
        # rides the single `_write` below rather than a rewrite: `closed_at`
        # therefore lands after the suite, which is the accepted shift
        # (CBN-EPIC-202.1 D4) — nothing keys on it, and no pre-suite record
        # ever exists for the v2 validator to miss.
        if snap.outcome not in CLOSURE_OUTCOMES:
            return snap, None, None
        try:
            head = git_head(root)
        except GitUnreadable:
            head = None
        beat.stage = "suite"

        def on_verb(verb: str) -> None:
            beat.stage = f"suite {verb}"

        measured = measure_suite(root, store, deps, on_verb=on_verb)
        return snap.model_copy(update={"suite": measured.suite}), measured.log, head


def measure_suite(
    root: Path,
    store: Store,
    deps: RunDeps,
    *,
    on_verb: Callable[[str], None] | None = None,
) -> SuiteRun:
    """`deps.run_suite`, else the fleet suite with its `suite.lock` wait echo.

    The post-close measure and the pre-launch baseline (CBN-420) share it, so
    the two cannot disagree on what runs or how a wait is announced. `on_verb`
    reaches only the fleet suite: an injected `deps.run_suite` names no verb.
    """
    # A suite queued behind another slug's verb waits silently for up to the sum
    # of their ceilings while holding this run's lock, so say so (CBN-310).
    on_wait = _echo_suite_wait(deps.echo)
    fleet = partial(run_fleet_suite, on_wait=on_wait, on_verb=on_verb)
    return (deps.run_suite or fleet)(root, store)


def finish(
    store: Store,
    record: RegistryRecord,
    candidate: NextTask,
    snap: CloseSnapshot,
    *,
    provider: str | None,
    model: str | None,
    exhausted: list[str],
    marked: list[str],
    invocation: str,
    started_at: datetime,
    suite_log: str | None,
    deps: RunDeps,
    trace: InvocationTrace,
    worktree: WorktreeContext | None = None,
) -> RunRecord:
    echo = deps.echo
    state = trace.settle_worktree(snap.outcome, snap.suite) if worktree is not None else None
    written = _write(
        store,
        record,
        candidate,
        snap,
        provider=provider,
        model=model,
        exhausted=exhausted,
        marked=marked,
        invocation=invocation,
        started_at=started_at,
        worktree=state,
    )
    trace.wrote_run(written)
    _record_park(trace, record, written, worktree)
    _save_suite_log(store, written, suite_log, echo)
    _echo_foreign_commits(written, echo)
    _echo_filed_commits(snap, echo)
    parked = [park.handoff() for park in trace.result.parked]
    report(store, record, written, echo=echo, publish=deps.publish_status, parked=parked)
    return written


def _record_park(
    trace: InvocationTrace,
    record: RegistryRecord,
    run: RunRecord,
    worktree: WorktreeContext | None,
) -> None:
    """A drain's park and whether it holds the repo, decided before the card (CBN-443).

    Here rather than in the loop, so the parking run's own card already names
    the hold or the continue. The worker is stopped by now, so the tree is final.
    A park in an isolated worktree stays on its branch, retained, and holds only
    when main is not a tree to go on from (`main_hold`, CBN-EPIC-450.5); the
    next read denies the parked id `retained`.
    """
    if run.outcome is not Outcome.BLOCKED or record.tier not in DRAIN_TIERS:
        return
    if trace.start_sha is None:
        return
    root = worktree.execution_root if worktree is not None else Path(record.path)
    note = read_text_or_none(root / run.tasknote_path) if run.tasknote_path else None
    held = (
        main_hold(worktree.landing_root)
        if worktree is not None
        else park_hold(root, trace.start_sha)
    )
    trace.record_park(run, held, descoped=note is not None and is_descoped(note))


def _write(
    store: Store,
    record: RegistryRecord,
    candidate: NextTask,
    snap: CloseSnapshot,
    *,
    provider: str | None,
    model: str | None,
    exhausted: list[str],
    marked: list[str],
    invocation: str,
    started_at: datetime,
    worktree: WorktreeRun | None,
) -> RunRecord:
    # `marked` is the **pre-flight** read's marked set, not a fresh one: it is the
    # queue state this dispatch was decided from, the same read that produced
    # `candidate` and `eligibility_reason`. Recording it is what lets a later
    # `caobunga status` see that a cohort was disarmed since (CBN-125).
    run = RunRecord(
        project=record.id,
        task_id=candidate.task_id,
        invocation_id=invocation,
        outcome=snap.outcome,
        commit_sha=snap.commit_sha,
        started_at=started_at,
        closed_at=utc_now(),
        provider=provider,
        model=model,
        effort=resolve_effort(candidate.plan_model, provider) if provider in MODELS else None,
        plan_model=candidate.plan_model,
        next_task=snap.next_task,
        tasknote_path=snap.tasknote_path,
        summary=snap.summary,
        follow_ups=snap.follow_ups,
        new_rows=snap.new_rows,
        log_path=snap.log_path,
        escalation_reason=snap.escalation,
        park_reason=snap.park_reason,
        park_prose=snap.park_prose,
        eligibility_reason=candidate.eligibility_reason,
        marked_tasks=marked,
        foreign_commits=snap.foreign_commits,
        suite=snap.suite,
        external_review=snap.external_review,
        exhausted_providers=exhausted,
        worktree=worktree,
    )
    store.write_run(run)
    return run


def _save_suite_log(
    store: Store, run: RunRecord, log: str | None, echo: Callable[[str], None] | None
) -> None:
    """Persist the suite's rendered output under the run's own stamp, then echo the fold.

    After `_write`, because the stamp is `closed_at`, minted there — the log
    pairs with `runs/` by filename on the CBN-148 rule, no pointer field. One
    `suite:` line, echo-only like `foreign:`: the record already carries the
    verdict and every per-verb fact. Nothing to save when nothing ran — a park
    or a failure — and `run.suite` is `None` on exactly those.
    """
    if run.suite is None:
        return
    if log is not None:
        try:
            store.save_suite(run.project, compact_utc(run.closed_at), run.task_id, log)
        except OSError:
            pass
    if echo is not None:
        echo(echo_line(run.suite))


def _echo_suite_wait(echo: Callable[[str], None] | None) -> Callable[[], None] | None:
    """The `on_wait` that tells the operator a verb is queued on `suite.lock`.

    Echo-only like `suite:` and `foreign:`; `None` when there is no echo, so
    `Store.suite_lock` is never handed a no-op.
    """
    if echo is None:
        return None
    return lambda: echo("suite: waiting for suite.lock")


def _echo_foreign_commits(run: RunRecord, echo: Callable[[str], None] | None) -> None:
    """One `foreign: <sha> <author> <subject>` line per commit CBN-172 caught.

    Echo-only, the same shape `run_echo.echo_unparsed` uses: the record already carries
    the full list, so this is transcript information about the run, not a second
    copy of the data.
    """
    if echo is None or not run.foreign_commits:
        return
    for commit in run.foreign_commits:
        echo(f"foreign: {commit.sha} {commit.author} {commit.subject}")


def _echo_filed_commits(snap: CloseSnapshot, echo: Callable[[str], None] | None) -> None:
    """One `filed: <sha> <author> <subject>` line per commit CBN-299 kept off `foreign:`.

    Echo-only and snapshot-sourced: the record carries the rows themselves as
    `new_rows`, so the commit that filed them is transcript information.
    """
    if echo is None or not snap.filed_commits:
        return
    for commit in snap.filed_commits:
        echo(f"filed: {commit.sha} {commit.author} {commit.subject}")
