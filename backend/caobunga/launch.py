"""The launch: pick a provider, start the worker, and wait, re-picking past a usage limit.

`caobunga.launch` owns the "who runs this task, and did they get to run it"
question — the per-task launch posture (`launch_posture`), the pick → launch →
wait loop with its CBN-213 usage-limit re-pick (`launch_until_close`), the
live attempt state it mutates (`Attempt`), session retire and shutdown, the
provider/model the record names (`attribution`), and the failure snapshot a
worker error becomes (`_failed_from_worker`). `RunDeps` / `Posture` live here
because this is their main consumer. A leaf module: `close`, `errors`, the flaitron
façade, `git`, `invocation`, `models`, `picker`, `prompt`, `store`, `suite`,
`taskmap`, `usage`, `wait`, and `worker` cross in, and nothing calls back into
`loop.py`. Split out of `loop.py` at CBN-EPIC-363.3.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from caobunga import usage as usage_mod
from caobunga.close import CloseSnapshot
from caobunga.errors import GitUnreadable, Unlaunchable, UserError, WorkerError
from caobunga.flaitron import (
    NextTask,
    list_open_tasknotes,
    pinned_skill_rel,
    supports_unattended,
    unattended_module_rel,
)
from caobunga.git import git_dirt, git_head, tree_after
from caobunga.invocation import InvocationTrace
from caobunga.models import (
    DRAIN_TIERS,
    LAUNCHING_TIERS,
    Escalation,
    Outcome,
    RegistryRecord,
    RunRecord,
)
from caobunga.picker import ProviderUnavailable, pick
from caobunga.prompt import worker_message
from caobunga.store import Store
from caobunga.suite import SuiteRun
from caobunga.taskmap import resolve_effort
from caobunga.usage import Leftover, ProviderUsage
from caobunga.wait import is_usage_limit_line, wait_for_close
from caobunga.worker import Session, Worker, safe_session_name


def _is_usage_limit_refusal(client: Worker, session: Session | None) -> bool:
    if session is None:
        return False
    lines = (*client.stderr_tail(session), *client.stdout_tail(session))
    return any(is_usage_limit_line(line) for line in lines)


def _tree_is_clean(root: Path) -> bool:
    """True only when porcelain is readable and empty. Unreadable is not clean."""
    lines = git_dirt(root)
    return lines is not None and not lines


def _should_repick(
    record: RegistryRecord,
    reason: Escalation,
    client: Worker,
    session: Session | None,
    root: Path,
    start_sha: str,
) -> bool:
    """Usage-limit refusal on a clean tree at an unmoved HEAD, unpinned — CBN-213.

    A hard `register --provider` pin does not pivot: `pick` would return the
    same provider even after an exhausted overlay (`_override` ignores leftover).
    `TIMEOUT` is a still-running worker; `WORKER_LAUNCH` has no session output.
    A clean tree alone is not "left nothing" (CBN-260): a worker that committed
    and then hit its limit leaves porcelain empty, and relaunching the task on
    top of that commit is the relaunch-over-work the clean-tree gate exists to refuse.
    An unreadable HEAD is not an unmoved one.
    """
    if record.provider is not None:
        return False
    if reason not in (Escalation.WORKER_ERROR, Escalation.WORKER_EXITED):
        return False
    if not _is_usage_limit_refusal(client, session):
        return False
    try:
        if git_head(root) != start_sha:
            return False
    except GitUnreadable:
        return False
    return _tree_is_clean(root)


def _usage_with_exhausted(
    usage: Sequence[ProviderUsage] | None,
    exhausted: set[str],
) -> Sequence[ProviderUsage] | None:
    """Copy of `usage` with named providers marked leftover=exhausted.

    In-invocation overlay only — not persisted, not a leftover dump. Empty
    `exhausted` returns `usage` unchanged so the first pick is byte-identical.
    """
    if not exhausted:
        return usage
    rows = list(usage) if usage is not None else usage_mod.probe_all()
    return [
        row.model_copy(update={"leftover": Leftover.EXHAUSTED})
        if row.provider in exhausted
        else row
        for row in rows
    ]


def _park_mismatch(root: Path, task_id: str) -> str | None:
    """Name an open note that carries `park-reason:` without `status: blocked` (CBN-328).

    A fleet repo's CORE-243 shape: the worker reported a park but left the note
    `in-progress`. Report-only — close detection keys on `status:` alone
    (CBN-327), so this never reclassifies the exit as a park.
    """
    for note in list_open_tasknotes(root):
        if note.task_id == task_id and note.park_reason and note.status != "blocked":
            return f"park-reason present but status {note.status}, not blocked"
    return None


def _failed_from_worker(exc: WorkerError, root: Path, task_id: str) -> CloseSnapshot:
    reason = exc.escalation or Escalation.WORKER_ERROR
    summary = str(exc)
    # The mismatch goes ahead of the tree line: it is short and the summary is
    # clipped at the tail, where a long file sample would push it off.
    if reason is Escalation.WORKER_EXITED and (mismatch := _park_mismatch(root, task_id)):
        summary = f"{summary} — {mismatch}"
    if reason in (Escalation.WORKER_EXITED, Escalation.TIMEOUT):
        summary = f"{summary} — {tree_after(root, task_id)}"
    return CloseSnapshot(outcome=Outcome.FAILED, summary=summary, escalation=reason)


@dataclass(frozen=True)
class RunDeps:
    """`execute_run`'s injected collaborators and knobs, fixed for one invocation.

    Bundled so `_run_gated` / `launch_until_close` take one argument instead of
    re-forwarding each kwarg (CBN-266). `timeout` is the resolved value, never
    None. `store` stays positional: the call chain is keyed on it, not tuned by it.
    """

    worker: Worker | None
    poll_interval: float
    timeout: float
    sleep: Callable[[float], None]
    usage: Sequence[ProviderUsage] | None
    echo: Callable[[str], None] | None
    publish_status: Callable[[Store, RunRecord], Path] | None
    run_suite: Callable[[Path, Store], SuiteRun] | None
    grace: float
    trace: InvocationTrace


@dataclass(frozen=True)
class Posture:
    """What the worker is told and what the wait may claim, fixed before launch."""

    unattended: bool
    unattended_pick: bool
    drain_pick: bool


@dataclass
class Attempt:
    """The live launch state across re-picks, owned by `_run_gated`.

    Mutable on purpose: `launch_until_close` sets `session` the moment one
    exists, so `_run_gated`'s `finally` shuts it down even when an exception
    escapes mid-wait — a returned session would be lost on that path.
    """

    session: Session | None = None
    provider: str | None = None
    model: str | None = None
    exhausted: set[str] = field(default_factory=set)


def launch_posture(
    record: RegistryRecord,
    root: Path,
    echo: Callable[[str], None] | None,
) -> Posture:
    # Probe the target's pinned `/ft-task` before launching: a pin that does not
    # recognize `--unattended` turns the posture flag into the operator-less
    # banner it exists to prevent (CBN-108). Echo the degrade so a stale target
    # pin is visible in the run rather than silently costing posture.
    unattended = supports_unattended(root)
    if not unattended and echo is not None:
        echo(f"{record.id}: flaitron pin does not recognize --unattended; sending --fast")
    # Distinct from `unattended` above, deliberately (CBN-117). That one is the
    # *target's* capability and governs only which flag the worker message
    # carries; this one is *our* dispatch policy — the same expression the loop
    # uses for its own next read — and governs what the record may claim comes
    # next. Sourcing the claim from the probe would let a stale target pin
    # silently change what we say about caobunga's own queue. Since CBN-118 this
    # is `True` for every tier that reaches here; kept as the membership test
    # rather than a literal so a new tier inherits the right answer.
    return Posture(
        unattended=unattended,
        unattended_pick=record.tier in LAUNCHING_TIERS,
        drain_pick=record.tier in DRAIN_TIERS,
    )


def launch_until_close(
    client: Worker,
    attempt: Attempt,
    record: RegistryRecord,
    candidate: NextTask,
    posture: Posture,
    *,
    root: Path,
    start_sha: str,
    log_path: Path,
    baseline: list[str],
    deps: RunDeps,
) -> CloseSnapshot:
    """Pick → launch → wait, re-picking past a usage-limit refusal (CBN-213).

    Session shutdown on exit is the caller's; this only retires a session it
    is about to replace.
    """
    last_fail: WorkerError | None = None
    while True:
        deps.trace.result.stop_reason = "provider"
        try:
            chosen = pick(
                candidate.plan_model,
                provider=record.provider,
                model=record.model,
                providers=record.providers,
                usage=_usage_with_exhausted(deps.usage, attempt.exhausted),
            )
        except ValueError as exc:
            if last_fail is not None:
                return _failed_from_worker(last_fail, root, candidate.task_id)
            # No provider can run here is the machine's state, not this repo's:
            # exit 5, so `fleet` does not skip it as a 2 (CBN-EPIC-313.5).
            if isinstance(exc, ProviderUnavailable):
                raise Unlaunchable(str(exc)) from exc
            raise UserError(str(exc)) from exc
        ran = (attempt.provider, attempt.model)
        attempt.provider = chosen.provider
        attempt.model = chosen.model
        deps.trace.result.provider = chosen.provider
        deps.trace.result.model = chosen.model
        deps.trace.result.stop_reason = "worker-start"
        deps.trace.checkpoint()
        try:
            try:
                attempt.session = client.launch(
                    root,
                    provider=chosen.provider,
                    model=chosen.model,
                    # Per pick, not per posture: a CBN-213 re-pick changes the
                    # provider, and the rung is that provider's column (CBN-332).
                    effort=resolve_effort(candidate.plan_model, chosen.provider),
                    session_name=safe_session_name(f"{record.id}-{candidate.task_id}"),
                    log_path=log_path,
                )
                client.send(
                    attempt.session,
                    worker_message(
                        candidate,
                        unattended=posture.unattended,
                        unattended_module=unattended_module_rel(root),
                        pinned_skill=pinned_skill_rel(root),
                    ),
                )
            except WorkerError as exc:
                raise WorkerError(str(exc), Escalation.WORKER_LAUNCH) from exc
            except UserError as exc:
                return _fail_unlaunched_repick(
                    exc,
                    last_fail,
                    ran,
                    client,
                    attempt,
                    root=root,
                    task_id=candidate.task_id,
                    deps=deps,
                )
            return wait_for_close(
                client,
                attempt.session,
                root,
                candidate.task_id,
                start_sha,
                timeout=deps.timeout,
                poll_interval=deps.poll_interval,
                sleep=deps.sleep,
                unattended=posture.unattended_pick,
                drain=posture.drain_pick,
                baseline=baseline,
                echo=deps.echo,
            )
        except WorkerError as exc:
            # An unforeseen WorkerError still fails the run exactly as before;
            # it just falls back to the generic reason rather than inventing one.
            reason = exc.escalation or Escalation.WORKER_ERROR
            if not _should_repick(record, reason, client, attempt.session, root, start_sha):
                return _failed_from_worker(exc, root, candidate.task_id)
            attempt.exhausted.add(chosen.provider)
            last_fail = exc
            if deps.echo is not None:
                deps.echo(f"usage-limit: {chosen.provider} exhausted this invocation; re-picking")
            retire_session(client, attempt, deps.trace)


def _fail_unlaunched_repick(
    exc: UserError,
    last_fail: WorkerError | None,
    ran: tuple[str | None, str | None],
    client: Worker,
    attempt: Attempt,
    *,
    root: Path,
    task_id: str,
    deps: RunDeps,
) -> CloseSnapshot:
    """A re-pick that cannot launch (binary gone since `which`, no recipe) does
    not erase the worker that already ran: like an exhausted pick, the last
    failure is the record (CBN-316). Attribution is rewound to that worker and
    a session `launch` reserved before `send` refused is retired, so
    `attribution` and settle never see a worker that did not run. With no
    prior worker, refuse as before.
    """
    if last_fail is None:
        raise exc
    unlaunched = attempt.provider
    retire_session(client, attempt, deps.trace)
    attempt.provider, attempt.model = ran
    deps.trace.result.provider, deps.trace.result.model = ran
    deps.trace.checkpoint()
    if deps.echo is not None:
        deps.echo(f"usage-limit: re-pick {unlaunched} cannot launch: {exc}")
    return _failed_from_worker(last_fail, root, task_id)


def shutdown_session(client: Worker, session: Session, trace: InvocationTrace) -> None:
    failed = True
    try:
        client.shutdown(session)
    except WorkerError:
        pass  # Preserve the legacy shutdown policy; retain uncertainty in the receipt.
    else:
        failed = False
    finally:
        trace.worker_shutdown(session.session_name, failed=failed)


def retire_session(client: Worker, attempt: Attempt, trace: InvocationTrace) -> None:
    if attempt.session is not None:
        shutdown_session(client, attempt.session, trace)
        attempt.session = None


def attribution(attempt: Attempt) -> tuple[str | None, str | None]:
    """Provider/model the run record names: the live session's, else the last
    pick's when a re-pick ran out, else none (nothing launched)."""
    if attempt.session is not None:
        return attempt.session.provider or attempt.provider, attempt.model
    if attempt.exhausted:
        return attempt.provider, attempt.model
    return None, None
