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
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from caobunga.close import reinspect_settled
from caobunga.errors import (
    GitUnreadable,
    HeadLost,
    NoTaskError,
    ReportError,
    UserError,
)
from caobunga.finish import finish, refresh_tree, settle_and_measure
from caobunga.flaitron import (
    NextTask,
    ProjectRead,
    completed_task_ids,
    eligibility,
    list_open_tasknotes,
    read_project,
)
from caobunga.git import git_head, git_toplevel
from caobunga.invocation import (
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
    closed_green,
    reconcile_stale,
)
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
    ExecutionTier,
    Outcome,
    RegistryRecord,
    RunRecord,
    utc_now,
)
from caobunga.presence import FILING_ROOT, tree_state
from caobunga.run_echo import (
    denial_codes,
    echo_eligibility,
    echo_observe,
    echo_unparsed,
    no_task_message,
    outcome_exit_detail,
    run_tally,
    suite_exit_detail,
    unpinned_exit_detail,
)
from caobunga.store import Store
from caobunga.suite import SuiteRun, suite_stops
from caobunga.usage import ProviderUsage
from caobunga.wait import DEFAULT_POLL, SETTLE_GRACE
from caobunga.worker import Worker, select_worker

ENV_TIMEOUT = "CAOBUNGA_RUN_TIMEOUT"
DEFAULT_TIMEOUT = 7200.0


def run_timeout() -> float:
    raw = os.environ.get(ENV_TIMEOUT)
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        return float(raw)
    except ValueError as exc:
        raise UserError(f"invalid {ENV_TIMEOUT}={raw!r}") from exc


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
    options = _RunOptions(
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
    _, last, error = _drive_invocation(project_id, store, options)
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
    """Return a durable receipt, including refusals and incomplete approved work."""
    options = _RunOptions(
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
    result, _, _ = _drive_invocation(project_id, store, options)
    return result


@dataclass(frozen=True)
class _RunOptions:
    """The public adapters' knobs as passed, bundled once per call (CBN-EPIC-363.2).

    Unlike `RunDeps`, `timeout` is still unresolved here: `_execute_run`
    resolves it after the `timeout-config` stamp, so a bad `run_timeout()` keeps
    its stop reason. `store` stays a separate argument, as on `RunDeps`.
    """

    worker: Worker | None
    poll_interval: float
    timeout: float | None
    sleep: Callable[[float], None]
    echo: Callable[[str], None] | None
    usage: Sequence[ProviderUsage] | None
    publish_status: Callable[[Store, RunRecord], Path] | None
    on_run: Callable[[RunRecord], None] | None
    run_suite: Callable[[Path, Store], SuiteRun] | None
    grace: float


FORCE_QUIT_PRESSES = 3
INTERRUPT_NOTICE = (
    "interrupt received — stopping the worker and writing the record (~10 s); "
    "^C twice more to force-quit"
)


@dataclass
class _InterruptGuard:
    """Press count for one invocation; `finalizing` once the run itself has ended."""

    presses: int = 0
    finalizing: bool = False


@contextlib.contextmanager
def _interrupt_guard() -> Iterator[_InterruptGuard]:
    """Count ^C presses for one invocation so a repeat cannot skip the receipt (CBN-375).

    The first press raises `KeyboardInterrupt` as the default handler does, and
    the unwind stops the worker and writes the `interrupted` receipt. Presses
    during that cleanup re-print and are absorbed, as is a first press once the
    run has ended and only the receipt write is left; the third raises again to
    force the exit. Signal handlers install only on the main thread, and never
    over an inherited `SIG_IGN`, so any other caller keeps its disposition. The
    notice is a raw write to fd 2: a buffered write from inside a handler that
    interrupted one raises.
    """
    guard = _InterruptGuard()
    if (
        threading.current_thread() is not threading.main_thread()
        or signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    ):
        yield guard
        return

    def handler(_signum: int, _frame: object) -> None:
        guard.presses += 1
        if guard.presses >= FORCE_QUIT_PRESSES:
            raise KeyboardInterrupt
        if guard.presses == 1:
            _say(INTERRUPT_NOTICE)
            if not guard.finalizing:
                raise KeyboardInterrupt
            return
        _say("interrupt received again — still writing the record; ^C once more to force-quit")

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield guard
    finally:
        signal.signal(
            signal.SIGINT, previous if previous is not None else signal.default_int_handler
        )


def _say(line: str) -> None:
    with contextlib.suppress(OSError):
        os.write(2, f"{line}\n".encode())


def _drive_invocation(
    project_id: str, store: Store | None, options: _RunOptions
) -> tuple[InvocationResult, RunRecord | None, BaseException | None]:
    store = store or Store()
    echo = options.echo
    with _interrupt_guard() as guard:
        trace = InvocationTrace(project_id, store)
        error: BaseException | None = None
        exit_code = 0
        try:
            _execute_run(project_id, store, options, trace=trace)
        except KeyboardInterrupt as exc:
            error = exc
            exit_code = 130
            trace.result.status = InvocationStatus.INTERRUPTED
            trace.result.stop_reason = "interrupted"
            trace.result.detail = (
                "Invocation interrupted; closure and readiness require reconciliation."
            )
        except Exception as exc:  # noqa: BLE001 — receipt boundary accounts for unexpected failures
            error = exc
            exit_code = exc.exit_code if isinstance(exc, UserError) else 4
            _record_invocation_error(trace, exc)
        guard.finalizing = True
        if error is None and trace.last is not None and trace.last.outcome is Outcome.FAILED:
            exit_code = 4
        result = trace.finish(exit_code=exit_code)
        if echo is not None and result.tier is not ExecutionTier.OBSERVE:
            # The bare `outcome` code names no cause; the receipt's detail does (CBN-287).
            cause = (
                f": {' '.join(result.detail.split())}" if result.stop_reason == "outcome" else ""
            )
            # `last` alone hides what the drain's earlier tasks did (CBN-295).
            ran = f"; ran {len(trace.runs)}: {run_tally(trace.runs)}" if len(trace.runs) > 1 else ""
            echo(
                f"invocation: {result.status.value} — {result.stop_reason}{cause}{ran}; "
                f"successful={str(result.successful).lower()}"
            )
            echo(f"invocation-record: {store.invocation_file(result.receipt_id)}")
            for step in result.recovery:
                echo(f"recovery: {step}")
    return result, trace.last, error


def _record_invocation_error(trace: InvocationTrace, exc: Exception) -> None:
    result = trace.result
    result.detail = str(exc)
    if isinstance(exc, NoTaskError):
        result.stop_reason = "denied" if result.denials or result.unparsed_lines else "empty"
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
            result.stop_reason = "git-unreadable"
        elif isinstance(exc, HeadLost):
            result.stop_reason = "head-lost"
        elif isinstance(exc, ReportError):
            result.stop_reason = "report"
        elif not isinstance(exc, UserError):
            result.stop_reason = "orchestrator-error"


def _execute_run(
    project_id: str,
    store: Store,
    options: _RunOptions,
    *,
    trace: InvocationTrace,
) -> RunRecord | None:
    """Honor the project's tier. Observe returns None; other tiers write run records."""
    echo = options.echo
    trace.result.stop_reason = "registry"
    record = store.get(project_id)
    trace.register(record)
    if not record.enabled:
        trace.result.stop_reason = "disabled"
        raise UserError(f"project {project_id} is disabled; not launching")
    # Held for the rest of the function — every tier, observe included — so a
    # second concurrent `caobunga run` on the same repo — under this slug or any
    # other registered at the same path or anywhere else in the same git
    # checkout — exits 2 instead of racing a live worker in it (CBN-EPIC-16.2,
    # CBN-EPIC-313.2, CBN-EPIC-313.6). After the registry validation above so
    # unknown/disabled projects keep their own messages.
    trace.checkpoint()
    trace.result.stop_reason = "active-run"
    root = Path(record.path)
    with store.run_lock(record.id, git_toplevel(root) or root):
        if record.tier is ExecutionTier.OBSERVE:
            trace.result.stop_reason = "observe"
            echo_observe(record, _read_project_or_stop(root), store, echo)
            trace.result.status = InvocationStatus.OBSERVED
            return None
        # Under the lock, so a dead run's receipt is told from a live one (CBN-376).
        reconcile_stale(store, record, trace.result.receipt_id, echo)

        # The receipt and each task record share one invocation key. Observe
        # leaves the key in memory and writes no receipt or task record.
        invocation = trace.result.invocation_id
        last: RunRecord | None = None
        first = True
        trace.result.stop_reason = "timeout-config"
        timeout_s = options.timeout if options.timeout is not None else run_timeout()
        trace.result.timeout_seconds = timeout_s
        unattended_pick = record.tier in LAUNCHING_TIERS
        drain_pick = record.tier in DRAIN_TIERS
        pinned: frozenset[str] = frozenset()
        deps = RunDeps(
            worker=options.worker,
            poll_interval=options.poll_interval,
            timeout=timeout_s,
            sleep=options.sleep,
            usage=options.usage,
            echo=echo,
            publish_status=options.publish_status,
            run_suite=options.run_suite,
            grace=options.grace,
            trace=trace,
        )
        while True:
            trace.result.stop_reason = "project-read"
            read = _read_project_or_stop(root, unattended=unattended_pick, drain=drain_pick)
            candidate = read.candidate
            if first:
                pinned = _marked_ids(read)
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
                deps=deps,
            )
            if options.on_run is not None:
                options.on_run(written)
            last = written
            first = False
            stop = _post_run_stop(last)
            if record.tier is ExecutionTier.GATED:
                # One task is the whole scope: any committed green close finishes it (CBN-301).
                _record_gated_stop(trace, None if closed_green(last) else stop)
                return last
            if stop is not None:
                _record_gated_stop(trace, stop)
                trace.result.status = InvocationStatus.INCOMPLETE
                _echo_drain_exit(*stop, echo)
                return last


def _read_project_or_stop(
    root: Path, *, unattended: bool = False, drain: bool = False
) -> ProjectRead:
    """`read_project` for the direct reads outside `wait.poll`: an `OSError` is a named stop.

    Here nothing treats it as transient (CBN-168), so left raw it would reach the
    receipt boundary as `orchestrator-error` and traceback the CLI; a `UserError`
    keeps the `observe` / `project-read` stage and exits 2 like a missing PLAN.
    """
    try:
        return read_project(root, unattended=unattended, drain=drain)
    except OSError as exc:
        raise UserError(f"cannot read the Flaitron project at {root}: {exc}") from exc


def _record_pre_stop(
    trace: InvocationTrace, stop: tuple[str, str], candidate: NextTask | None
) -> None:
    trace.result.stop_reason, trace.result.detail = stop
    if candidate is not None:
        trace.result.task_id = candidate.task_id
        trace.result.status = InvocationStatus.INCOMPLETE


def _record_gated_stop(trace: InvocationTrace, stop: tuple[str, str] | None) -> None:
    trace.result.stop_reason, trace.result.detail = stop or ("gated", "One-task scope finished.")
    if stop is not None:
        trace.result.status = InvocationStatus.INCOMPLETE
        # A committed run's summary is already in the `run:` dump (CBN-302).
        if trace.last is not None and stop[0] == "outcome" and not trace.last.commit_sha:
            trace.result.detail += f" — {trace.last.summary}"


def _refuse_if_dirty(project_id: str, root: Path, candidate: NextTask) -> None:
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
    """
    state = tree_state(root, candidate, list_open_tasknotes(root))
    if not state.readable:
        raise UserError(f"{project_id}: cannot read git status in {root}; not launching")
    if not state.blocking:
        return
    blocking = state.blocking
    # The whole list: the recovery step says "inspect the listed changes" (CBN-287).
    shown = "; ".join(entry.text.strip() for entry in blocking)
    if state.filing_only:
        raise UserError(
            f"{project_id}: target repo has {len(blocking)} uncommitted change(s), "
            f"all of them flaitron filing artifacts a dispatched worker writes; "
            f"if a worker filed this, commit it: "
            f"git -C {root} add {FILING_ROOT} && git -C {root} commit -m "
            f'"chore: commit worker filing" — then re-run: {shown}'
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


def _echo_drain_exit(reason: str, detail: str, echo: Callable[[str], None] | None) -> None:
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
) -> tuple[str, str] | None:
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
        return "unpinned", unpinned_exit_detail(candidate)
    return None


def _post_run_stop(last: RunRecord) -> tuple[str, str] | None:
    """The drain exit this run's record ends on, or None to read again."""
    if last.outcome in CLOSURE_OUTCOMES and suite_stops(last):
        # A closure whose suite did not pass (CBN-EPIC-202.4); any closure outcome,
        # so a red de-scoped / no-tasknote close names the suite fix (CBN-304).
        return "suite", suite_exit_detail(last)
    if last.outcome is not Outcome.CLOSED:
        return "outcome", outcome_exit_detail(last)
    return None


def _no_candidate_exit(read: ProjectRead) -> tuple[str, str]:
    """Classify the no-eligible-candidate exit into `complete` / `incomplete`.

    A drain has no cohort: any denial is a row the drain would have taken, so the
    scope is the whole queue.
    """
    if read.denied:
        return (
            "incomplete",
            f"no eligible candidate; {len(read.denied)} denied: {denial_codes(read.denied)}",
        )
    return "complete", "no eligible candidate remains"


def _marked_ids(read: ProjectRead) -> frozenset[str]:
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
        )[0]
    )


def _run_gated(
    store: Store,
    record: RegistryRecord,
    candidate: NextTask,
    *,
    marked: list[str],
    baseline: list[str],
    invocation: str,
    deps: RunDeps,
) -> RunRecord:
    root = Path(record.path)
    started_at = utc_now()
    # No already-`blocked` branch here since CBN-252: the headless pick denies a
    # parked row (`parked`). One arrives only by a sub-second race (the note
    # flips after the read); the close then classifies it `worker-parked`.

    deps.trace.result.task_id = candidate.task_id
    deps.trace.result.escalation_reason = None
    deps.trace.result.provider = record.provider
    deps.trace.result.model = record.model
    deps.trace.result.stop_reason = "git-unreadable"
    start_sha = git_head(root)
    if start_sha is None:
        # git answered: no repo, or an unborn HEAD — not an unreadable git (CBN-321).
        deps.trace.result.stop_reason = "not-a-repo"
        raise UserError(f"not a git repository: {root}")
    deps.trace.start_sha = start_sha

    # Operator-presence boundary (CBN-EPIC-17.3). After the git-repo check, so
    # the dirt read is never the one reporting "not a repository"; before
    # the pick, so a dirty tree refuses for the more specific reason.
    deps.trace.result.stop_reason = "dirty-tree"
    try:
        _refuse_if_dirty(record.id, root, candidate)
    except UserError as exc:
        # The pick echoed `allow` before this read (CBN-288); say it no longer stands.
        # A hung or missing `git` is not dirt (CBN-317), so it names its own cause.
        cause = "git-unreadable" if isinstance(exc, GitUnreadable) else "dirty-tree"
        if deps.echo is not None and candidate.eligibility_reason is not None:
            deps.echo(f"eligibility: {candidate.task_id} allow withdrawn: {cause} — not launching")
        raise

    posture = launch_posture(record, root, deps.echo)
    # Tee target for a `local` worker's stdout/stderr (CBN-162). Computed for
    # every tier and attached to the run record only if a file actually landed,
    # so an adapter that ignores `log_path` leaves no dangling path.
    log_path = store.log_file(record.id, invocation, candidate.task_id)
    deps.trace.result.worker_log = str(log_path)
    deps.trace.result.suite_log = None
    owns = deps.worker is None
    deps.trace.result.stop_reason = "worker-start"
    client = deps.worker or select_worker()
    attempt = Attempt()
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
        )
        if log_path.is_file():
            snap = snap.model_copy(update={"log_path": str(log_path)})
        run_provider, run_model = attribution(attempt)
        suite_log: str | None = None
        if attempt.session is not None:
            snap, suite_log = settle_and_measure(
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
            )
            # The worker can still be writing (a `## Completed` rotation, e.g.)
            # through the settle and the suite. Stop it, then re-read the tree,
            # so the record names what it actually left (CBN-352), and any commit
            # or amend it landed meanwhile is attributed (CBN-356).
            retire_session(client, attempt, deps.trace)
            snap = reinspect_settled(
                root,
                candidate.task_id,
                start_sha,
                snap,
                unattended=posture.unattended_pick,
                drain=posture.drain_pick,
                baseline=baseline,
                on_branch=True,
            )
            snap = refresh_tree(snap, root, candidate.task_id)
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
        )
    finally:
        if attempt.session is not None:
            shutdown_session(client, attempt.session, deps.trace)
        if owns:
            client.close()
