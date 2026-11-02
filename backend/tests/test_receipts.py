"""Invocation receipts (CBN-272): outcomes, interrupts, stale-receipt reconcile."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import (
    CLOSED_PLAN,
    EXHAUSTED_PLAN,
    OPEN_PLAN,
    TWO_CLOSED,
    FakeWorker,
    _archive_note,
    _BusyWorker,
    _close_worker,
    _closing_client,
    _commit,
    _ExitedWorker,
    _git,
    _git_repo,
    _green_suite,
    _not_green_suite,
    _note,
    _register,
    _run,
    _usage,
    _UsageLimitThenCloseWorker,
)

from caobunga import loop
from caobunga.errors import ReceiptUnwritten, UserError, WorkerError
from caobunga.flaitron import ProjectRead, read_project
from caobunga.git import git_dirt, git_head
from caobunga.invocation import (
    InvocationTrace,
    reconcile_stale,
    running_verdicts,
    stale_receipts,
)
from caobunga.models import (
    Escalation,
    ExecutionTier,
    InvocationResult,
    InvocationStatus,
    LandingState,
    Outcome,
    RunRecord,
    StopReason,
    WorktreeRun,
    compact_utc,
    utc_now,
)
from caobunga.recovery import _NO_RECOVERY_STOPS, _RECOVERY, _parked_descoped
from caobunga.store import Store
from caobunga.suite import SuiteRun
from caobunga.worker import STATUS_ERROR, STATUS_EXITED, Session
from caobunga.worktree import start


# CBN-272: invocation receipts are independent of the last task's outcome.
def _invoke(project_id: str, store: Store, **kwargs):
    result = loop.execute_invocation(
        project_id,
        store=store,
        usage=_usage(),
        run_suite=kwargs.pop("run_suite", _green_suite),
        poll_interval=0,
        timeout=kwargs.pop("timeout", 2),
        **kwargs,
    )
    if result.tier is not ExecutionTier.OBSERVE:
        assert store.load_invocation(result.receipt_id) == result
        assert result.finished_at is not None
    return result


@pytest.mark.parametrize("tier", [ExecutionTier.GATED, ExecutionTier.PROCEED_ON_GREEN])
@pytest.mark.parametrize("verdict", ["green", "red", "timeout", "refused"])
def test_invocation_retains_closure_when_suite_stops(
    tmp_path: Path,
    caobunga_home: Path,
    tier: ExecutionTier,
    verdict: str,
) -> None:
    repo = _git_repo(
        tmp_path,
        OPEN_PLAN.replace(
            "- [ ] **CBN-012** [medium] [unattended] | cao-client — client work.", ""
        ),
    )
    store = Store(caobunga_home)
    _register(store, repo, tier=tier)
    worker = _closing_client(FakeWorker(), lambda _msg: _close_worker(repo, plan=EXHAUSTED_PLAN))
    result = _invoke(
        "app",
        store,
        worker=worker,
        run_suite=_green_suite if verdict == "green" else _not_green_suite(verdict),
    )
    run = store.last_run("app")
    assert run is not None
    assert result.run_records == [str(path) for path in store.runs_dir("app").glob("*.yaml")]
    assert run.invocation_id == result.invocation_id
    assert result.last_commit_sha == run.commit_sha == git_head(repo)
    assert result.legacy_exit_code == 0  # direct CLI compatibility
    assert result.successful is (verdict == "green")
    assert result.approved_scope_finished is (verdict == "green")
    assert result.stop_reason == (
        "suite" if verdict != "green" else "gated" if tier is ExecutionTier.GATED else "complete"
    )
    assert result.suite_log is not None and Path(result.suite_log).is_file()
    if verdict != "green":
        assert "test" in result.detail
        assert any("historical run record" in step for step in result.recovery)


@pytest.mark.parametrize("mode", ["empty", "unmarked", "blocked", "parked", "malformed"])
def test_invocation_empty_and_denied_scope(
    tmp_path: Path,
    caobunga_home: Path,
    mode: str,
) -> None:
    plan = OPEN_PLAN
    notes = None
    if mode == "empty":
        plan = TWO_CLOSED
    elif mode == "unmarked":
        plan = plan.replace(" [unattended]", "")
    elif mode == "blocked":
        plan = plan.replace("— reader work.", "— reader work. Blocked by [[X-001]].")
        plan = plan.replace("— client work.", "— client work. Blocked by [[X-001]].")
    elif mode == "parked":
        notes = {t: _note("blocked", "input-needed — access") for t in ("CBN-011", "CBN-012")}
    else:
        plan = "## High\n- [ ] **CBN-011** [!unattended] — malformed\n"
    repo = _git_repo(tmp_path, plan, notes)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    worker = FakeWorker()
    result = _invoke("app", store, worker=worker)
    assert not worker.launches
    assert not result.run_records
    assert result.legacy_exit_code == 3
    assert result.successful is (mode in {"empty", "unmarked"})
    assert result.approved_scope_finished is result.successful
    if mode == "parked":
        assert all("parked" in denial.reason for denial in result.denials)
    if mode == "malformed":
        assert result.unparsed_lines == [2]
        assert any("syntax" in step for step in result.recovery)


@pytest.mark.parametrize(
    "failure", ["dirty", "active", "provider", "disabled", "missing-plan", "no-git"]
)
def test_invocation_refusals_are_durable_without_run_records(
    tmp_path: Path,
    caobunga_home: Path,
    failure: str,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = FakeWorker()
    if failure == "dirty":
        (repo / "partial.py").write_text("partial\n")
    elif failure == "disabled":
        record = store.get("app").model_copy(update={"enabled": False})
        store._write_yaml(store.project_file("app"), record)
    elif failure == "missing-plan":
        (repo / ".flaitron/PLAN.md").unlink()
    elif failure == "no-git":
        repo = tmp_path / "plain"
        (repo / ".flaitron").mkdir(parents=True)
        (repo / ".flaitron/PLAN.md").write_text(OPEN_PLAN)
        _register(store, repo, slug="plain")
    if failure == "active":
        with store.run_lock("app", repo):
            result = _invoke("app", store, worker=worker)
    elif failure == "provider":
        result = loop.execute_invocation(
            "app", store=store, worker=worker, usage=[], run_suite=_green_suite
        )
        assert store.load_invocation(result.receipt_id) == result
    else:
        result = _invoke("plain" if failure == "no-git" else "app", store, worker=worker)
    assert not result.successful
    # No pickable provider is the machine's state, exit 5 (CBN-EPIC-313.5).
    assert result.legacy_exit_code == (5 if failure == "provider" else 2)
    assert not result.run_records
    assert not worker.launches
    assert result.recovery
    if failure == "dirty":
        assert result.stop_reason == "dirty-tree"
        assert result.tree and any("partial.py" in line for line in result.tree)
    if failure == "active":
        assert result.stop_reason == "active-run"
    if failure == "disabled":
        assert result.stop_reason == "disabled"


def test_red_baseline_receipt_is_a_refusal_pointing_at_its_log(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-420: a suite already red before launch is a readiness refusal, not a task run."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = FakeWorker()
    result = _invoke(
        "app", store, worker=worker, run_suite=_not_green_suite("red", baseline_green=False)
    )
    assert result.status is InvocationStatus.REFUSED
    assert result.stop_reason == "baseline"
    assert result.legacy_exit_code == 2
    assert not result.successful
    assert not result.run_records
    assert not worker.launches
    assert result.suite_log is not None
    assert result.suite_log.endswith("-CBN-011-baseline.log")
    assert Path(result.suite_log).is_file()
    assert any("already failing on the clean tree" in step for step in result.recovery)


def test_invocation_invalid_timeout_env_stops_at_timeout_config(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_timeout()` raises after the `timeout-config` stamp, so the receipt names it."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    monkeypatch.setenv(loop.ENV_TIMEOUT, "soon")
    worker = FakeWorker()
    result = _invoke("app", store, worker=worker, timeout=None)
    assert result.stop_reason == "timeout-config"
    assert result.legacy_exit_code == 2
    assert not worker.launches


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0", "-1"])
def test_invocation_non_finite_or_non_positive_timeout_env_stops_at_timeout_config(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """CBN-432: `nan` would otherwise disable the deadline; `0`/negative would kill the worker
    on its first poll. Both must raise the same `UserError` the non-numeric case already does."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    monkeypatch.setenv(loop.ENV_TIMEOUT, raw)
    worker = FakeWorker()
    result = _invoke("app", store, worker=worker, timeout=None)
    assert result.stop_reason == "timeout-config"
    assert result.legacy_exit_code == 2
    assert not worker.launches


@pytest.mark.parametrize("adapter", ["run", "invocation"])
def test_public_kwargs_reach_their_consumers(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    """Both public adapters forward every knob to the seam that consumes it (CBN-EPIC-363.2)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    settles: list[dict[str, object]] = []
    suites: list[Path] = []
    published: list[str] = []
    ran: list[str] = []
    lines: list[str] = []
    monkeypatch.setattr("caobunga.finish.settle_worker", lambda *_a, **kw: settles.append(kw))

    def nap(_seconds: float) -> None:
        return None

    def suite(root: Path, suite_store: Store) -> SuiteRun:
        suites.append(root)
        return _green_suite(root, suite_store)

    def publish(_store: Store, run: RunRecord) -> Path:
        published.append(run.task_id)
        return store.status_file("app")

    entry = loop.execute_run if adapter == "run" else loop.execute_invocation
    with _closing_client(FakeWorker(), lambda _msg: _close_worker(repo)) as worker:
        entry(
            "app",
            store=store,
            worker=worker,
            poll_interval=0.001,
            timeout=2,
            sleep=nap,
            usage=_usage(),
            echo=lines.append,
            publish_status=publish,
            on_run=lambda run: ran.append(run.task_id),
            run_suite=suite,
            grace=0.25,
        )
    assert settles == [{"poll_interval": 0.001, "sleep": nap, "grace": 0.25}]
    # The pre-launch baseline (CBN-420), then the close.
    assert suites == [repo, repo]
    assert published == ["CBN-011"]
    assert ran == ["CBN-011"]
    # `suite:` is `finish`'s echo, so the caller's echo reached `RunDeps`, not just the receipt.
    assert any(line.startswith("suite:") for line in lines)


@pytest.mark.parametrize("project", ["missing", "../escape"])
def test_invocation_unknown_slug_is_accounted_safely(caobunga_home: Path, project: str) -> None:
    store = Store(caobunga_home)
    result = _invoke(project, store, worker=FakeWorker())
    assert result.project == project
    assert not result.successful
    assert result.stop_reason == "registry"
    assert result.legacy_exit_code == 2
    assert store.invocation_file(result.receipt_id).parent == caobunga_home / "invocations"


def test_invocation_echo_names_the_outcome_cause(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-287: `outcome` alone names no cause; the echoed line carries the receipt's detail."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    lines: list[str] = []
    result = _invoke(
        "app", store, worker=_ExitedWorker(1, "", "Stopped before closure"), echo=lines.append
    )
    assert result.stop_reason == "outcome"
    assert f"invocation: incomplete — outcome: {result.detail}; successful=false" in lines


def test_invocation_gated_stop_keeps_the_summary_without_a_commit(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-302: with no closure commit the summary is the only cause the operator gets."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    result = _invoke("app", store, worker=_ExitedWorker(1, "", "Stopped before closure"))
    (run,) = store.list_runs("app")
    assert run.commit_sha is None
    assert run.summary
    assert result.detail.endswith(f" — {run.summary}")


def test_invocation_outcome_stop_omits_the_summary_when_a_commit_closed_it(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-302: the `run:` dump already prints the summary; the receipt need not repeat it.

    A drain, because a gated committed green close finishes its scope (CBN-301).
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    lines: list[str] = []

    def close_without_tasknote(_msg: str) -> None:
        (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
        _commit(repo, "feat: CBN-011 — close")

    worker = _closing_client(FakeWorker(), close_without_tasknote)
    result = _invoke("app", store, worker=worker, echo=lines.append)
    (run,) = store.list_runs("app")
    assert run.outcome is Outcome.NO_TASKNOTE
    assert run.commit_sha is not None
    assert run.summary
    assert result.stop_reason == "outcome"
    assert run.summary not in result.detail
    assert f"invocation: incomplete — outcome: {result.detail}; successful=false" in lines
    # The committed close is task evidence even though the drain stopped on it (CBN-301).
    assert "CBN-011" not in result.remaining_tasks


@pytest.mark.parametrize("descoped", [False, True])
@pytest.mark.parametrize(
    ("verdict", "detail"),
    [("red", "test exit 1"), ("timeout", "test timeout"), ("refused", "test refused: no justfile")],
)
def test_drain_stops_suite_on_a_non_green_committed_closure_outcome(
    tmp_path: Path, caobunga_home: Path, descoped: bool, verdict: str, detail: str
) -> None:
    """CBN-304: a non-green de-scoped / no-tasknote close stops `suite`, not `outcome`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    lines: list[str] = []

    def close(_msg: str) -> None:
        if descoped:
            _close_worker(repo, "CBN-011", CLOSED_PLAN, descoped=True)
        else:
            (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
            _commit(repo, "feat: CBN-011 — close")

    result = _invoke(
        "app",
        store,
        worker=_closing_client(FakeWorker(), close),
        echo=lines.append,
        run_suite=_not_green_suite(verdict),
    )
    (run,) = store.list_runs("app")
    assert run.outcome is (Outcome.DE_SCOPED if descoped else Outcome.NO_TASKNOTE)
    assert result.stop_reason == "suite"
    assert f"drain: suite — CBN-011 suite {verdict} — {detail}" in lines
    assert any("Fix the failing" in step for step in result.recovery)


@pytest.mark.parametrize("descoped", [False, True])
@pytest.mark.parametrize("verdict", ["green", "red"])
def test_invocation_gated_committed_close_finishes_the_one_task_scope(
    tmp_path: Path, caobunga_home: Path, descoped: bool, verdict: str
) -> None:
    """CBN-301: a gated no-tasknote / de-scoped close with a green suite is a complete receipt."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def close(_msg: str) -> None:
        if descoped:
            _close_worker(repo, "CBN-011", CLOSED_PLAN, descoped=True)
        else:
            (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
            _commit(repo, "feat: CBN-011 — close")

    result = _invoke(
        "app",
        store,
        worker=_closing_client(FakeWorker(), close),
        run_suite=_green_suite if verdict == "green" else _not_green_suite(verdict),
    )
    (run,) = store.list_runs("app")
    assert run.outcome is (Outcome.DE_SCOPED if descoped else Outcome.NO_TASKNOTE)
    assert result.last_commit_sha == run.commit_sha is not None
    if verdict == "green":
        assert result.status == "complete"
        assert result.successful and result.approved_scope_finished
        assert result.stop_reason == "gated"
        assert result.remaining_tasks == []
        assert result.recovery == []
    else:
        assert result.status == "incomplete"
        assert not result.successful
        assert result.remaining_tasks == ["CBN-011"]
        # The suite stop, not `outcome` (CBN-304): the recovery names the suite fix.
        assert result.stop_reason == "suite"
        assert "test exit 1" in result.detail
        assert run.summary not in result.detail
        assert any("Fix the failing" in step for step in result.recovery)


def test_invocation_echo_tally_covers_every_task_the_drain_ran(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-295: a close then a park must not read as only the park."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    lines: list[str] = []

    def park() -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-012.md"
        note.write_text(_note("blocked", "destructive — sql"), encoding="utf-8")

    steps = iter([lambda: _close_worker(repo), park])
    worker = _closing_client(FakeWorker(), lambda _msg: next(steps)())
    _invoke("app", store, worker=worker, echo=lines.append)
    closed, parked = store.list_runs("app")
    assert closed.commit_sha is not None
    tally = f"ran 2: CBN-011 closed {closed.commit_sha[:8]}, CBN-012 blocked (destructive)"
    assert any(line.startswith("invocation: incomplete") and tally in line for line in lines)
    assert parked.task_id == "CBN-012"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_invocation_process_exit_without_committed_closure(
    tmp_path: Path,
    caobunga_home: Path,
    exit_code: int,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(
        exit_code,
        "",
        "Stopped before closure",
        on_send=lambda: (repo / "partial.py").write_text("partial\n"),
    )
    result = _invoke("app", store, worker=worker)
    assert not result.successful
    assert result.last_commit_sha is None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.legacy_exit_code == 4
    assert f"exit code {exit_code}" in result.detail
    assert any("Committed closure evidence is missing" in step for step in result.recovery)
    assert result.shutdown == "returned"


def test_invocation_worker_exited_on_clean_unmoved_tree_says_nothing_ran(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    """CBN-293: a clean exit that touched nothing has no partial work to resume."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(0, "", "Declined: out of scope", on_send=lambda: _write_log(worker))
    result = _invoke("app", store, worker=worker)
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.tree == []
    assert result.tasknote_path is None
    assert worker.log_path is not None
    assert any(
        "Nothing ran for CBN-011" in step and str(worker.log_path) in step
        for step in result.recovery
    )
    assert not any("Committed closure evidence is missing" in step for step in result.recovery)
    assert not any("resume partial work" in step for step in result.recovery)


def test_invocation_worker_exited_nothing_ran_without_a_log_names_the_summary(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    result = _invoke("app", store, worker=_ExitedWorker(0, "", "Declined: out of scope"))
    assert result.worker_log is None
    assert any(
        "Nothing ran for CBN-011" in step and "the escalation summary" in step
        for step in result.recovery
    )


def _write_log(worker: _BusyWorker) -> None:
    assert worker.log_path is not None
    worker.log_path.parent.mkdir(parents=True, exist_ok=True)
    worker.log_path.write_text("[stdout] declined\n")


def test_invocation_worker_error_on_clean_unmoved_tree_says_nothing_ran(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    """CBN-284: a provider refusal before any work is not 'resume partial work'."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = FakeWorker()
    worker.on_send = lambda _msg: setattr(worker, "state", STATUS_ERROR)
    result = _invoke("app", store, worker=worker)
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert result.tree == []
    assert any("Nothing ran for CBN-011" in step for step in result.recovery)
    assert not any("Committed closure evidence is missing" in step for step in result.recovery)


def _invocation_failure_worker(repo: Path, failure: str):
    worker = FakeWorker()
    kwargs = {}
    if failure == "launch":

        def boom(*_args, **_kwargs) -> Session:
            raise WorkerError("model access denied")

        worker.launch = boom
    elif failure == "park":

        def park(_msg):
            (repo / ".flaitron/tasknote/CBN-011.md").write_text(
                _note("blocked", "input-needed — grant access")
            )

        worker.on_send = park
    elif failure == "timeout":
        kwargs["timeout"] = 0.01
    elif failure in {"interrupt", "unexpected"}:

        def interrupt(_msg):
            (repo / "partial.py").write_text("partial\n")
            if failure == "interrupt":
                raise KeyboardInterrupt
            raise RuntimeError("unexpected callback failure")

        worker.on_send = interrupt
    else:
        worker.on_send = lambda _msg: _close_worker(repo)

        def broken_card(_store, _run):
            raise UserError("card disk error")

        kwargs["publish_status"] = broken_card
    return worker, kwargs


@pytest.mark.parametrize(
    "failure", ["launch", "park", "timeout", "interrupt", "unexpected", "report"]
)
def test_invocation_failure_recovery_and_evidence(
    tmp_path: Path,
    caobunga_home: Path,
    failure: str,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker, kwargs = _invocation_failure_worker(repo, failure)
    result = _invoke("app", store, worker=worker, **kwargs)
    assert not result.successful
    assert result.task_id == "CBN-011"
    assert result.recovery
    assert result.provider == "codex" and result.model
    if failure == "launch":
        assert "model access denied" in result.detail
        assert any("model access" in step for step in result.recovery)
    elif failure == "park":
        assert result.park_reason == "input-needed — grant access"
        assert result.tasknote_status == "blocked"
        assert any("/ft-task CBN-011" in step for step in result.recovery)
    elif failure == "timeout":
        assert result.timeout_seconds == 0.01
        assert result.escalation_reason is Escalation.TIMEOUT
        assert result.shutdown == "returned"
        assert any("0.01s" in step for step in result.recovery)
    elif failure == "report":
        assert result.last_commit_sha == git_head(repo)
        assert len(result.run_records) == 1
        assert result.stop_reason == "report"
    else:
        assert result.tree and "?? partial.py" in result.tree
        assert result.shutdown == "returned"
        assert result.stop_reason == (
            "interrupted" if failure == "interrupt" else "orchestrator-error"
        )
        assert result.legacy_exit_code == (130 if failure == "interrupt" else 4)


def _press_ctrl_c() -> None:
    os.kill(os.getpid(), signal.SIGINT)


def _pressing_shutdown(worker: FakeWorker, presses: int) -> None:
    """`worker.shutdown` takes `presses` ^C before it returns (CBN-375)."""
    shutdown = worker.shutdown

    def pressed(session: Session) -> None:
        for _ in range(presses):
            _press_ctrl_c()
        shutdown(session)

    worker.shutdown = pressed


def _pressing_finish(monkeypatch: pytest.MonkeyPatch, presses: int) -> None:
    """`trace.finish`'s snapshot takes `presses` ^C before it reads (CBN-375)."""
    snapshot = InvocationTrace.snapshot

    def pressed(self: InvocationTrace) -> None:
        for _ in range(presses):
            _press_ctrl_c()
        snapshot(self)

    monkeypatch.setattr(InvocationTrace, "snapshot", pressed)


@pytest.mark.parametrize("second", ["shutdown", "finish"])
def test_repeat_interrupt_during_cleanup_still_writes_the_receipt(
    tmp_path: Path,
    caobunga_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    second: str,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: _press_ctrl_c())
    if second == "shutdown":
        _pressing_shutdown(worker, 1)
    else:
        _pressing_finish(monkeypatch, 1)
    before = signal.getsignal(signal.SIGINT)
    result = _invoke("app", store, worker=worker)
    assert result.status is InvocationStatus.INTERRUPTED
    assert result.stop_reason == "interrupted"
    assert result.legacy_exit_code == 130
    assert result.shutdown == "returned" and worker.deleted
    assert signal.getsignal(signal.SIGINT) is before
    err = capfd.readouterr().err
    assert loop.INTERRUPT_NOTICE in err
    assert "interrupt received again" in err


def test_third_interrupt_forces_the_exit(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: _press_ctrl_c())
    _pressing_shutdown(worker, 2)
    result = _invoke("app", store, worker=worker)
    # The third press aborted the shutdown, and the receipt still says so.
    assert worker.deleted == []
    assert result.shutdown == "failed"
    assert result.status is InvocationStatus.INCOMPLETE
    assert result.stop_reason == "interrupted"
    assert result.legacy_exit_code == 130


def test_third_interrupt_during_finish_escapes(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: _press_ctrl_c())
    _pressing_finish(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        loop.execute_invocation(
            "app", store=store, worker=worker, usage=_usage(), timeout=2, run_suite=_green_suite
        )


def test_first_interrupt_after_the_run_is_absorbed(
    tmp_path: Path,
    caobunga_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Once the run has ended, only the receipt write is left: a first press is
    absorbed, so the receipt the notice promises is written."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: _close_worker(repo))
    _pressing_finish(monkeypatch, 1)
    result = _invoke("app", store, worker=worker)
    assert result.successful
    assert loop.INTERRUPT_NOTICE in capfd.readouterr().err


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGTERM])
def test_hangup_unwinds_like_the_first_interrupt(
    tmp_path: Path,
    caobunga_home: Path,
    capfd: pytest.CaptureFixture[str],
    signum: signal.Signals,
) -> None:
    """A closed terminal or a `kill` stops the worker and writes the receipt (CBN-430)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: os.kill(os.getpid(), signum))
    before = signal.getsignal(signum)
    result = _invoke("app", store, worker=worker)
    assert result.status is InvocationStatus.INTERRUPTED
    assert result.stop_reason == "interrupted"
    assert result.legacy_exit_code == 130
    assert result.shutdown == "returned" and worker.deleted
    assert f"({signum.name})" in result.detail
    assert signal.getsignal(signum) is before
    assert loop.INTERRUPT_NOTICE in capfd.readouterr().err


def test_repeat_hangups_never_force_the_exit(tmp_path: Path, caobunga_home: Path) -> None:
    """A second SIGHUP and a forwarded SIGTERM land mid-shutdown: absorbed, not counted."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: os.kill(os.getpid(), signal.SIGHUP))
    shutdown = worker.shutdown

    def hung_up_again(session: Session) -> None:
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGHUP):
            os.kill(os.getpid(), signum)
        shutdown(session)

    worker.shutdown = hung_up_again
    result = _invoke("app", store, worker=worker)
    assert result.status is InvocationStatus.INTERRUPTED
    assert result.shutdown == "returned" and worker.deleted
    assert "(SIGHUP)" in result.detail


def test_hangup_then_two_presses_force_the_exit(tmp_path: Path, caobunga_home: Path) -> None:
    """A hangup is a first press: ^C twice more still forces the exit."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: os.kill(os.getpid(), signal.SIGHUP))
    _pressing_shutdown(worker, 2)
    result = _invoke("app", store, worker=worker)
    assert worker.deleted == []
    assert result.shutdown == "failed"
    assert result.stop_reason == "interrupted"
    assert "(SIGHUP)" in result.detail


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGHUP, signal.SIGTERM])
def test_interrupt_guard_keeps_an_inherited_sig_ign(signum: signal.Signals) -> None:
    previous = signal.signal(signum, signal.SIG_IGN)
    try:
        with loop._interrupt_guard():
            assert signal.getsignal(signum) is signal.SIG_IGN
    finally:
        signal.signal(signum, previous)


@pytest.mark.parametrize("every_write", [False, True])
def test_an_unwritten_receipt_is_named_not_a_raw_oserror(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch, every_write: bool
) -> None:
    """CBN-442: a full disk at the receipt write raises `ReceiptUnwritten`, exit 4.

    The terminal write alone failing leaves the last checkpoint `running` for
    the next run to reconcile (2026-10-29's three stranded legs); every write
    failing leaves no receipt at all (the eight legs spawned after the disk
    filled). Neither echoes an `invocation-record:` line for a file not written.
    """
    repo = _git_repo(tmp_path, TWO_CLOSED)
    store = Store(caobunga_home)
    _register(store, repo)
    write = Store.write_invocation

    def full(self: Store, result: InvocationResult) -> Path:
        if every_write or result.finished_at is not None:
            raise OSError(errno.ENOSPC, "No space left on device")
        return write(self, result)

    monkeypatch.setattr(Store, "write_invocation", full)
    lines: list[str] = []
    with pytest.raises(ReceiptUnwritten) as raised:
        loop.execute_invocation(
            "app", store=store, worker=FakeWorker(), usage=_usage(), timeout=2, echo=lines.append
        )
    assert raised.value.exit_code == 4
    assert raised.value.reason == "No space left on device"
    assert str(raised.value).startswith("invocation-record-unwritten: No space left on device\n")
    assert isinstance(raised.value.__cause__, OSError)
    # The run's own stop survives the failed write: the checkpoint's ENOSPC, or the empty PLAN.
    stopped = "OSError: [Errno 28] No space left" if every_write else "NoTaskError: no next task"
    assert f"\nstopped first: {stopped}" in str(raised.value)
    assert raised.value.record is None
    assert not any(line.startswith(("invocation:", "invocation-record:")) for line in lines)
    stranded = store.running_invocations("app")
    assert [r.status for r in stranded] == ([] if every_write else [InvocationStatus.RUNNING])


def test_an_unwritten_receipt_carries_the_last_run_record(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-442 review: a close's record survives its receipt, for the CLI to dump."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    write = Store.write_invocation

    def full(self: Store, result: InvocationResult) -> Path:
        if result.finished_at is not None:
            raise OSError(errno.ENOSPC, "No space left on device")
        return write(self, result)

    def close(_msg: str) -> None:
        (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
        _commit(repo, "feat: CBN-011 — close")

    monkeypatch.setattr(Store, "write_invocation", full)
    with pytest.raises(ReceiptUnwritten) as raised:
        loop.execute_invocation(
            "app",
            store=store,
            worker=_closing_client(FakeWorker(), close),
            usage=_usage(),
            timeout=2,
            run_suite=_green_suite,
            poll_interval=0,
        )
    (run,) = store.list_runs("app")
    record = raised.value.record
    assert record is not None
    assert (record.task_id, record.commit_sha) == (run.task_id, run.commit_sha)
    assert run.commit_sha is not None
    assert "stopped first" not in str(raised.value)


def test_invocation_drain_retains_every_task_reference(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    closes = iter([lambda: _close_worker(repo), lambda: _close_worker(repo, "CBN-012", TWO_CLOSED)])
    worker = _closing_client(FakeWorker(), lambda _msg: next(closes)())
    lines: list[str] = []
    result = _invoke("app", store, worker=worker, echo=lines.append)
    assert result.successful and result.approved_scope_finished
    first, second = store.list_runs("app")
    assert first.commit_sha and second.commit_sha
    tally = f"ran 2: CBN-011 closed {first.commit_sha[:8]}, CBN-012 closed {second.commit_sha[:8]}"
    assert any(line.startswith("invocation: complete") and tally in line for line in lines)
    assert result.authorized_tasks == ["CBN-011", "CBN-012"]
    assert result.remaining_tasks == []
    assert result.run_records == [
        str(path) for path in sorted(store.runs_dir("app").glob("*.yaml"))
    ]
    assert len(result.run_records) == 2
    assert len(worker.launches) == 2


def test_invocation_unmarked_tail_does_not_fail_completed_authorized_scope(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    plan = OPEN_PLAN.replace("**CBN-012** [medium] [unattended]", "**CBN-012** [medium]")
    after = CLOSED_PLAN.replace("**CBN-012** [medium] [unattended]", "**CBN-012** [medium]")
    repo = _git_repo(tmp_path, plan)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    result = _invoke(
        "app",
        store,
        worker=_closing_client(FakeWorker(), lambda _msg: _close_worker(repo, plan=after)),
    )
    assert result.successful and result.approved_scope_finished
    assert result.authorized_tasks == ["CBN-011"]
    assert result.denials[0].task_id == "CBN-012"
    assert result.stop_reason == "incomplete"  # legacy whole-queue echo vocabulary


def test_invocation_refuses_unpinned_work_and_preserves_initial_scope(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    plan = OPEN_PLAN.replace("**CBN-012** [medium] [unattended]", "**CBN-012** [medium]")
    repo = _git_repo(tmp_path, plan)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    worker = _closing_client(FakeWorker(), lambda _msg: _close_worker(repo))
    result = _invoke("app", store, worker=worker)
    assert result.stop_reason == "unpinned"
    assert result.task_id == "CBN-012"
    assert not result.successful
    assert result.authorized_tasks == ["CBN-011"]
    assert len(worker.launches) == 1
    assert any("who changed" in step for step in result.recovery)


def test_invocation_observe_remains_read_only(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.OBSERVE)
    worker = FakeWorker()
    result = _invoke("app", store, worker=worker)
    assert result.status.value == "observed"
    assert not worker.launches
    assert not (caobunga_home / "invocations").exists()
    assert not store.list_runs("app")


def _dead_pid() -> int:
    """A pid that was a real process and is now reaped."""
    proc = subprocess.Popen(["true"])  # noqa: S607
    proc.wait()
    return proc.pid


def _stale_receipt(
    store: Store, repo: Path, *, pid: int | None, project: str = "app", path: str | None = None
) -> InvocationResult:
    """A `running` checkpoint as an uncatchable death leaves it (CBN-376).

    Started a day ahead of the fixture's `init` commit, so only a commit dated
    after that counts as having landed since.
    """
    started = utc_now() + timedelta(days=1)
    result = InvocationResult(
        invocation_id=compact_utc(started),
        receipt_id=f"{compact_utc(started)}-{os.urandom(16).hex()}",
        project=project,
        started_at=started,
        pid=pid,
        stop_reason=StopReason.WORKER_START,
        path=path if path is not None else str(repo.resolve()),
        tier=ExecutionTier.GATED,
        task_id="CBN-011",
        authorized_tasks=["CBN-011"],
        remaining_tasks=["CBN-011"],
    )
    store.write_invocation(result)
    return result


def _commit_dated(repo: Path, message: str, when: str) -> None:
    _git(repo, "add", "-A")
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when},
    )


def test_stale_running_receipt_is_reconciled_to_abandoned(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-376: a dead run's receipt is rewritten and reported; the repo is only read."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stale = _stale_receipt(store, repo, pid=_dead_pid())
    (repo / "landed.py").write_text("landed\n")
    _commit_dated(repo, "feat: landed after the crash", (utc_now() + timedelta(days=2)).isoformat())
    (repo / ".flaitron/tasknote/CBN-011.md").write_text(
        "---\nstatus: in-progress\n---\n# CBN-011\n", encoding="utf-8"
    )
    (repo / "partial.py").write_text("partial\n")
    head, tree = git_head(repo), git_dirt(repo)
    lines: list[str] = []
    worker = FakeWorker()

    result = _invoke("app", store, worker=worker, echo=lines.append)

    # The dirty-tree refusal still stops this run, after the reconcile.
    assert result.stop_reason == "dirty-tree"
    assert not worker.launches
    assert (git_head(repo), git_dirt(repo)) == (head, tree)
    rewritten = store.load_invocation(stale.receipt_id)
    assert rewritten.status is InvocationStatus.ABANDONED
    assert rewritten.stop_reason == "process-gone"
    assert rewritten.finished_at is not None
    assert rewritten.legacy_exit_code is None
    assert not rewritten.successful and not rewritten.approved_scope_finished
    assert "last stage worker-start" in rewritten.detail
    assert "since started_at: 1 commit(s)" in rewritten.detail
    assert rewritten.head_sha == head
    assert rewritten.tree and any("partial.py" in line for line in rewritten.tree)
    assert rewritten.tasknote_status == "in-progress"
    assert any("may still be alive" in step for step in rewritten.recovery)
    stale_lines = [line for line in lines if line.startswith("stale-receipt")]
    assert stale_lines[0].startswith(f"stale-receipt: {stale.receipt_id} running since ")
    assert stale_lines[0].endswith("gone, last stage worker-start) → abandoned")
    assert "1 commit(s)" in stale_lines[1] and "feat: landed after the crash" in stale_lines[1]
    assert "partial.py" in stale_lines[2] and "(in-progress)" in stale_lines[2]
    assert stale_lines[3] == f"stale-receipt-record: {store.invocation_file(stale.receipt_id)}"
    # The new run's own receipt carries the pid the next reconcile reads.
    assert result.pid == os.getpid()


def test_stale_legacy_receipt_without_pid_reconciles_on_the_lock(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """A pre-CBN-376 receipt has no pid; holding the lock for its own path is the evidence."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stale = _stale_receipt(store, repo, pid=None)
    (repo / "partial.py").write_text("partial\n")
    lines: list[str] = []
    _invoke("app", store, worker=FakeWorker(), echo=lines.append)
    rewritten = store.load_invocation(stale.receipt_id)
    assert rewritten.status is InvocationStatus.ABANDONED
    assert "pid unrecorded" in rewritten.detail
    assert "0 commit(s) — nothing" in "\n".join(lines)
    assert "no open tasknote" in "\n".join(lines)


def test_stale_receipt_of_a_moved_slug_reads_its_own_repo(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """A dead pid is dead wherever the slug points now; evidence comes from the dead run's repo."""
    old = _git_repo(tmp_path / "old", OPEN_PLAN)
    new = _git_repo(tmp_path / "new", OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, new)
    stale = _stale_receipt(store, old, pid=_dead_pid())
    (new / "unrelated.py").write_text("unrelated\n")
    _commit_dated(new, "feat: unrelated", (utc_now() + timedelta(days=2)).isoformat())
    (new / "partial.py").write_text("partial\n")
    _invoke("app", store, worker=FakeWorker())
    rewritten = store.load_invocation(stale.receipt_id)
    assert rewritten.status is InvocationStatus.ABANDONED
    assert "since started_at: 0 commit(s)" in rewritten.detail
    assert rewritten.head_sha == git_head(old)


def test_stale_receipt_rewrite_failure_does_not_stop_the_run(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stale = _stale_receipt(store, repo, pid=_dead_pid())
    write = store.write_invocation

    def failing(result: InvocationResult) -> Path:
        if result.receipt_id == stale.receipt_id:
            raise OSError("disk full")
        return write(result)

    monkeypatch.setattr(store, "write_invocation", failing)
    (repo / "partial.py").write_text("partial\n")
    lines: list[str] = []
    result = _invoke("app", store, worker=FakeWorker(), echo=lines.append)
    assert result.stop_reason == "dirty-tree"
    assert f"stale-receipt: {stale.receipt_id} not rewritten: disk full" in lines
    assert store.load_invocation(stale.receipt_id) == stale


@pytest.mark.parametrize(
    "case", ["alive", "other-slug", "other-path", "terminal", "observe", "misnamed"]
)
def test_stale_reconcile_leaves_unproven_receipts_alone(
    tmp_path: Path, caobunga_home: Path, case: str
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    tier = ExecutionTier.OBSERVE if case == "observe" else ExecutionTier.GATED
    _register(store, repo, tier=tier)
    if case == "alive":
        stale = _stale_receipt(store, repo, pid=os.getppid())
    elif case == "other-slug":
        stale = _stale_receipt(store, repo, pid=_dead_pid(), project="other")
    elif case == "other-path":
        stale = _stale_receipt(store, repo, pid=None, path=str(tmp_path / "elsewhere"))
    else:
        stale = _stale_receipt(store, repo, pid=_dead_pid())
    if case == "terminal":
        stale = stale.model_copy(
            update={
                "status": InvocationStatus.REFUSED,
                "finished_at": utc_now(),
                "legacy_exit_code": 2,
            }
        )
        store.write_invocation(stale)
    misnamed = caobunga_home / "invocations" / "20261018T000000000Z-ab.yaml"
    if case == "misnamed":
        store.invocation_file(stale.receipt_id).rename(misnamed)
    (repo / "partial.py").write_text("partial\n")
    lines: list[str] = []
    _invoke("app", store, worker=FakeWorker(), echo=lines.append)
    if case == "misnamed":
        assert not store.invocation_file(stale.receipt_id).exists()
        assert store.load_invocation("20261018T000000000Z-ab") == stale
    else:
        assert store.load_invocation(stale.receipt_id) == stale
    assert not [line for line in lines if line.startswith("stale-receipt")]


def test_abandoned_receipt_has_a_finish_time_and_no_legacy_exit() -> None:
    trace_result = InvocationResult(
        invocation_id="20261018T000000000Z",
        receipt_id="20261018T000000000Z-ab",
        project="app",
        started_at=utc_now(),
    )
    data = trace_result.model_dump()
    with pytest.raises(ValueError, match="abandoned"):
        InvocationResult.model_validate({**data, "status": "abandoned"})
    with pytest.raises(ValueError, match="abandoned"):
        InvocationResult.model_validate(
            {**data, "status": "abandoned", "finished_at": utc_now(), "legacy_exit_code": 0}
        )
    InvocationResult.model_validate({**data, "status": "abandoned", "finished_at": utc_now()})


@pytest.mark.parametrize(
    ("tier", "stage"),
    [(ExecutionTier.OBSERVE, "observe"), (ExecutionTier.GATED, "project-read")],
)
def test_read_oserror_is_a_named_stop(
    tmp_path: Path,
    caobunga_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tier: ExecutionTier,
    stage: str,
) -> None:
    """CBN-326. The loop's direct reads are not under `wait.poll`: an `OSError` is a named stop."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=tier)

    def unreadable(_root: Path, **_kwargs: object) -> None:
        raise PermissionError("PLAN.md")

    monkeypatch.setattr("caobunga.loop.read_project", unreadable)
    result = _invoke("app", store, worker=FakeWorker())
    assert result.stop_reason == stage
    assert result.legacy_exit_code == 2
    assert "cannot read the Flaitron project" in result.detail
    with pytest.raises(UserError, match="cannot read the Flaitron project"):
        _run("app", store=store, worker=FakeWorker(), poll_interval=0, timeout=2)


def test_read_oserror_between_drained_tasks_keeps_the_run_record(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    reads: list[Path] = []

    def read_then_unreadable(root: Path, **kwargs: Any) -> ProjectRead:
        reads.append(root)
        if len(reads) > 1:
            raise PermissionError("PLAN.md")
        return read_project(root, **kwargs)

    monkeypatch.setattr("caobunga.loop.read_project", read_then_unreadable)
    worker = _closing_client(FakeWorker(), lambda _msg: _close_worker(repo))
    result = _invoke("app", store, worker=worker)
    assert result.status.value == "incomplete"
    assert result.stop_reason == "project-read"
    assert result.legacy_exit_code == 2
    assert len(result.run_records) == 1
    assert len(worker.launches) == 1


@pytest.mark.parametrize("repick", [False, True])
def test_invocation_shutdown_failure_survives_later_success(
    tmp_path: Path,
    caobunga_home: Path,
    repick: bool,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(
        store, repo, tier=ExecutionTier.PROCEED_ON_GREEN, providers=["claude_code", "grok_cli"]
    )
    closes = iter([lambda: _close_worker(repo), lambda: _close_worker(repo, "CBN-012", TWO_CLOSED)])
    if repick:
        worker = _UsageLimitThenCloseWorker(lambda: next(closes)())
    else:
        worker = _closing_client(FakeWorker(), lambda _msg: next(closes)())
    shutdown = worker.shutdown
    failed_sessions = []

    def fail_first(session: Session) -> None:
        shutdown(session)
        if not failed_sessions:
            failed_sessions.append(session.session_name)
            raise WorkerError("first shutdown failed")

    worker.shutdown = fail_first
    result = _invoke("app", store, worker=worker)
    assert len(result.run_records) == 2  # legacy dispatch continues as before
    assert result.remaining_tasks == []
    assert not result.successful
    assert result.shutdown == "failed"
    assert any(failed_sessions[0] in step for step in result.recovery)
    assert result.legacy_exit_code == 0


def test_invocation_uncommitted_plan_archive_is_not_closure(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = FakeWorker()

    def paper_close(_msg: str) -> None:
        (repo / ".flaitron/PLAN.md").write_text(CLOSED_PLAN)
        _archive_note(repo, "CBN-011")
        worker.state = STATUS_EXITED

    worker.on_send = paper_close
    before = git_head(repo)
    result = _invoke("app", store, worker=worker)
    assert not result.successful
    assert result.head_sha == before
    assert result.last_commit_sha is None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.tree
    assert any("Committed closure evidence is missing" in step for step in result.recovery)


def test_invocation_park_commit_is_not_a_closure_sha(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    before = git_head(repo)

    def commit_park(_msg: str) -> None:
        (repo / ".flaitron/tasknote/CBN-011.md").write_text(
            _note("blocked", "input-needed — access")
        )
        _commit(repo, "chore: preserve parked work")

    worker = _closing_client(FakeWorker(), commit_park)
    result = _invoke("app", store, worker=worker)
    run = store.last_run("app")
    assert run is not None and run.outcome is Outcome.BLOCKED
    assert run.commit_sha == result.head_sha != before
    assert result.last_commit_sha is None
    assert not result.successful
    assert any("/ft-task CBN-011" in step for step in result.recovery)


@pytest.mark.parametrize(
    ("verdict", "confirm_and_close"), [("De-scope", True), ("Re-scope", False)]
)
def test_invocation_descope_park_recovers_by_confirm_and_close(
    tmp_path: Path, caobunga_home: Path, verdict: str, confirm_and_close: bool
) -> None:
    """CBN-294: a De-scope park has nothing to resume; any other park still resumes."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = FakeWorker()

    def park(_msg: str) -> None:
        note = _note("blocked", "drift — already delivered elsewhere")
        (repo / ".flaitron/tasknote/CBN-011.md").write_text(f"{note}\n**Verdict:** {verdict}\n")

    worker.on_send = park
    result = _invoke("app", store, worker=worker)
    assert result.park_reason == "drift — already delivered elsewhere"
    steps = " ".join(result.recovery)
    assert ("confirm and close the row" in steps) is confirm_and_close
    assert ("resume CBN-011 attended with /ft-task CBN-011" in steps) is not confirm_and_close


@pytest.mark.parametrize("unreadable", ["none", "missing", "not-utf8"])
def test_parked_descoped_is_false_without_a_readable_note(tmp_path: Path, unreadable: str) -> None:
    """CBN-294: no readable tasknote falls back to the generic resume text."""
    run = RunRecord(
        project="app",
        task_id="CBN-011",
        outcome=Outcome.BLOCKED,
        closed_at=utc_now(),
        summary="parked",
    )
    path = None if unreadable == "none" else str(tmp_path / "CBN-011.md")
    if unreadable == "not-utf8":
        assert path is not None
        Path(path).write_bytes(b"\xff\xfe**Verdict:** De-scope")
    assert _parked_descoped(run, path) is False


def test_invocation_worker_exited_with_moved_head_keeps_closure_missing_text(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-293: a commit means work happened, even when the tree is clean."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def commit_unrelated() -> None:
        (repo / "other.py").write_text("x\n")
        _commit(repo, "chore: unrelated")

    result = _invoke("app", store, worker=_ExitedWorker(0, "", "", on_send=commit_unrelated))
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.tree == []
    assert any("Committed closure evidence is missing" in step for step in result.recovery)
    assert not any("Nothing ran" in step for step in result.recovery)


def test_invocation_worker_exited_with_open_tasknote_keeps_closure_missing_text(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-293: an open tasknote is partial work to resume, even on a clean unmoved tree."""
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _note("in-progress")})
    store = Store(caobunga_home)
    _register(store, repo)
    result = _invoke("app", store, worker=_ExitedWorker(0, "", ""))
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.tree == []
    assert result.tasknote_path is not None
    assert any("Committed closure evidence is missing" in step for step in result.recovery)
    assert not any("Nothing ran" in step for step in result.recovery)


def test_no_recovery_stops_are_disjoint_from_recovery_steps() -> None:
    """CBN-407: a benign stop reason never doubles as a `_RECOVERY` key."""
    assert not _NO_RECOVERY_STOPS & set(_RECOVERY)


# Wire vocabulary from the existing invocation writers, including checkpoints.
_STOP_REASON_VALUES = {
    "active-run",
    "baseline",
    "complete",
    "denied",
    "dirty-tree",
    "disabled",
    "empty",
    "gated",
    "git-unreadable",
    "head-lost",
    "incomplete",
    "interrupted",
    "landing",
    "not-a-repo",
    "observe",
    "orchestrator-error",
    "outcome",
    "process-gone",
    "project-read",
    "provider",
    "registry",
    "report",
    "running",
    "suite",
    "timeout-config",
    "unpinned",
    "worker-start",
    "worktree",
}


def test_stop_reason_wire_vocabulary() -> None:
    assert {reason.value for reason in StopReason} == _STOP_REASON_VALUES
    assert {reason.value for reason in _NO_RECOVERY_STOPS} == {
        "empty",
        "complete",
        "incomplete",
        "gated",
        "denied",
    }


@pytest.mark.parametrize("value", sorted(_STOP_REASON_VALUES))
def test_stop_reason_json_round_trip(value: str) -> None:
    receipt = InvocationResult(
        invocation_id="invocation",
        receipt_id="receipt",
        project="app",
        started_at=utc_now(),
        stop_reason=StopReason(value),
    )
    # Old on-disk receipts contain plain strings, as do new serialized receipts.
    assert receipt.model_dump(mode="json")["stop_reason"] == value
    restored = InvocationResult.model_validate_json(receipt.model_dump_json())
    assert restored.stop_reason is StopReason(value)


def test_stop_reason_default_and_invalid_input() -> None:
    receipt = InvocationResult(
        invocation_id="invocation",
        receipt_id="receipt",
        project="app",
        started_at=utc_now(),
    )
    assert receipt.stop_reason is StopReason.RUNNING
    with pytest.raises(ValueError, match="stop_reason"):
        InvocationResult.model_validate({**receipt.model_dump(), "stop_reason": "outocme"})


def _dead_isolated_receipt(store: Store, repo: Path) -> tuple[InvocationResult, Path]:
    """A dead run's receipt naming the worktree it was running in (CBN-EPIC-450.6)."""
    context = start(repo, "CBN-011", git_head(repo) or "")
    (context.worker_root / "partial.py").write_text("partial\n")
    _commit_dated(
        context.worker_root, "wip: on the branch", (utc_now() + timedelta(days=2)).isoformat()
    )
    stale = _stale_receipt(store, repo, pid=_dead_pid())
    stale.worktrees.append(
        WorktreeRun(
            branch=context.branch,
            worker_root=str(context.worker_root),
            execution_root=str(context.execution_root),
            landing_root=str(context.landing_root),
            base_sha=context.base_sha,
        )
    )
    store.write_invocation(stale)
    return stale, context.worker_root


def test_stale_reconcile_reads_a_dead_isolated_runs_worktree(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    main_head = git_head(repo)
    stale, wt = _dead_isolated_receipt(store, repo)

    (rewritten,) = reconcile_stale(store, store.get("app"), "own")

    assert rewritten.status is InvocationStatus.ABANDONED
    assert rewritten.head_sha == git_head(wt) != main_head
    assert "Landed on wt-CBN-011 since its base: 1 commit(s)" in rewritten.detail
    assert rewritten.worktrees[0].landing is LandingState.RETAINED
    assert any(
        step.startswith("Retained, not landed: branch wt-CBN-011") for step in rewritten.recovery
    )
    assert store.load_invocation(stale.receipt_id) == rewritten


def test_stale_receipts_report_reads_landed_from_the_worktree(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    _dead_isolated_receipt(store, repo)
    record = store.get("app")
    (item,) = stale_receipts(record, running_verdicts(store, "app"))
    assert item.landed is not None
    assert [line.split(" ", 1)[1] for line in item.landed] == ["wip: on the branch"]


def test_stale_reconcile_of_a_removed_worktree_reads_main_but_retains_the_branch(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Never landed, so the branch may hold the only copy even with the directory gone."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    _stale, wt = _dead_isolated_receipt(store, repo)
    _git(repo, "worktree", "remove", "--force", str(wt))

    (rewritten,) = reconcile_stale(store, store.get("app"), "own")

    assert rewritten.head_sha == git_head(repo)
    assert "Landed on HEAD since" in rewritten.detail
    assert rewritten.worktrees[0].landing is LandingState.RETAINED
    assert any(
        step.startswith("Retained, not landed: branch wt-CBN-011") for step in rewritten.recovery
    )


def test_stale_reconcile_of_a_run_that_died_after_landing_claims_nothing(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Landing settles the entry at once, so a death before the record write is not unlanded."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stale, _wt = _dead_isolated_receipt(store, repo)
    stale.worktrees[0].landing = LandingState.LANDED
    stale.worktrees[0].landed_sha = git_head(repo)
    store.write_invocation(stale)

    (rewritten,) = reconcile_stale(store, store.get("app"), "own")

    assert "Landed on HEAD since" in rewritten.detail
    assert rewritten.worktrees[0].landing is LandingState.LANDED
    assert not any(step.startswith("Retained") for step in rewritten.recovery)


def test_receipt_without_worktrees_loads_from_a_legacy_file(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    stale = _stale_receipt(store, repo, pid=None)
    path = store.invocation_file(stale.receipt_id)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.pop("worktrees", None)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert "worktrees" not in yaml.safe_load(path.read_text(encoding="utf-8"))
    assert store.load_invocation(stale.receipt_id).worktrees == []
