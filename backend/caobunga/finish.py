"""The finish: settle the worker, measure the closed tree, and write the run record.

`caobunga.finish` owns the record-producing half of a run, the helpers
`_run_gated` calls once a close is detected: the settle and suite measure
(`settle_and_measure`), the `tree:` refresh applied after `loop` retires the
session and re-reads the close (`refresh_tree`), and the record write with its
`suite:` / `foreign:` / `filed:` echoes and the `report` call (`finish`). A
leaf module: `close`, the flaitron façade, `git`, `launch`, `models`, `report`,
`store`, `suite`, `wait`, and `worker` cross in, and nothing calls back into
`loop.py`. Split out of `loop.py` at CBN-EPIC-363.4.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path

from caobunga.close import CloseSnapshot, reinspect_settled
from caobunga.flaitron import NextTask
from caobunga.git import tree_after
from caobunga.launch import Posture, RunDeps
from caobunga.models import (
    CLOSURE_OUTCOMES,
    Escalation,
    Outcome,
    RegistryRecord,
    RunRecord,
    compact_utc,
    utc_now,
)
from caobunga.report import report
from caobunga.store import Store
from caobunga.suite import echo_line
from caobunga.suite import run_suite as run_fleet_suite
from caobunga.wait import settle_worker
from caobunga.worker import Session, Worker


def refresh_tree(snap: CloseSnapshot, root: Path, task_id: str) -> CloseSnapshot:
    """`snap` with its trailing `tree:` line re-read; unchanged when it carries none.

    Only the outcomes that fold `tree_after` are touched — always last, so the
    swap is the final ` — tree: ` segment. Any other summary is free text a
    worker wrote, which may contain that separator by chance (CBN-352).
    """
    folds = snap.outcome in (Outcome.CLOSED, Outcome.DE_SCOPED) or snap.escalation in (
        Escalation.WORKER_EXITED,
        Escalation.TIMEOUT,
    )
    if not folds:
        return snap
    head, sep, _ = snap.summary.rpartition(" — tree: ")
    if not sep:
        return snap
    return snap.model_copy(update={"summary": f"{head} — {tree_after(root, task_id)}"})


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
) -> tuple[CloseSnapshot, str | None]:
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
        return snap, None
    # A suite queued behind another slug's verb waits silently for up to the sum
    # of their ceilings while holding this run's lock, so say so (CBN-310).
    on_wait = _echo_suite_wait(deps.echo)
    measured = (deps.run_suite or partial(run_fleet_suite, on_wait=on_wait))(root, store)
    return snap.model_copy(update={"suite": measured.suite}), measured.log


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
) -> RunRecord:
    echo = deps.echo
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
    )
    deps.trace.wrote_run(written)
    _save_suite_log(store, written, suite_log, echo)
    _echo_foreign_commits(written, echo)
    _echo_filed_commits(snap, echo)
    report(store, record, written, echo=echo, publish=deps.publish_status)
    return written


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
        plan_model=candidate.plan_model,
        next_task=snap.next_task,
        tasknote_path=snap.tasknote_path,
        summary=snap.summary,
        follow_ups=snap.follow_ups,
        new_rows=snap.new_rows,
        log_path=snap.log_path,
        escalation_reason=snap.escalation,
        park_reason=snap.park_reason,
        eligibility_reason=candidate.eligibility_reason,
        marked_tasks=marked,
        foreign_commits=snap.foreign_commits,
        suite=snap.suite,
        external_review=snap.external_review,
        exhausted_providers=exhausted,
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
        store.save_suite(run.project, compact_utc(run.closed_at), run.task_id, log)
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
