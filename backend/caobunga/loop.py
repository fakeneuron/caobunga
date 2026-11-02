"""Observe / gated / later-tier run loop.

Composes store, Flaitron reader, the local worker, the three leaves split out at
CBN-EPIC-232 — `prompt` (what the worker is told), `wait` (until its task
closes), `report` (card and rollup after the close) — `launch` (pick, launch,
and the usage-limit re-pick), split out at CBN-EPIC-363.3, and `finish` (settle,
suite measure, and the run-record write), split out at CBN-EPIC-363.4. The
observe, eligibility, and exit-detail text lives in `run_echo`, split out at
CBN-EPIC-363.5.
"""

from __future__ import annotations

import contextlib
import math
import os
import shlex
import signal
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from caobunga import landing
from caobunga.bootstrap import refuse_unready, run_bootstrap
from caobunga.close import CloseSnapshot, reinspect_settled
from caobunga.errors import (
    GitUnreadable,
    HeadLost,
    NoTaskError,
    ReceiptUnwritten,
    ReportError,
    Unlaunchable,
    UserError,
)
from caobunga.finish import finish, measure_suite, refresh_tree, settle_and_measure
from caobunga.flaitron import (
    NextTask,
    ProjectRead,
    completed_task_ids,
    eligibility,
    list_open_tasknotes,
    read_project,
)
from caobunga.git import dirt_sample, git_head, git_porcelain, main_worktree
from caobunga.invocation import InvocationTrace, closed_green, reconcile_stale
from caobunga.launch import (
    Attempt,
    RunDeps,
    attribution,
    launch_posture,
    launch_until_close,
    retire_session,
    shutdown_session,
)
from caobunga.models import (
    CLOSURE_OUTCOMES,
    DRAIN_TIERS,
    LAUNCHING_TIERS,
    Escalation,
    ExecutionTier,
    InvocationResult,
    InvocationStatus,
    Outcome,
    RegistryRecord,
    RunRecord,
    StopReason,
    SuiteVerdict,
    utc_now,
)
from caobunga.presence import tree_state
from caobunga.recovery import cleanup_recovery
from caobunga.retained import drain_retained, report_retained
from caobunga.run_echo import (
    denial_codes,
    echo_eligibility,
    echo_observe,
    echo_unparsed,
    no_task_message,
    non_green_verbs,
    outcome_exit_detail,
    run_tally,
    suite_exit_detail,
    unpinned_exit_detail,
)
from caobunga.store import Store
from caobunga.suite import SuiteRun, echo_line, suite_stops
from caobunga.suite_verbs import JUST_MISSING
from caobunga.usage import ProviderUsage
from caobunga.wait import DEFAULT_POLL, SETTLE_GRACE, SuiteHeartbeat
from caobunga.worker import Worker, select_worker
from caobunga.worktree import BRANCH_PREFIX, WorktreeContext
from caobunga.worktree import start as start_worktree

ENV_TIMEOUT = "CAOBUNGA_RUN_TIMEOUT"
DEFAULT_TIMEOUT = 7200.0


def run_timeout() -> float:
    raw = os.environ.get(ENV_TIMEOUT)
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError as exc:
        raise UserError(f"invalid {ENV_TIMEOUT}={raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise UserError(f"invalid {ENV_TIMEOUT}={raw!r}")
    return value


def execute_run(
    project_id: str,
    *,
    store: Store | None = None,
    worker: Worker | None = None,
    poll_interval: float = DEFAULT_POLL,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] | None = None,
    usage: Sequence[ProviderUsage] | None = None,
    publish_status: Callable[[Store, RunRecord], Path] | None = None,
    on_run: Callable[[RunRecord], None] | None = None,
    run_suite: Callable[[Path, Store], SuiteRun] | None = None,
    grace: float = SETTLE_GRACE,
) -> RunRecord | None:
    """Compatibility adapter: last task record or the original direct-run exception."""
    deps = RunDeps(
        worker=worker,
        poll_interval=poll_interval,
        timeout=timeout,
        sleep=sleep,
        echo=echo,
        usage=usage,
        publish_status=publish_status,
        on_run=on_run,
        run_suite=run_suite,
        grace=grace,
    )
    _, last, error = _drive_invocation(project_id, store, deps)
    if error is not None:
        raise error
    return last


def execute_invocation(
    project_id: str,
    *,
    store: Store | None = None,
    worker: Worker | None = None,
    poll_interval: float = DEFAULT_POLL,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] | None = None,
    usage: Sequence[ProviderUsage] | None = None,
    publish_status: Callable[[Store, RunRecord], Path] | None = None,
    on_run: Callable[[RunRecord], None] | None = None,
    run_suite: Callable[[Path, Store], SuiteRun] | None = None,
    grace: float = SETTLE_GRACE,
) -> InvocationResult:
    """Return a durable receipt, including refusals and incomplete approved work.

    Raises `ReceiptUnwritten` when the receipt itself cannot be written (CBN-442).
    """
    deps = RunDeps(
        worker=worker,
        poll_interval=poll_interval,
        timeout=timeout,
        sleep=sleep,
        echo=echo,
        usage=usage,
        publish_status=publish_status,
        on_run=on_run,
        run_suite=run_suite,
        grace=grace,
    )
    result, _, _ = _drive_invocation(project_id, store, deps)
    return result


FORCE_QUIT_PRESSES = 3
# A closed terminal (SIGHUP) or a `kill` (SIGTERM) unwinds as the first ^C does (CBN-430).
INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)
INTERRUPT_NOTICE = (
    "interrupt received — stopping the worker and writing the record (~10 s); "
    "^C twice more to force-quit"
)


@dataclass
class _InterruptGuard:
    """Press count for one invocation; `finalizing` once the run itself has ended.

    `signal_name` names the first signal caught, for the receipt's `detail`.
    """

    presses: int = 0
    finalizing: bool = False
    signal_name: str | None = None


@contextlib.contextmanager
def _interrupt_guard() -> Iterator[_InterruptGuard]:
    """Count ^C presses for one invocation so a repeat cannot skip the receipt (CBN-375).

    The first press raises `KeyboardInterrupt` as the default handler does, and
    the unwind stops the worker and writes the `interrupted` receipt. Presses
    during that cleanup re-print and are absorbed, as is a first press once the
    run has ended and only the receipt write is left; the third raises again to
    force the exit. A SIGHUP or SIGTERM is a first press too (CBN-430), but a
    later one is only absorbed, never counted: a closed terminal can deliver
    SIGHUP more than once, and a `fleet` child also gets the fleet's forwarded
    SIGTERM, so counting them would force-quit past the receipt with no ^C at
    all. Signal handlers install only on the main thread, and never over
    an inherited `SIG_IGN`, so any other caller keeps its disposition. The
    notice is a raw write to fd 2: a buffered write from inside a handler that
    interrupted one raises, and on a hung-up terminal it fails quietly.
    """
    guard = _InterruptGuard()
    if threading.current_thread() is not threading.main_thread():
        yield guard
        return
    caught = [sig for sig in INTERRUPT_SIGNALS if signal.getsignal(sig) is not signal.SIG_IGN]

    def handler(signum: int, _frame: object) -> None:
        if guard.presses and signum != signal.SIGINT:
            return
        guard.presses += 1
        if guard.signal_name is None:
            guard.signal_name = signal.Signals(signum).name
        if guard.presses >= FORCE_QUIT_PRESSES:
            raise KeyboardInterrupt
        if guard.presses == 1:
            _say(INTERRUPT_NOTICE)
            if not guard.finalizing:
                raise KeyboardInterrupt
            return
        _say("interrupt received again — still writing the record; ^C once more to force-quit")

    previous = {sig: signal.signal(sig, handler) for sig in caught}
    try:
        yield guard
    finally:
        for sig, before in previous.items():
            # `None`: a handler not installed from Python; SIGINT's is the default one.
            if before is None:
                before = signal.default_int_handler if sig is signal.SIGINT else signal.SIG_DFL
            signal.signal(sig, before)


def _say(line: str) -> None:
    with contextlib.suppress(OSError):
        os.write(2, f"{line}\n".encode())


def _drive_invocation(
    project_id: str, store: Store | None, deps: RunDeps
) -> tuple[InvocationResult, RunRecord | None, BaseException | None]:
    store = store or Store()
    echo = deps.echo
    with _interrupt_guard() as guard:
        trace = InvocationTrace(project_id, store)
        error: BaseException | None = None
        exit_code = 0
        try:
            _execute_run(project_id, store, deps, trace=trace)
        except KeyboardInterrupt as exc:
            error = exc
            exit_code = 130
            trace.result.status = InvocationStatus.INTERRUPTED
            trace.result.stop_reason = StopReason.INTERRUPTED
            by = f" ({guard.signal_name})" if guard.signal_name is not None else ""
            trace.result.detail = (
                f"Invocation interrupted{by}; closure and readiness require reconciliation."
            )
        except Exception as exc:  # noqa: BLE001 — receipt boundary accounts for unexpected failures
            error = exc
            exit_code = exc.exit_code if isinstance(exc, UserError) else 4
            _record_invocation_error(trace, exc)
        guard.finalizing = True
        if error is None and trace.last is not None and trace.last.outcome is Outcome.FAILED:
            exit_code = 4
        try:
            result = trace.finish(exit_code=exit_code)
        except OSError as exc:
            # Outside the boundary above: a full disk here was a raw traceback
            # behind a `running` checkpoint, or no receipt at all (CBN-442).
            raise ReceiptUnwritten(
                exc.strerror or str(exc), stopped=error, record=trace.last
            ) from exc
        if echo is not None and result.tier is not ExecutionTier.OBSERVE:
            # The bare `outcome` code names no cause; the receipt's detail does (CBN-287).
            cause = (
                f": {' '.join(result.detail.split())}"
                if result.stop_reason == StopReason.OUTCOME
                else ""
            )
            # `last` alone hides what the drain's earlier tasks did (CBN-295).
            ran = f"; ran {len(trace.runs)}: {run_tally(trace.runs)}" if len(trace.runs) > 1 else ""
            echo(
                f"invocation: {result.status.value} — {result.stop_reason}{cause}{ran}; "
                f"successful={str(result.successful).lower()}"
            )
            echo(f"invocation-record: {store.invocation_file(result.receipt_id)}")
            for park in result.parked:
                echo(f"parked: {park.handoff()}")
            for step in result.recovery:
                echo(f"recovery: {step}")
    return result, trace.last, error


def _record_invocation_error(trace: InvocationTrace, exc: Exception) -> None:
    result = trace.result
    result.detail = str(exc)
    if isinstance(exc, NoTaskError):
        result.stop_reason = (
            StopReason.DENIED if result.denials or result.unparsed_lines else StopReason.EMPTY
        )
        result.status = (
            InvocationStatus.INCOMPLETE
            if result.authorized_tasks or result.unparsed_lines
            else InvocationStatus.EMPTY
        )
    else:
        result.status = (
            InvocationStatus.INCOMPLETE if result.run_records else InvocationStatus.REFUSED
        )
        if isinstance(exc, GitUnreadable):
            result.stop_reason = StopReason.GIT_UNREADABLE
        elif isinstance(exc, HeadLost):
            result.stop_reason = StopReason.HEAD_LOST
        elif isinstance(exc, ReportError):
            result.stop_reason = StopReason.REPORT
        elif not isinstance(exc, UserError):
            result.stop_reason = StopReason.ORCHESTRATOR_ERROR


def _with_default_providers(record: RegistryRecord, store: Store) -> RegistryRecord:
    """Fill a record with no pin and no allow-list from `config.yaml` (CBN-414).

    Resolved here, not in `Store.get`: a resolved record must never reach
    `register`'s merge and be written back as the record's own list.
    """
    if record.provider is not None or record.providers is not None:
        return record
    return record.model_copy(update={"providers": store.load_config().providers})


def _execute_run(
    project_id: str,
    store: Store,
    deps: RunDeps,
    *,
    trace: InvocationTrace,
) -> RunRecord | None:
    """Honor the project's tier. Observe returns None; other tiers write run records."""
    echo = deps.echo
    trace.result.stop_reason = StopReason.REGISTRY
    record = store.get(project_id)
    trace.register(record)
    if not record.enabled:
        trace.result.stop_reason = StopReason.DISABLED
        raise UserError(f"project {project_id} is disabled; not launching")
    # Held for the rest of the function — every tier, observe included — so a
    # second concurrent `caobunga run` on the same repo — under this slug or any
    # other registered at the same path, anywhere else in the same checkout, or
    # in any linked worktree of it — exits 2 instead of racing a live worker or
    # a landing (CBN-EPIC-16.2, CBN-EPIC-313.2, CBN-EPIC-450.2). After the
    # registry validation above so unknown/disabled projects keep their own messages.
    trace.checkpoint()
    trace.result.stop_reason = StopReason.ACTIVE_RUN
    root = Path(record.path)
    with store.run_lock(record.id, main_worktree(root) or root):
        if record.tier is ExecutionTier.OBSERVE:
            trace.result.stop_reason = StopReason.OBSERVE
            _observe(record, root, store, echo)
            trace.result.status = InvocationStatus.OBSERVED
            return None
        # Under the lock, so a dead run's receipt is told from a live one (CBN-376).
        reconcile_stale(store, record, trace.result.receipt_id, echo)
        # Past the disabled and observe exits, so a bad `config.yaml` only stops
        # a run that would pick a provider.
        trace.result.stop_reason = StopReason.REGISTRY
        record = _with_default_providers(record, store)

        # The receipt and each task record share one invocation key. Observe
        # leaves the key in memory and writes no receipt or task record.
        invocation = trace.result.invocation_id
        last: RunRecord | None = None
        first = True
        trace.result.stop_reason = StopReason.TIMEOUT_CONFIG
        timeout_s = deps.timeout if deps.timeout is not None else run_timeout()
        trace.result.timeout_seconds = timeout_s
        deps = replace(deps, timeout=timeout_s)
        unattended_pick = record.tier in LAUNCHING_TIERS
        drain_pick = record.tier in DRAIN_TIERS
        pinned: frozenset[str] = frozenset()
        while True:
            retained = _read_retained(record, trace)
            trace.result.stop_reason = StopReason.PROJECT_READ
            read = _read_project_or_stop(
                root, unattended=unattended_pick, drain=drain_pick, retained=retained
            )
            candidate = read.candidate
            if first:
                pinned = _marked_ids(read, retained)
            trace.saw_read(read, pinned, first=first)
            trace.checkpoint()
            if unattended_pick:
                echo_eligibility(read, echo)
                echo_unparsed(read, echo)
            stop = _pre_dispatch_stop(project_id, read, pinned, first=first)
            if stop is not None:
                assert last is not None  # noqa: S101
                _record_pre_stop(trace, stop, candidate)
                _echo_drain_exit(*stop, echo)
                return last
            assert candidate is not None  # noqa: S101 — `_pre_dispatch_stop` stops on None
            written = _run_gated(
                store,
                record,
                candidate,
                marked=read.coverage.marked_tasks,
                # Every parsed id, not just the marked or the open ones: this is
                # the "what did this PLAN hold when we launched" baseline the
                # close diffs against (CBN-138.3), a different question from the
                # marked set beside it.
                baseline=[task.task_id for task in read.tasks],
                invocation=invocation,
                measure_baseline=first,
                deps=deps,
                trace=trace,
                retained=retained,
            )
            if deps.on_run is not None:
                deps.on_run(written)
            last = written
            first = False
            if record.tier is ExecutionTier.GATED:
                # One task is the whole scope: any committed green close finishes it (CBN-301).
                _record_gated_stop(
                    trace,
                    _landing_stop(last, trace) if closed_green(last) else _post_run_stop(last),
                )
                return last
            stop = _drain_stop(last, trace, echo)
            if stop is not None:
                _record_gated_stop(trace, stop)
                trace.result.status = InvocationStatus.INCOMPLETE
                _echo_drain_exit(*stop, echo)
                return last


def _observe(
    record: RegistryRecord, root: Path, store: Store, echo: Callable[[str], None] | None
) -> None:
    """The observe dump. A report, so an unreadable leftover read degrades (CBN-EPIC-450.6)."""
    retained = report_retained(record)
    if retained is None and echo is not None:
        echo("retained: unreadable — git could not list wt- leftovers; a launch refuses")
    seen = retained or frozenset()
    echo_observe(record, _read_project_or_stop(root, retained=seen), store, echo, seen)


def _read_retained(record: RegistryRecord, trace: InvocationTrace) -> frozenset[str]:
    """The ids an isolated project's `wt-<ID>` leftovers deny, read fresh each pick.

    Empty for a project that does not isolate (CBN-EPIC-450.5 scope). Before
    each read the previous task's still-retained worktree moves onto the
    receipt's recovery list, so a later task's worktree cannot displace it.
    """
    trace.retire_worktree()
    if record.isolate:
        trace.result.stop_reason = StopReason.WORKTREE
    return drain_retained(record)


def _read_project_or_stop(
    root: Path,
    *,
    unattended: bool = False,
    drain: bool = False,
    retained: frozenset[str] = frozenset(),
) -> ProjectRead:
    """`read_project` for the direct reads outside `wait.poll`: an `OSError` is a named stop.

    Here nothing treats it as transient (CBN-168), so left raw it would reach the
    receipt boundary as `orchestrator-error` and traceback the CLI; a `UserError`
    keeps the `observe` / `project-read` stage and exits 2 like a missing PLAN.
    """
    try:
        return read_project(root, unattended=unattended, drain=drain, retained=retained)
    except OSError as exc:
        raise UserError(f"cannot read the Flaitron project at {root}: {exc}") from exc


def _record_pre_stop(
    trace: InvocationTrace, stop: tuple[StopReason, str], candidate: NextTask | None
) -> None:
    trace.result.stop_reason, trace.result.detail = stop
    if candidate is not None:
        trace.result.task_id = candidate.task_id
        trace.result.status = InvocationStatus.INCOMPLETE


def _record_gated_stop(trace: InvocationTrace, stop: tuple[StopReason, str] | None) -> None:
    trace.result.stop_reason, trace.result.detail = stop or (
        StopReason.GATED,
        "One-task scope finished.",
    )
    if stop is not None:
        trace.result.status = InvocationStatus.INCOMPLETE
        # A committed run's summary is already in the `run:` dump (CBN-302).
        if trace.last is not None and stop[0] == "outcome" and not trace.last.commit_sha:
            trace.result.detail += f" — {trace.last.summary}"


def _refuse_if_dirty(store: Store, project_id: str, root: Path, candidate: NextTask) -> None:
    """Refuse to dispatch into a tree the operator is standing in (CBN-EPIC-17.3).

    Every launching tier, `gated` included: Caobunga always launches a *headless*
    worker, so "the operator is at the orchestrator terminal" does not mean
    anybody is inside the target repo. On a fresh launch this is the cheaper,
    earlier form of a stop flaitron's own foreign-dirt gate would make anyway; on
    the continue path (CBN-109) it is the only stop that exists, because that
    prompt tells the worker not to re-invoke `/ft-task` and so never reaches the
    skill pre-flight where flaitron's gate lives.

    Exit 2, alongside the run mutex and `not a git repository` — the task is
    eligible and the repo is not ready, which is neither exit 3's "nothing
    eligible" nor a task run worth recording. Like the mutex, it refuses before
    a task record is written; the invocation receipt accounts for the refusal.

    The verdict comes from `presence.tree_state` rather than a local dirt read
    (CBN-130), so `caobunga status` renders the same answer this refuses on. The
    dirt-message, the exit code, and this call's placement are unchanged. A `git`
    that hung or is missing raises `GitUnreadable` out of that read (exit 5,
    CBN-317); only a nonzero `git status` is the exit-2 `cannot read git status`.

    The open-note list is re-read here rather than threaded down from the pick.
    The git read is fresh by definition, and the exemption is a claim about the
    tree the two are compared against; taking one half from a parse that happened
    before the previous task in a drain closed would be the staler answer.

    **A filing gets its own message, never its own exemption (CBN-138.4).** Since
    CBN-138.2 the prompt asks a worker to file what it discovers, so the dirt this
    refuses on is now routinely something Caobunga itself asked for — and it read
    identically to an operator's half-edited source tree, which is the gap
    CBN-138.1 F3 named: the cause was unnamed, not that it blocked. `filing_only`
    is all-or-nothing, so one line of operator dirt returns the generic message
    verbatim and every CBN-EPIC-17.3 assertion holds unedited. What changes is the
    prose and the command it offers; the exit code, the placement, and the set of
    lines that block are untouched. Caobunga prints that command and never runs it
    — cleaning a target tree is as much the operator's act as seeding a marker
    (§"Marker coverage", read-only in both directions).

    **Only for a filing the last run owns (CBN-434).** A tasknote or stub counts
    when its id is that run's `task_id` or one of its `new_rows`. A live
    session's in-flight note (CBN-429 F1) gets the generic message, so the
    offered commit never sweeps up another session's work. The record is read
    here, fresh, for the same reason the open-note list is. A PLAN edit counts
    only after a run that recorded `new_rows` (CBN-435).

    **The command names the blocking paths, on both `add` and `commit` (CBN-435).**
    A directory-scoped `git add .flaitron` also staged the exempt candidate note,
    and a bare `commit` would carry anything already in the index.
    """
    last_run = store.last_run(project_id)
    notes = list_open_tasknotes(root)
    state = tree_state(root, candidate, notes, last_run=last_run)
    if not state.readable:
        raise UserError(f"{project_id}: cannot read git status in {root}; not launching")
    if not state.blocking:
        return
    blocking = state.blocking
    # The whole list: the recovery step says "inspect the listed changes" (CBN-287).
    shown = "; ".join(entry.text.strip() for entry in blocking)
    if state.filing_only:
        paths = " ".join(shlex.quote(entry.path) for entry in blocking if entry.path)
        repo = shlex.quote(str(root))
        # A park the worker left uncommitted reads as a filing too (CBN-443).
        parked = {note.task_id for note in notes if note.status == "blocked"}
        resume = "".join(
            f" — {task_id} is parked; resume it attended with /ft-task {task_id}"
            for task_id in sorted(parked.intersection(entry.task_id for entry in blocking))
        )
        raise UserError(
            f"{project_id}: target repo has {len(blocking)} uncommitted change(s), "
            f"all of them flaitron filing artifacts a dispatched worker writes; "
            f"if a worker filed this, commit it: "
            f"git -C {repo} add -- {paths} && git -C {repo} commit -m "
            f'"chore: commit worker filing" -- {paths} — then re-run: {shown}{resume}'
        )
    # Dirt that is an open tasknote is a run's partial work: name it and the resume.
    resume = "".join(
        f" — open tasknote {task_id} holds partial work; resume it attended with /ft-task {task_id}"
        for task_id in sorted({entry.task_id for entry in blocking if entry.task_id})
    )
    raise UserError(
        f"{project_id}: target repo has {len(blocking)} uncommitted change(s); "
        f"commit, stash, or discard them, then re-run: {shown}{resume}"
    )


def _ready_head(
    store: Store,
    record: RegistryRecord,
    root: Path,
    candidate: NextTask,
    deps: RunDeps,
    trace: InvocationTrace,
) -> str:
    """The launch's `start_sha`, once the target is a repo with a clean tree.

    Read again after the baseline suite (CBN-420), whose minutes are long enough
    for HEAD to move or the suite's own output to dirty the tree.
    """
    trace.result.stop_reason = StopReason.GIT_UNREADABLE
    start_sha = git_head(root)
    if start_sha is None:
        # git answered: no repo, or an unborn HEAD — not an unreadable git (CBN-321).
        trace.result.stop_reason = StopReason.NOT_A_REPO
        raise UserError(f"not a git repository: {root}")
    trace.start_sha = start_sha

    # Operator-presence boundary (CBN-EPIC-17.3). After the git-repo check, so
    # the dirt read is never the one reporting "not a repository"; before
    # the pick, so a dirty tree refuses for the more specific reason.
    trace.result.stop_reason = StopReason.DIRTY_TREE
    try:
        _refuse_if_dirty(store, record.id, root, candidate)
    except UserError as exc:
        # The pick echoed `allow` before this read (CBN-288); say it no longer stands.
        # A hung or missing `git` is not dirt (CBN-317), so it names its own cause.
        cause = "git-unreadable" if isinstance(exc, GitUnreadable) else "dirty-tree"
        if deps.echo is not None and candidate.eligibility_reason is not None:
            deps.echo(f"eligibility: {candidate.task_id} allow withdrawn: {cause} — not launching")
        raise
    return start_sha


def _refuse_if_red_baseline(
    store: Store,
    record: RegistryRecord,
    root: Path,
    candidate: NextTask,
    *,
    invocation: str,
    deps: RunDeps,
    trace: InvocationTrace,
) -> None:
    """Refuse to dispatch onto a suite that is already failing (CBN-420).

    Five workers (CBN-377, CBN-399, and three tasks in a fleet repo) each spent a
    session learning that the red they parked `input-needed` on predated their
    diff — a flaitron bump, a `.env` bleed — that operator commits had landed
    between invocations. The last closure's suite was green every time, so no
    recorded verdict could stand in for this run; ~90 s of suite is the price
    of not spending a worker session on it.

    Once per invocation, before the first dispatch: a drain's later dispatches
    start from a tree the post-close suite already measured green, or the drain
    would have stopped. Same verbs, same runner, same `suite_stops` threshold
    as that post-close gate, so `timeout` and `refused` refuse too. Like the
    dirty-tree refusal it writes no task record and exits 2, which `fleet`
    folds as that repo's `refused` without touching another repo's run — except
    a missing `just`, which is the machine's state and exits 5.
    """
    measured = measure_suite(root, store, deps)
    if deps.echo is not None:
        deps.echo(echo_line(measured.suite, label="baseline"))
    verdict = measured.suite.verdict
    if verdict is SuiteVerdict.GREEN:
        return
    try:
        log = store.save_suite(record.id, invocation, f"{candidate.task_id}-baseline", measured.log)
    except OSError as exc:
        where = f"suite log not saved: {exc}"
    else:
        trace.result.suite_log = str(log)
        where = f"suite log: {log}"
    if deps.echo is not None and candidate.eligibility_reason is not None:
        deps.echo(
            f"eligibility: {candidate.task_id} allow withdrawn: baseline-{verdict.value} "
            "— not launching"
        )
    if any(verb.refused == JUST_MISSING for verb in measured.suite.verbs):
        # No repo's suite can run on this machine: its state, not a per-repo skip
        # `fleet` would fold green (CBN-EPIC-313.5).
        raise Unlaunchable(
            f"{record.id}: {JUST_MISSING}, so the suite baseline cannot run; "
            f"install just, then re-run. {where}"
        )
    raise UserError(
        f"{record.id}: the suite baseline is {verdict.value} on the clean tree before "
        f"launch ({non_green_verbs(measured.suite)}); not dispatching {candidate.task_id} "
        f"onto a failure it did not cause — fix it to green, then re-run. {where}"
    )


def _registry_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Home-relative registry names added, removed, or re-hashed between two digests."""
    return sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )


def _flag_registry_change(
    snap: CloseSnapshot, before: dict[str, str], after: dict[str, str]
) -> CloseSnapshot:
    """`snap` escalated `registry-changed` when the registry moved while the worker ran (CBN-424).

    The post-hoc half of CBN-247: that hook stops a `claude_code` worker's
    `caobunga register` and file-tool writes, but not a Bash shell write, and
    `grok_cli` / `codex` / `cursor_cli` carry no guard at all. A digest cannot
    tell a worker's write from the operator's own `register` in another
    terminal, so the record says the registry changed, not who changed it. The
    worker's commit stays on the record; the suite goes, since the record model
    forbids it off a closure outcome.
    """
    changed = _registry_changes(before, after)
    if not changed:
        return snap
    return snap.model_copy(
        update={
            "outcome": Outcome.FAILED,
            "escalation": Escalation.REGISTRY_CHANGED,
            "summary": f"registry changed during run: {', '.join(changed)} — {snap.summary}",
            "suite": None,
        }
    )


def _echo_drain_exit(reason: StopReason, detail: str, echo: Callable[[str], None] | None) -> None:
    """State why a drain ended, at every drain-ending return (CBN-119.4).

    `CBN-EPIC-18.N` Finding 5 carried forward that a drain does not discriminate
    an **exhaustion exit** (nothing eligible remains) from a **marker exit** (the
    walk stopped because the next row was unmarked). Both end the loop with no
    candidate, no error, and — since `CBN-117` reads `next_task` at the run's own
    posture — no `next_task` on the last record either, so the two were
    indistinguishable on every surface. Discriminating them needs no trial: at
    the exit the loop already holds the denial list, and whether it is empty is
    exactly the difference.

    Task-record fields stay unchanged. The invocation receipt also records
    this reason (CBN-272), so callers need not scrape terminal output to learn
    why the invocation stopped.

    `gated` returns a single task and is not a drain, so it echoes nothing — the
    vocabulary stays meaningful by describing only walks that ended.
    """
    if echo is None:
        return
    echo(f"drain: {reason} — {detail}")


def _pre_dispatch_stop(
    project_id: str, read: ProjectRead, pinned: frozenset[str], *, first: bool
) -> tuple[StopReason, str] | None:
    """The drain exit this read ends on before dispatch, or None to dispatch.

    An empty first read is not a drain exit but exit 3, so it raises instead.
    """
    candidate = read.candidate
    if candidate is None:
        if first:
            raise NoTaskError(no_task_message(project_id, read.denied, read.coverage.unparsed_rows))
        return _no_candidate_exit(read)
    if not first and candidate.task_id not in pinned:
        # Marked after this drain's first read (CBN-246). The worker that
        # just closed wrote this PLAN, so dispatching here would let a
        # worker arm rows for itself; the operator's re-run dispatches it.
        return StopReason.UNPINNED, unpinned_exit_detail(candidate)
    return None


def _post_run_stop(last: RunRecord) -> tuple[StopReason, str] | None:
    """The drain exit this run's record ends on, or None to read again."""
    if last.outcome in CLOSURE_OUTCOMES and suite_stops(last):
        # A closure whose suite did not pass (CBN-EPIC-202.4); any closure outcome,
        # so a red de-scoped / no-tasknote close names the suite fix (CBN-304).
        return StopReason.SUITE, suite_exit_detail(last)
    if last.outcome is not Outcome.CLOSED:
        return StopReason.OUTCOME, outcome_exit_detail(last)
    return None


def _drain_stop(
    last: RunRecord,
    trace: InvocationTrace,
    echo: Callable[[str], None] | None,
) -> tuple[StopReason, str] | None:
    """`_post_run_stop`, except a drain continues past a clean park (CBN-443).

    No new eligibility rule: the re-read denies the parked row `parked`, any row
    `Blocked by` it `blocked`, and its epic's audits `audit`, so the drain
    continues only to rows the park does not gate. Anything else holds the repo.
    The hold itself is decided in `finish`, before the card is written; a park
    with no recorded decision stops as before.
    """
    stop = _post_run_stop(last) or _landing_stop(last, trace)
    if stop is None or last.outcome is not Outcome.BLOCKED:
        return stop
    park = trace.result.parked[-1] if trace.result.parked else None
    if park is None or park.task_id != last.task_id:
        return stop
    if park.held is not None:
        return stop[0], f"{stop[1]} — repo held: {park.held}"
    if echo is not None and trace.worktree is not None:
        echo(f"park: {last.task_id} retained on {trace.worktree.branch} — continuing the drain")
    elif echo is not None:
        echo(f"park: {last.task_id} committed with a clean tree — continuing the drain")
    return None


def _landing_stop(last: RunRecord, trace: InvocationTrace) -> tuple[StopReason, str] | None:
    """The stop for a green isolated closure main did not get (CBN-EPIC-450.4).

    Its record is a green closure, so nothing else may stop a gated run on it,
    but main does not hold it: a drain's re-read would pick the same row again.
    """
    if trace.unlanded is None:
        return None
    return StopReason.LANDING, f"{last.task_id} {last.outcome} close not landed: {trace.unlanded}"


def _no_candidate_exit(read: ProjectRead) -> tuple[StopReason, str]:
    """Classify the no-eligible-candidate exit into `complete` / `incomplete`.

    A drain has no cohort: any denial is a row the drain would have taken, so the
    scope is the whole queue.
    """
    if read.denied:
        return (
            StopReason.INCOMPLETE,
            f"no eligible candidate; {len(read.denied)} denied: {denial_codes(read.denied)}",
        )
    return StopReason.COMPLETE, "no eligible candidate remains"


def _marked_ids(read: ProjectRead, retained: frozenset[str] = frozenset()) -> frozenset[str]:
    """Ids the first read would dispatch, `Blocked by` aside — the drain's pin (CBN-246).

    Not `coverage.marked_tasks`: that is the *eligible* set, and a marked row
    whose `Blocked by` clears mid-drain is a legitimate continue. Not the bare
    marker either (CBN-260): a marked row denied `handoff` / `exploratory` /
    `frontier` / `parked` at first read was pinned, so a worker that stripped the denying
    token armed it with nobody asked. The clause is emptied and everything else
    goes through `eligibility` — the one predicate — so a row blocked *and*
    `[handoff]` stays out. `open_ids` is empty for the same reason the clause
    is emptied (CBN-309): a marked `.N` whose siblings close mid-drain is a
    legitimate continue, so the `audit` dimension must not keep it out of the pin.
    A row `retained` at first read stays out too (CBN-EPIC-450.5): a worker in a
    linked worktree shares the repo's refs, so it could delete that branch.
    """
    done = completed_task_ids(read.tasks)
    by_id = {note.task_id: note for note in read.open_tasknotes}
    return frozenset(
        task.task_id
        for task in read.tasks
        if task.unattended
        and eligibility(
            task.model_copy(update={"blocked_by": []}),
            unattended=True,
            completed_ids=done,
            note=by_id.get(task.task_id),
            open_ids=(),
            retained=retained,
        )[0]
    )


def _ready_worktree(
    store: Store,
    record: RegistryRecord,
    candidate: NextTask,
    start_sha: str,
    *,
    invocation: str,
    deps: RunDeps,
    trace: InvocationTrace,
) -> WorktreeContext:
    """Start `wt-<ID>` at `start_sha`, bootstrap it, and refuse unless it is ready.

    The readiness order of `bootstrap.py` (CBN-EPIC-450.3), up to the baseline,
    which the caller runs on the execution root. From the moment the worktree
    exists it is on the trace, so every later stop names it as retained.
    """
    trace.result.stop_reason = StopReason.WORKTREE
    registry_root = Path(record.path)
    try:
        _refuse_uncarried(record.id, registry_root)
        trace.landing_ref = _landing_branch(record.id, registry_root)
        context = start_worktree(registry_root, candidate.task_id, start_sha)
        trace.start_worktree(context)
        if deps.echo is not None:
            deps.echo(f"worktree: {context.branch} at {context.worker_root}")
        result = run_bootstrap(context, record)
        where = ""
        if result is not None:
            if deps.echo is not None:
                deps.echo(f"bootstrap: {result.failure or 'ok'} ({result.duration:.1f}s)")
            if not result.ok:
                where = _save_bootstrap_log(store, record, candidate, invocation, result.render())
        try:
            refuse_unready(context, result)
        except UserError as exc:
            if type(exc) is not UserError or not where:
                raise
            raise UserError(f"{exc} {where}") from exc
    except UserError as exc:
        if deps.echo is not None and candidate.eligibility_reason is not None:
            cause = "git-unreadable" if isinstance(exc, GitUnreadable) else "worktree"
            deps.echo(f"eligibility: {candidate.task_id} allow withdrawn: {cause} — not launching")
        raise
    return context


def _landing_branch(project_id: str, root: Path) -> str:
    """The branch main has checked out, refusing a detached main (CBN-EPIC-450.4).

    A fast-forward advances whatever main has checked out, so a detached main
    could never land: refuse before a worker spends a session.
    """
    ref = landing.checked_out(root)
    if ref is None:
        raise UserError(
            f"{project_id}: the main checkout is not on a branch, so an isolated close "
            "could not land; check out a branch, then re-run"
        )
    return ref


def _refuse_uncarried(project_id: str, root: Path) -> None:
    """Refuse main dirt the operator-presence gate let through (CBN-EPIC-450.4).

    That gate exempts the candidate's own open tasknote so a stranded run can
    resume in place (CBN-109). A worktree starts from `HEAD` and carries
    nothing uncommitted, so the worker would never see that note, and the
    landing would then refuse on the same dirt in main.
    """
    dirt = git_porcelain(root)
    if dirt is None:
        raise UserError(f"{project_id}: cannot read git status in {root}; not launching")
    if dirt:
        raise UserError(
            f"{project_id}: the main checkout has {len(dirt)} uncommitted change(s) a "
            f"worktree would not carry: {dirt_sample(dirt)} — an isolated run starts from "
            "HEAD, so commit them, then re-run"
        )


def _refuse_baseline_dirt(context: WorktreeContext) -> None:
    """Refuse a worktree the baseline suite left dirty (CBN-EPIC-450.4).

    The in-place path re-runs the presence gate after the baseline for this;
    here the dirt would be counted as the worker's and refuse the landing.
    """
    dirt = git_porcelain(context.worker_root)
    if dirt is None or dirt:
        what = (
            f"left {len(dirt)} uncommitted change(s): {dirt_sample(dirt)}"
            if dirt
            else "left a tree git cannot read"
        )
        raise UserError(
            f"{context.branch}: the baseline suite {what} in {context.worker_root} — "
            "make the suite write only ignored paths; not launching"
        )


def _save_bootstrap_log(
    store: Store, record: RegistryRecord, candidate: NextTask, invocation: str, body: str
) -> str:
    try:
        log = store.save_suite(record.id, invocation, f"{candidate.task_id}-bootstrap", body)
    except OSError as exc:
        return f"Bootstrap log not saved: {exc}"
    return f"Bootstrap log: {log}"


def _settle_isolated(
    context: WorktreeContext,
    snap: CloseSnapshot,
    suite_log: str | None,
    measured_head: str | None,
    *,
    store: Store,
    deps: RunDeps,
    trace: InvocationTrace,
    started: float | None = None,
) -> tuple[CloseSnapshot, str | None]:
    """Re-measure a moved HEAD, then land a verified closure (CBN-EPIC-450.4).

    The worker is stopped, so the worktree HEAD read here is final. A closure
    whose HEAD moved after the suite started is measured again there, so the
    suite on the record is the one for the commit offered to land. `started`,
    the close wait's start, carries the `progress:` heartbeat through that
    re-measure (CBN-456). Only a
    `closed` outcome, unescalated, whose suite is green, lands; anything else
    stays on its branch and stops on its own record. A landing that does not
    happen sets `trace.unlanded`, which the loop stops `landing` on.
    """
    if snap.outcome not in CLOSURE_OUTCOMES:
        return snap, suite_log
    if snap.suite is not None and snap.suite.verdict is not SuiteVerdict.GREEN:
        # Not green: the record's own `suite` stop names it; nothing lands.
        return snap, suite_log
    # Green from here on, so a close main does not get must stop `landing`.
    trace.unlanded = _never_lands(snap)
    if trace.unlanded is not None:
        return snap, suite_log
    try:
        final = git_head(context.worker_root)
    except GitUnreadable as exc:
        final, unreadable = None, f"git could not read the worktree HEAD: {exc}"
    else:
        unreadable = None if final is not None else "the worktree has no readable HEAD"
    if final is not None and final != measured_head:
        if deps.echo is not None:
            deps.echo(f"suite: worktree HEAD moved to {final[:12]} during the suite; re-measuring")
        measured = _remeasure(context, store, deps, started)
        snap = snap.model_copy(update={"suite": measured.suite})
        suite_log = measured.log
    if snap.suite is None or snap.suite.verdict is not SuiteVerdict.GREEN:
        return snap, suite_log
    if final is None:
        trace.unlanded = unreadable
    else:
        _land(context, final, deps.echo, trace)
    return snap, suite_log


def _remeasure(
    context: WorktreeContext, store: Store, deps: RunDeps, started: float | None
) -> SuiteRun:
    """Measure the isolated worktree again under the `progress:` heartbeat (CBN-456)."""
    with SuiteHeartbeat(deps.echo, started) as beat:
        beat.stage = "suite"

        def on_verb(verb: str) -> None:
            beat.stage = f"suite {verb}"

        return measure_suite(context.execution_root, store, deps, on_verb=on_verb)


def _never_lands(snap: CloseSnapshot) -> str | None:
    """Why a green (or unmeasured) closure is not a landing candidate, or `None`."""
    if snap.suite is None:
        return "no suite was measured on its close"
    if snap.escalation is not None:
        return f"its close escalated ({snap.escalation.value})"
    if snap.outcome is not Outcome.CLOSED:
        return f"a {snap.outcome.value} close is never landed automatically"
    return None


def _land(
    context: WorktreeContext,
    sha: str,
    echo: Callable[[str], None] | None,
    trace: InvocationTrace,
) -> None:
    """Fast-forward main to `sha` and remove the worktree, or record why not."""
    say = echo or (lambda _line: None)
    try:
        reason = landing.land(context, sha, ref=trace.landing_ref)
    except GitUnreadable as exc:
        reason = f"git did not answer while landing: {exc}"
    if reason is not None:
        trace.unlanded = reason
        say(f"landing: {context.branch} not landed — {reason}")
        return
    say(f"landing: {context.branch} fast-forwarded {context.landing_root} to {sha[:12]}")
    trace.worktree = None
    trace.mark_landed(sha)
    # The suite measured `sha` green in the worktree, and main is now at it.
    trace.green_sha = sha
    unsynced = landing.sync_submodules(context)
    if unsynced is not None:
        trace.retained.append(
            f"{context.branch.removeprefix(BRANCH_PREFIX)} landed at {sha[:12]}, but the main "
            f"checkout's submodules were not updated ({unsynced}); run "
            f"git -C {shlex.quote(str(context.landing_root))} submodule update "
            "before the next run."
        )
        say(f"landing: submodules not updated — {unsynced}")
        trace.note_landing(f"submodules not updated: {unsynced}")
    why = landing.cleanup(context, sha)
    if why is None:
        say(f"landing: removed worktree {context.worker_root} and branch {context.branch}")
        return
    trace.retained.append(
        cleanup_recovery(
            task_id=context.branch.removeprefix(BRANCH_PREFIX),
            sha=sha,
            branch=context.branch,
            worker_root=str(context.worker_root),
            landing_root=str(context.landing_root),
            why=why,
        )
    )
    say(f"landing: worktree {context.worker_root} not removed — {why}")
    trace.note_landing(f"worktree not removed: {why}")


def _ready_dispatch(
    store: Store,
    record: RegistryRecord,
    candidate: NextTask,
    *,
    invocation: str,
    measure_baseline: bool,
    deps: RunDeps,
    trace: InvocationTrace,
) -> tuple[Path, str, WorktreeContext | None]:
    """Readiness before launch: `(root to run in, start_sha, worktree or None)`.

    An isolated record runs in its own `wt-<ID>` worktree (CBN-EPIC-450.4):
    the returned root is then its execution root, used for everything after,
    and the main checkout is read again only to land.
    """
    root = Path(record.path)
    start_sha = _ready_head(store, record, root, candidate, deps, trace)
    context: WorktreeContext | None = None
    if record.isolate:
        context = _ready_worktree(
            store, record, candidate, start_sha, invocation=invocation, deps=deps, trace=trace
        )
        root = context.execution_root
        if not measure_baseline and start_sha != trace.green_sha:
            # Main moved since a suite last measured it (an operator commit
            # during a park, CBN-EPIC-450.5), so the tree this dispatch starts
            # from is unmeasured.
            measure_baseline = True
            if deps.echo is not None:
                deps.echo(f"baseline: main moved to {start_sha[:12]} since its last green suite")

    # Readiness, second half (CBN-420): after the dirty-tree gate, so the suite
    # measures a clean tree and dirt refuses for its own, more specific reason.
    if measure_baseline:
        trace.result.stop_reason = StopReason.BASELINE
        _refuse_if_red_baseline(
            store, record, root, candidate, invocation=invocation, deps=deps, trace=trace
        )
        # The suite took minutes: an operator commit may have moved HEAD, or the
        # suite's own output dirtied the tree. The close diffs against `start_sha`.
        # A worktree's `start_sha` is its base, fixed; main moving is a landing check.
        if context is None:
            start_sha = _ready_head(store, record, root, candidate, deps, trace)
        else:
            trace.result.stop_reason = StopReason.WORKTREE
            _refuse_baseline_dirt(context)
            trace.green_sha = start_sha
    return root, start_sha, context


def _run_gated(
    store: Store,
    record: RegistryRecord,
    candidate: NextTask,
    *,
    marked: list[str],
    baseline: list[str],
    invocation: str,
    measure_baseline: bool,
    deps: RunDeps,
    trace: InvocationTrace,
    retained: frozenset[str] = frozenset(),
) -> RunRecord:
    started_at = utc_now()
    # No already-`blocked` branch here since CBN-252: the headless pick denies a
    # parked row (`parked`). One arrives only by a sub-second race (the note
    # flips after the read); the close then classifies it `worker-parked`.

    trace.result.task_id = candidate.task_id
    trace.result.escalation_reason = None
    trace.result.provider = record.provider
    trace.result.model = record.model
    trace.unlanded = None
    root, start_sha, context = _ready_dispatch(
        store,
        record,
        candidate,
        invocation=invocation,
        measure_baseline=measure_baseline,
        deps=deps,
        trace=trace,
    )

    posture = launch_posture(record, root, deps.echo, retained)
    # Tee target for a `local` worker's stdout/stderr (CBN-162). Computed for
    # every tier and attached to the run record only if a file actually landed,
    # so an adapter that ignores `log_path` leaves no dangling path.
    log_path = store.log_file(record.id, invocation, candidate.task_id)
    trace.result.worker_log = str(log_path)
    trace.result.suite_log = None
    owns = deps.worker is None
    trace.result.stop_reason = StopReason.WORKER_START
    client = deps.worker or select_worker(store.home)
    attempt = Attempt()
    registry_before = store.registry_digest()
    records_before = len(trace.runs)
    raised = False
    try:
        snap = launch_until_close(
            client,
            attempt,
            record,
            candidate,
            posture,
            root=root,
            start_sha=start_sha,
            log_path=log_path,
            baseline=baseline,
            deps=deps,
            trace=trace,
        )
        if log_path.is_file():
            snap = snap.model_copy(update={"log_path": str(log_path)})
        run_provider, run_model = attribution(attempt)
        suite_log: str | None = None
        measured_head: str | None = None
        if attempt.session is not None:
            snap, suite_log, measured_head = settle_and_measure(
                client,
                attempt.session,
                snap,
                store,
                root,
                task_id=candidate.task_id,
                start_sha=start_sha,
                posture=posture,
                baseline=baseline,
                deps=deps,
                started=attempt.started,
            )
            # The worker can still be writing (a `## Completed` rotation, e.g.)
            # through the settle and the suite. Stop it, then re-read the tree,
            # so the record names what it actually left (CBN-352), and any commit
            # or amend it landed meanwhile is attributed (CBN-356).
            retire_session(client, attempt, trace)
            snap = reinspect_settled(
                root,
                candidate.task_id,
                start_sha,
                snap,
                unattended=posture.unattended_pick,
                drain=posture.drain_pick,
                baseline=baseline,
                on_branch=True,
                retained=posture.retained,
            )
            snap = refresh_tree(snap, root, candidate.task_id, baseline=baseline, since=start_sha)
        snap = _flag_registry_change(snap, registry_before, store.registry_digest())
        if context is not None:
            snap, suite_log = _settle_isolated(
                context,
                snap,
                suite_log,
                measured_head,
                store=store,
                deps=deps,
                trace=trace,
                started=attempt.started,
            )
        return finish(
            store,
            record,
            candidate,
            snap,
            provider=run_provider,
            model=run_model,
            exhausted=sorted(attempt.exhausted),
            marked=marked,
            invocation=invocation,
            started_at=started_at,
            suite_log=suite_log,
            deps=deps,
            trace=trace,
            worktree=context,
        )
    except BaseException:
        raised = True
        raise
    finally:
        if attempt.session is not None:
            shutdown_session(client, attempt.session, trace)
        if raised and len(trace.runs) == records_before:
            # A raise wrote no task record to carry a registry change, so the
            # receipt names it (CBN-428). Read once the worker is stopped, and
            # best effort: it must not replace the exception unwinding.
            with contextlib.suppress(Exception):
                trace.registry_changed = _registry_changes(registry_before, store.registry_digest())
        if owns:
            client.close()
