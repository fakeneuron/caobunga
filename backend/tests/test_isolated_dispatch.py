"""Isolated dispatch: run in a `wt-<ID>` worktree, land by fast-forward (CBN-EPIC-450.4).

Every case drives `loop.execute_invocation` against real temp git repos. The
fake worker writes where it was launched (`cwd`), so a close lands in the
worktree and main only moves when the loop fast-forwards it.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    CLOSED_PLAN,
    OPEN_PLAN,
    TWO_CLOSED,
    FakeWorker,
    _close_worker,
    _closing_client,
    _commit,
    _dirty,
    _ExitedWorker,
    _git,
    _git_repo,
    _green_suite,
    _not_green_suite,
    _note,
    _usage,
    _UsageLimitThenCloseWorker,
)
from test_worktree import allow_file_submodules  # noqa: F401

from caobunga import landing as landing_mod
from caobunga import loop, presence
from caobunga import worktree as worktree_mod
from caobunga.errors import GitUnreadable
from caobunga.git import git_head as real_git_head
from caobunga.landing import checked_out, cleanup, land
from caobunga.models import (
    ExecutionTier,
    InvocationResult,
    InvocationStatus,
    LandingState,
    Outcome,
    RegistryRecord,
    utc_now,
)
from caobunga.store import Store
from caobunga.suite import SuiteRun
from caobunga.worktree import start, worktree_path

TASK = "CBN-011"


def _head(repo: Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branches(repo: Path) -> list[str]:
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line.startswith("wt-")]


def _register(
    store: Store,
    repo: Path,
    *,
    tier: ExecutionTier = ExecutionTier.GATED,
    bootstrap: list[str] | None = None,
) -> None:
    store.register(
        RegistryRecord(
            id="app",
            path=str(repo.resolve()),
            tier=tier,
            registered_at=utc_now(),
            bootstrap=bootstrap,
            isolate=True,
        )
    )


def _cwd(fake: FakeWorker) -> Path:
    assert fake.created is not None
    return Path(str(fake.created["cwd"]))


def _wt(repo: Path, task_id: str = TASK) -> Path:
    return worktree_path(repo.resolve(), task_id)


def _invoke(
    store: Store,
    worker: object,
    *,
    echo: list[str] | None = None,
    run_suite: Callable[[Path, Store], SuiteRun] = _green_suite,
) -> InvocationResult:
    return loop.execute_invocation(
        "app",
        store=store,
        worker=worker,  # type: ignore[arg-type]
        poll_interval=0,
        timeout=2,
        echo=echo.append if echo is not None else None,
        usage=_usage(),
        run_suite=run_suite,
    )


def _recording_suite(roots: list[Path]) -> Callable[[Path, Store], SuiteRun]:
    def run(root: Path, store: Store) -> SuiteRun:
        roots.append(root)
        return _green_suite(root, store)

    return run


def _assert_retained(repo: Path, receipt: InvocationResult, task_id: str = TASK) -> None:
    assert _wt(repo, task_id).is_dir()
    assert f"wt-{task_id}" in _branches(repo)
    assert any(
        step.startswith(f"Retained, not landed: branch wt-{task_id}") for step in receipt.recovery
    ), receipt.recovery


# --- routing and landing ---


def test_route_runs_in_the_worktree_and_lands_by_fast_forward(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    roots: list[Path] = []
    main_heads: list[str] = []
    lines: list[str] = []

    def close(_message: str) -> None:
        _close_worker(_cwd(fake))
        main_heads.append(_head(repo))

    with _closing_client(fake, close) as worker:
        receipt = _invoke(store, worker, echo=lines, run_suite=_recording_suite(roots))

    wt = _wt(repo)
    assert _cwd(fake) == wt
    # Baseline and the post-close suite both measured the worktree.
    assert roots == [wt, wt]
    # Main sat still while the worker ran.
    assert main_heads == [base]
    run = store.list_runs("app")[-1]
    assert run.outcome is Outcome.CLOSED
    assert _head(repo) == run.commit_sha
    assert (repo / ".flaitron" / "PLAN.md").read_text(encoding="utf-8") == CLOSED_PLAN
    assert not wt.exists()
    assert _branches(repo) == []
    assert receipt.stop_reason == "gated"
    assert receipt.successful
    assert not any(step.startswith("Retained") for step in receipt.recovery)
    assert f"worktree: wt-{TASK} at {wt}" in lines
    assert any(line.startswith(f"landing: wt-{TASK} fast-forwarded") for line in lines)


@pytest.mark.usefixtures("allow_file_submodules")
def test_lands_and_removes_a_worktree_with_an_initialized_submodule(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    core = tmp_path / "core"
    core.mkdir()
    (core / "SPEC.md").write_text("pinned\n", encoding="utf-8")
    _git(core, "init")
    _commit(core, "pinned")
    _git(repo, "submodule", "add", str(core), "vendor/core")
    _commit(repo, "add core")
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    seen: list[bool] = []

    def close(_message: str) -> None:
        seen.append((_cwd(fake) / "vendor" / "core" / "SPEC.md").is_file())
        _close_worker(_cwd(fake))

    with _closing_client(fake, close) as worker:
        receipt = _invoke(store, worker)

    assert seen == [True]
    assert _head(repo) == store.list_runs("app")[-1].commit_sha
    assert not _wt(repo).exists()
    assert _branches(repo) == []
    assert receipt.successful


def test_lands_each_close_of_a_drain_and_continues(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    plans = iter([("CBN-011", CLOSED_PLAN), ("CBN-012", TWO_CLOSED)])

    def close(_message: str) -> None:
        task_id, plan = next(plans)
        _close_worker(_cwd(fake), task_id, plan)

    with _closing_client(fake, close) as worker:
        receipt = _invoke(store, worker)

    cwds = [Path(str(launch["cwd"])) for launch in fake.launches]
    assert cwds == [_wt(repo, "CBN-011"), _wt(repo, "CBN-012")]
    runs = store.list_runs("app")
    assert [run.outcome for run in runs] == [Outcome.CLOSED, Outcome.CLOSED]
    assert _head(repo) == runs[-1].commit_sha
    assert _branches(repo) == []
    assert receipt.stop_reason == "complete"
    assert receipt.status is InvocationStatus.COMPLETE


# --- readiness ---


def test_readiness_main_dirt_refuses_before_any_worktree(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    _dirty(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    receipt = _invoke(store, fake)
    assert receipt.stop_reason == "dirty-tree"
    assert fake.launches == []
    assert not _wt(repo).exists()
    assert _branches(repo) == []


def test_readiness_failed_bootstrap_refuses_saves_its_log_and_retains(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, bootstrap=[sys.executable, "-c", "print('nope'); raise SystemExit(3)"])
    fake = FakeWorker()
    lines: list[str] = []
    receipt = _invoke(store, fake, echo=lines)
    assert receipt.stop_reason == "worktree"
    assert receipt.status is InvocationStatus.REFUSED
    assert fake.launches == []
    assert "exited 3 in" in receipt.detail
    assert "Bootstrap log: " in receipt.detail
    log = Path(receipt.detail.split("Bootstrap log: ", 1)[1].strip())
    assert "nope" in log.read_text(encoding="utf-8")
    assert any(line.startswith("bootstrap: exited 3") for line in lines)
    assert "execution worktree could not be readied" in receipt.recovery[0]
    _assert_retained(repo, receipt)


def test_readiness_bootstrap_dirt_refuses_and_retains(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    code = "open('stray.txt', 'w').write('x')"
    _register(store, repo, bootstrap=[sys.executable, "-c", code])
    fake = FakeWorker()
    receipt = _invoke(store, fake)
    assert receipt.stop_reason == "worktree"
    assert "uncommitted change(s)" in receipt.detail
    assert fake.launches == []
    _assert_retained(repo, receipt)


def test_readiness_a_red_baseline_measures_the_worktree_and_retains(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    roots: list[Path] = []
    red = _not_green_suite("red", baseline_green=False)

    def suite(root: Path, s: Store) -> SuiteRun:
        roots.append(root)
        return red(root, s)

    receipt = _invoke(store, fake, run_suite=suite)
    assert receipt.stop_reason == "baseline"
    assert roots == [_wt(repo)]
    assert fake.launches == []
    _assert_retained(repo, receipt)


# --- never landed ---


def test_retain_a_worker_failure_and_read_its_evidence_from_the_worktree(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    wt = _wt(repo)

    def partial() -> None:
        (wt / "partial.txt").write_text("half\n", encoding="utf-8")
        _commit(wt, "wip")

    worker = _ExitedWorker(1, "crashed", on_send=partial)
    receipt = _invoke(store, worker)
    assert store.list_runs("app")[-1].outcome is Outcome.FAILED
    assert _head(repo) == base
    # The receipt's HEAD is the worktree's, so "nothing ran" is not claimed.
    assert receipt.head_sha == _head(wt)
    assert receipt.head_sha != base
    assert not any(step.startswith("Nothing ran") for step in receipt.recovery)
    _assert_retained(repo, receipt)


def test_retain_a_de_scoped_close_is_never_landed(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake), descoped=True)) as worker:
        receipt = _invoke(store, worker)
    assert store.list_runs("app")[-1].outcome is Outcome.DE_SCOPED
    assert _head(repo) == base
    assert receipt.stop_reason == "outcome"
    _assert_retained(repo, receipt)


def test_retain_a_red_suite_close_is_never_landed(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake))) as worker:
        receipt = _invoke(store, worker, run_suite=_not_green_suite("red"))
    assert store.list_runs("app")[-1].outcome is Outcome.CLOSED
    assert _head(repo) == base
    assert receipt.stop_reason == "suite"
    _assert_retained(repo, receipt)


def test_unlanded_dirty_worktree_stops_landing(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(_cwd(fake))
        _dirty(_cwd(fake))

    with _closing_client(fake, close) as worker:
        receipt = _invoke(store, worker)
    assert store.list_runs("app")[-1].outcome is Outcome.CLOSED
    assert _head(repo) == base
    assert receipt.stop_reason == "landing"
    assert "the worktree has 1 uncommitted change(s)" in receipt.detail
    assert receipt.status is InvocationStatus.INCOMPLETE
    assert "did not fast-forward" in receipt.recovery[0]
    _assert_retained(repo, receipt)


def test_diverge_main_moved_during_the_run_stops_a_drain_on_landing(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(_cwd(fake))
        (repo / "operator.txt").write_text("op\n", encoding="utf-8")
        _commit(repo, "operator commit")

    with _closing_client(fake, close) as worker:
        receipt = _invoke(store, worker)
    operator = _head(repo)
    assert len(fake.launches) == 1
    assert receipt.stop_reason == "landing"
    assert "the main checkout moved" in receipt.detail
    assert _head(repo) == operator
    _assert_retained(repo, receipt)


def test_dirty_main_at_landing_is_not_landed(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(_cwd(fake))
        _dirty(repo)

    with _closing_client(fake, close) as worker:
        receipt = _invoke(store, worker)
    assert receipt.stop_reason == "landing"
    assert "the main checkout has 1 uncommitted change(s)" in receipt.detail
    assert _head(repo) == base
    _assert_retained(repo, receipt)


# --- late commits ---


def _late_commit_suite(
    fake: FakeWorker, verdicts: list[str]
) -> tuple[Callable[[Path, Store], SuiteRun], list[Path]]:
    """Baseline green; the post-close suite commits in the worktree (a worker still
    writing), then each later measure answers the next verdict."""
    roots: list[Path] = []

    def run(root: Path, store: Store) -> SuiteRun:
        roots.append(root)
        if len(roots) == 2:
            (_cwd(fake) / "late.txt").write_text("late\n", encoding="utf-8")
            _commit(_cwd(fake), "chore: late rotation")
            return _green_suite(root, store)
        if len(roots) > 2 and verdicts.pop(0) != "green":
            return _not_green_suite("red", baseline_green=False)(root, store)
        return _green_suite(root, store)

    return run, roots


def test_late_commit_during_the_suite_is_re_measured_then_landed(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    suite, roots = _late_commit_suite(fake, ["green"])
    lines: list[str] = []
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake))) as worker:
        receipt = _invoke(store, worker, echo=lines, run_suite=suite)
    assert len(roots) == 3
    assert any("re-measuring" in line for line in lines)
    assert (repo / "late.txt").is_file()
    assert receipt.successful


def test_late_commit_re_measure_runs_under_the_heartbeat(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-456: the re-measure gets the close wait's start and a `suite` stage."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    suite, _roots = _late_commit_suite(fake, ["green"])
    starts: list[float | None] = []
    stages: list[str] = []

    class Recording:
        def __init__(self, _echo: Callable[[str], None] | None, started: float | None) -> None:
            starts.append(started)

        def __enter__(self) -> Recording:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def __setattr__(self, name: str, value: object) -> None:
            if name == "stage":
                stages.append(str(value))

    monkeypatch.setattr("caobunga.loop.SuiteHeartbeat", Recording)
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake))) as worker:
        receipt = _invoke(store, worker, run_suite=suite)
    assert receipt.successful
    (started,) = starts
    assert isinstance(started, float)
    assert stages == ["suite"]


def test_late_commit_re_measured_red_is_not_landed(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    suite, roots = _late_commit_suite(fake, ["red"])
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake))) as worker:
        receipt = _invoke(store, worker, run_suite=suite)
    assert len(roots) == 3
    run = store.list_runs("app")[-1]
    assert run.suite is not None
    assert run.suite.verdict.value == "red"
    assert _head(repo) == base
    assert receipt.stop_reason == "suite"
    _assert_retained(repo, receipt)


# --- interruption ---


def test_interrupt_mid_run_retains_and_the_receipt_names_it(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def interrupt(_message: str) -> None:
        raise KeyboardInterrupt

    with _closing_client(fake, interrupt) as worker:
        receipt = _invoke(store, worker)
    assert receipt.stop_reason == "interrupted"
    assert _head(repo) == base
    _assert_retained(repo, receipt)


# --- the CBN-213 refusal pivot ---


def test_pivot_re_picks_in_the_same_clean_unmoved_worktree(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(_wt(repo)))
    receipt = _invoke(store, worker)
    assert worker.launch_count == 2
    assert worker.providers[0] != worker.providers[1]
    assert store.list_runs("app")[-1].outcome is Outcome.CLOSED
    assert _head(repo) == store.list_runs("app")[-1].commit_sha
    assert receipt.successful


@pytest.mark.parametrize("mess", ["dirt", "commit"])
def test_pivot_refused_when_the_worktree_holds_work(
    tmp_path: Path, caobunga_home: Path, mess: str
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    wt = _wt(repo)

    def leave_work() -> None:
        _dirty(wt)
        if mess == "commit":
            _commit(wt, "wip")

    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(wt), dirty=leave_work)
    receipt = _invoke(store, worker)
    assert worker.launch_count == 1
    assert store.list_runs("app")[-1].outcome is Outcome.FAILED
    assert _head(repo) == base
    _assert_retained(repo, receipt)


# --- landing primitives ---


def test_land_refuses_a_sha_that_does_not_descend_from_the_base(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    context = start(repo, TASK, _head(repo))
    _git(context.worker_root, "checkout", "--orphan", "other")
    _commit(context.worker_root, "unrelated root")
    reason = land(context, _head(context.worker_root), ref=checked_out(repo))
    assert reason is not None
    assert "does not descend from the base" in reason


def test_recovery_cleanup_refuses_a_worktree_main_does_not_contain(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    context = start(repo, TASK, _head(repo))
    _close_worker(context.worker_root)
    reason = cleanup(context, _head(context.worker_root))
    assert reason is not None
    assert "not in the main checkout's history" in reason
    assert context.worker_root.is_dir()


def test_recovery_cleanup_refuses_a_dirty_or_moved_worktree(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    context = start(repo, TASK, _head(repo))
    base = _head(repo)
    _dirty(context.worker_root)
    assert "not clean" in (cleanup(context, base) or "")
    assert "no longer" in (cleanup(context, "0" * 40) or "")
    assert "no longer" in (land(context, "0" * 40, ref=checked_out(repo)) or "")
    assert "uncommitted change(s)" in (land(context, base, ref=checked_out(repo)) or "")
    assert "not on a branch" in (land(context, base, ref=None) or "")


def test_recovery_a_failed_cleanup_keeps_the_landing_and_names_the_leftover(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    monkeypatch.setattr("caobunga.landing.cleanup", lambda _ctx, _sha: "simulated refusal")
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake))) as worker:
        receipt = _invoke(store, worker, echo=lines)
    assert _head(repo) == store.list_runs("app")[-1].commit_sha
    assert receipt.stop_reason == "gated"
    assert any("landed on the main checkout" in step for step in receipt.recovery)
    assert any("not removed — simulated refusal" in line for line in lines)
    assert not any(step.startswith("Retained, not landed") for step in receipt.recovery)


def test_unlanded_when_git_hangs_while_landing(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo)

    def hang(_ctx: object, _sha: str) -> str | None:
        raise GitUnreadable(repo)

    monkeypatch.setattr("caobunga.landing.land", lambda c, s, **_kw: hang(c, s))
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake))) as worker:
        receipt = _invoke(store, worker)
    assert receipt.stop_reason == "landing"
    assert "git did not answer while landing" in receipt.detail
    assert _head(repo) == base
    _assert_retained(repo, receipt)


# --- review round 1 (CBN-EPIC-450.4) ---


def test_unlanded_a_gated_green_de_scope_stops_landing_not_finished(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake), descoped=True)) as worker:
        receipt = _invoke(store, worker)
    assert receipt.stop_reason == "landing"
    assert "de-scoped close not landed" in receipt.detail
    assert receipt.status is InvocationStatus.INCOMPLETE
    assert not receipt.successful
    assert _head(repo) == base
    _assert_retained(repo, receipt)


def test_readiness_refuses_the_candidates_own_uncommitted_note(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """The presence gate exempts it; a worktree would not carry it."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / ".flaitron" / "tasknote" / f"{TASK}.md").write_text(_note("starter"), encoding="utf-8")
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    receipt = _invoke(store, fake)
    assert receipt.stop_reason == "worktree"
    assert "a worktree would not carry" in receipt.detail
    assert fake.launches == []
    assert not _wt(repo).exists()


def test_diverge_main_switched_branch_is_not_landed(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(_cwd(fake))
        _git(repo, "switch", "-c", "scratch")

    with _closing_client(fake, close) as worker:
        receipt = _invoke(store, worker)
    assert receipt.stop_reason == "landing"
    assert "switched from refs/heads/" in receipt.detail
    assert _head(repo) == base
    _assert_retained(repo, receipt)


@pytest.mark.usefixtures("allow_file_submodules")
def test_lands_a_submodule_bump_and_leaves_main_clean(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    core = tmp_path / "core"
    core.mkdir()
    (core / "SPEC.md").write_text("v1\n", encoding="utf-8")
    _git(core, "init")
    _commit(core, "v1")
    _git(repo, "submodule", "add", str(core), "vendor/core")
    _commit(repo, "add core")
    (core / "SPEC.md").write_text("v2\n", encoding="utf-8")
    _commit(core, "v2")
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def bump_and_close(_message: str) -> None:
        sub = _cwd(fake) / "vendor" / "core"
        _git(sub, "fetch", "-q", "origin")
        _git(sub, "checkout", "-q", "FETCH_HEAD")
        _close_worker(_cwd(fake))

    with _closing_client(fake, bump_and_close) as worker:
        receipt = _invoke(store, worker)
    assert receipt.successful
    assert _head(repo) == store.list_runs("app")[-1].commit_sha
    assert (repo / "vendor" / "core" / "SPEC.md").read_text(encoding="utf-8") == "v2\n"
    status = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "status", "--porcelain"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""


def test_suite_red_with_an_unreadable_worktree_head_stops_suite_not_landing(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    real = real_git_head
    wt = _wt(repo)
    closed = {"done": False}

    def flaky(root: Path) -> str | None:
        """The worktree HEAD goes unreadable once the worker has closed."""
        if closed["done"] and Path(root) == wt:
            raise GitUnreadable(root)
        return real(root)

    def close(_message: str) -> None:
        _close_worker(_cwd(fake))
        closed["done"] = True

    monkeypatch.setattr("caobunga.loop.git_head", flaky)
    fake = FakeWorker()
    with _closing_client(fake, close) as worker:
        receipt = _invoke(store, worker, run_suite=_not_green_suite("red"))
    assert receipt.stop_reason == "suite"
    _assert_retained(repo, receipt)


# --- review round 2 (CBN-EPIC-450.4) ---


def test_readiness_a_baseline_that_dirties_the_worktree_refuses(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def dirtying(root: Path, s: Store) -> SuiteRun:
        (root / "coverage.xml").write_text("<x/>\n", encoding="utf-8")
        return _green_suite(root, s)

    receipt = _invoke(store, fake, run_suite=dirtying)
    assert receipt.stop_reason == "worktree"
    assert "the baseline suite left 1 uncommitted change(s)" in receipt.detail
    assert fake.launches == []
    _assert_retained(repo, receipt)


def test_unlanded_a_hang_during_the_merge_says_main_may_have_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    context = start(repo, TASK, _head(repo))
    _close_worker(context.worker_root)
    sha = _head(context.worker_root)
    real = landing_mod.git_run

    def hang_on_merge(root: Path, *args: str, timeout: float = 30.0) -> str | None:
        if args[:1] == ("merge",):
            raise GitUnreadable(root)
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(landing_mod, "git_run", hang_on_merge)
    reason = land(context, sha, ref=checked_out(repo))
    assert reason is not None
    assert "main may already be at" in reason


# --- review round 3 (CBN-EPIC-450.4) ---


def test_readiness_a_detached_main_refuses_before_any_worktree(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    _git(repo, "checkout", "-q", "--detach")
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    receipt = _invoke(store, fake)
    assert receipt.stop_reason == "worktree"
    assert "not on a branch" in receipt.detail
    assert fake.launches == []
    assert not _wt(repo).exists()
    assert _branches(repo) == []


@pytest.mark.usefixtures("allow_file_submodules")
def test_recovery_cleanup_sees_submodule_edits_an_ignore_setting_hides(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    core = tmp_path / "core"
    core.mkdir()
    (core / "SPEC.md").write_text("v1\n", encoding="utf-8")
    _git(core, "init")
    _commit(core, "v1")
    _git(repo, "submodule", "add", str(core), "vendor/core")
    _git(repo, "config", "-f", ".gitmodules", "submodule.vendor/core.ignore", "dirty")
    _commit(repo, "add core, ignore dirty")
    context = start(repo, TASK, _head(repo))
    (context.worker_root / "vendor" / "core" / "SPEC.md").write_text("edit\n", encoding="utf-8")
    sha = _head(context.worker_root)
    _git(repo, "merge", "--ff-only", sha)
    reason = cleanup(context, sha)
    assert reason is not None
    assert "not clean" in reason
    assert context.worker_root.is_dir()


# --- parks and retained work (CBN-EPIC-450.5) ---

EPIC_PLAN = """# sample

## High

- [ ] **CBN-EPIC-20** [heavy] | epic — tracking row.
  - [ ] **CBN-EPIC-20.2** [medium] [unattended] | child — child work.
  - [ ] **CBN-EPIC-20.N** [medium] [unattended] | epic audit — audit work.
- [ ] **CBN-031** [medium] [unattended] | dependent — needs the child. Blocked by [[CBN-EPIC-20.2]].
- [ ] **CBN-032** [medium] [unattended] | unrelated — other work.

## Completed

(none)
"""


def _task_of(message: str) -> str:
    return next(word for word in message.split() if word.startswith("CBN-"))


def _park_in(root: Path, task_id: str, *, commit: bool = True) -> None:
    notes = root / ".flaitron" / "tasknote"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / f"{task_id}.md").write_text(_note("blocked", "drift — plan moved"), encoding="utf-8")
    if commit:
        _commit(root, f"chore: park {task_id}")


def _close_in(root: Path, task_id: str) -> None:
    plan = root / ".flaitron" / "PLAN.md"
    text = plan.read_text(encoding="utf-8").replace(f"- [ ] **{task_id}**", f"- [x] **{task_id}**")
    _close_worker(root, task_id, text)


def _drain(
    tmp_path: Path,
    caobunga_home: Path,
    act: Callable[[FakeWorker, str], None],
    *,
    plan: str = OPEN_PLAN,
    echo: list[str] | None = None,
    run_suite: Callable[[Path, Store], SuiteRun] = _green_suite,
) -> tuple[Path, Store, FakeWorker, InvocationResult]:
    repo = _git_repo(tmp_path, plan)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    with _closing_client(fake, lambda message: act(fake, _task_of(message))) as worker:
        receipt = _invoke(store, worker, echo=echo, run_suite=run_suite)
    return repo, store, fake, receipt


def _park_then_close(fake: FakeWorker, task_id: str) -> None:
    if task_id == TASK:
        _park_in(_cwd(fake), task_id)
    else:
        _close_in(_cwd(fake), task_id)


def test_park_continues_past_an_isolated_park_and_the_receipt_stays_incomplete(
    tmp_path: Path, caobunga_home: Path
) -> None:
    lines: list[str] = []
    repo, store, fake, receipt = _drain(tmp_path, caobunga_home, _park_then_close, echo=lines)

    runs = store.list_runs("app")
    assert [(run.task_id, run.outcome) for run in runs] == [
        ("CBN-011", Outcome.BLOCKED),
        ("CBN-012", Outcome.CLOSED),
    ]
    assert [Path(str(launch["cwd"])) for launch in fake.launches] == [
        _wt(repo, "CBN-011"),
        _wt(repo, "CBN-012"),
    ]
    # Main got the landed close and never the park commit.
    assert _head(repo) == runs[-1].commit_sha
    assert not (repo / ".flaitron" / "tasknote" / f"{TASK}.md").exists()
    assert "park: CBN-011 retained on wt-CBN-011 — continuing the drain" in lines
    assert receipt.parked[0].held is None
    assert receipt.parked[0].continued_with == ["CBN-012"]
    assert receipt.stop_reason == "incomplete"
    assert "CBN-011 (retained)" in receipt.detail
    assert receipt.status is InvocationStatus.INCOMPLETE
    assert receipt.successful is False
    assert receipt.remaining_tasks == ["CBN-011"]
    assert _branches(repo) == ["wt-CBN-011"]
    _assert_retained(repo, receipt)


def test_receipt_names_every_worktree_parked_in_one_drain(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo, store, _fake, receipt = _drain(
        tmp_path, caobunga_home, lambda fake, task_id: _park_in(_cwd(fake), task_id)
    )
    assert [run.outcome for run in store.list_runs("app")] == [Outcome.BLOCKED] * 2
    assert [park.held for park in receipt.parked] == [None, None]
    assert receipt.parked[0].continued_with == ["CBN-012"]
    assert receipt.stop_reason == "incomplete"
    assert receipt.successful is False
    for task_id in ("CBN-011", "CBN-012"):
        _assert_retained(repo, receipt, task_id)
    retained = [step for step in receipt.recovery if step.startswith("Retained, not landed")]
    assert len(retained) == 2


def test_dirty_park_in_the_worktree_still_continues(tmp_path: Path, caobunga_home: Path) -> None:
    def act(fake: FakeWorker, task_id: str) -> None:
        if task_id == TASK:
            _park_in(_cwd(fake), task_id, commit=False)
            _dirty(_cwd(fake), "half-done.py")
        else:
            _close_in(_cwd(fake), task_id)

    repo, store, _fake, receipt = _drain(tmp_path, caobunga_home, act)
    assert [run.outcome for run in store.list_runs("app")] == [Outcome.BLOCKED, Outcome.CLOSED]
    assert receipt.parked[0].held is None
    assert (_wt(repo, TASK) / "half-done.py").is_file()
    assert not (repo / "half-done.py").exists()
    _assert_retained(repo, receipt)


def test_main_dirt_holds_after_an_isolated_park(tmp_path: Path, caobunga_home: Path) -> None:
    repo_path = tmp_path / "repo"

    def act(fake: FakeWorker, task_id: str) -> None:
        _park_in(_cwd(fake), task_id)
        _dirty(repo_path)

    repo, store, fake, receipt = _drain(tmp_path, caobunga_home, act)
    assert len(fake.launches) == 1
    assert [run.outcome for run in store.list_runs("app")] == [Outcome.BLOCKED]
    assert receipt.stop_reason == "outcome"
    held = receipt.parked[0].held
    assert held is not None
    assert held.startswith("main checkout has 1 uncommitted change(s)")
    assert f"repo held: {held}" in receipt.detail
    _assert_retained(repo, receipt)


def test_dependents_and_the_audit_of_an_isolated_park_are_denied(
    tmp_path: Path, caobunga_home: Path
) -> None:
    def act(fake: FakeWorker, task_id: str) -> None:
        if task_id == "CBN-EPIC-20.2":
            _park_in(_cwd(fake), task_id)
        else:
            _close_in(_cwd(fake), task_id)

    repo, store, _fake, receipt = _drain(tmp_path, caobunga_home, act, plan=EPIC_PLAN)
    outcomes = {run.task_id: run.outcome for run in store.list_runs("app")}
    assert outcomes == {"CBN-EPIC-20.2": Outcome.BLOCKED, "CBN-032": Outcome.CLOSED}
    codes = {denial.task_id: denial.reason.split(" — ", 1)[0] for denial in receipt.denials}
    assert codes == {"CBN-EPIC-20.2": "retained", "CBN-EPIC-20.N": "audit", "CBN-031": "blocked"}
    assert receipt.stop_reason == "incomplete"
    assert receipt.successful is False
    _assert_retained(repo, receipt, "CBN-EPIC-20.2")


@pytest.mark.parametrize("kind", ["worktree", "branch", "directory"])
def test_restart_denies_an_orphan_and_dispatches_the_next_row(
    tmp_path: Path, caobunga_home: Path, kind: str
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    if kind == "worktree":
        start(repo, TASK, _head(repo))
    elif kind == "branch":
        _git(repo, "branch", f"wt-{TASK}")
    else:
        _wt(repo).mkdir(parents=True)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_in(_cwd(fake), "CBN-012")) as worker:
        receipt = _invoke(store, worker)

    assert [run.task_id for run in store.list_runs("app")] == ["CBN-012"]
    assert _cwd(fake) == _wt(repo, "CBN-012")
    assert receipt.denials[0].task_id == TASK
    assert receipt.denials[0].reason.startswith(f"retained — wt-{TASK} exists and is not landed")
    assert _head(repo) == store.list_runs("app")[0].commit_sha


def test_orphan_on_every_marked_row_exits_3_unsuccessful(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    for task_id in ("CBN-011", "CBN-012"):
        _git(repo, "branch", f"wt-{task_id}")
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    receipt = _invoke(store, fake)
    assert fake.launches == []
    assert receipt.legacy_exit_code == 3
    assert receipt.successful is False
    assert [denial.reason.split(" — ", 1)[0] for denial in receipt.denials] == ["retained"] * 2


def test_retained_read_refuses_when_git_cannot_list_branches(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    real = worktree_mod.git_run

    def no_refs(root: Path, *args: str, timeout: float = 30.0) -> str | None:
        return None if args[:1] == ("for-each-ref",) else real(root, *args, timeout=timeout)

    monkeypatch.setattr(worktree_mod, "git_run", no_refs)
    fake = FakeWorker()
    receipt = _invoke(store, fake)
    assert fake.launches == []
    assert receipt.stop_reason == "worktree"
    assert "cannot read branches" in receipt.detail


@pytest.mark.parametrize("moved", [True, False])
def test_rebaseline_only_when_main_moved_during_a_park(
    tmp_path: Path, caobunga_home: Path, moved: bool
) -> None:
    repo_path = tmp_path / "repo"
    roots: list[Path] = []
    lines: list[str] = []

    def act(fake: FakeWorker, task_id: str) -> None:
        if task_id == TASK:
            _park_in(_cwd(fake), task_id)
            if moved:
                (repo_path / "operator.txt").write_text("x\n", encoding="utf-8")
                _commit(repo_path, "chore: operator commit on main")
        else:
            _close_in(_cwd(fake), task_id)

    repo, store, _fake, _receipt = _drain(
        tmp_path, caobunga_home, act, echo=lines, run_suite=_recording_suite(roots)
    )
    assert [run.outcome for run in store.list_runs("app")] == [Outcome.BLOCKED, Outcome.CLOSED]
    first, second = _wt(repo, "CBN-011"), _wt(repo, "CBN-012")
    if moved:
        assert roots == [first, second, second]
        assert any(line.startswith("baseline: main moved to ") for line in lines)
    else:
        assert roots == [first, second]
        assert not any(line.startswith("baseline: main moved") for line in lines)


def test_main_dirt_holds_when_git_cannot_report_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(_root: Path) -> list[str]:
        raise GitUnreadable(_root)

    monkeypatch.setattr(presence, "git_porcelain", unreadable)
    assert presence.main_hold(tmp_path) == "git could not report the main checkout"


# --- landing state on the record and the receipt (CBN-EPIC-450.6) ---


def test_landing_state_landed_on_the_record_and_its_receipt_entry(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    base = _head(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake))) as worker:
        receipt = _invoke(store, worker)
    run = store.list_runs("app")[-1]
    state = run.worktree
    assert state is not None
    assert state.branch == f"wt-{TASK}"
    assert state.base_sha == base
    assert state.worker_root == state.execution_root == str(_wt(repo))
    assert state.landing_root == str(repo.resolve())
    assert state.landing is LandingState.LANDED
    assert state.landed_sha == _head(repo)
    assert state.detail is None
    assert receipt.worktrees == [state]


def test_landing_state_unlanded_keeps_the_outcome_closed(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(_cwd(fake))
        _dirty(_cwd(fake))

    with _closing_client(fake, close) as worker:
        receipt = _invoke(store, worker)
    run = store.list_runs("app")[-1]
    assert run.outcome is Outcome.CLOSED
    assert run.worktree is not None
    assert run.worktree.landing is LandingState.UNLANDED
    assert run.worktree.landed_sha is None
    assert "uncommitted change(s)" in (run.worktree.detail or "")
    assert receipt.worktrees[0].landing is LandingState.UNLANDED


def test_landing_state_red_suite_close_is_unlanded_with_its_verdict(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake))) as worker:
        _invoke(store, worker, run_suite=_not_green_suite("red"))
    state = store.list_runs("app")[-1].worktree
    assert state is not None
    assert state.landing is LandingState.UNLANDED
    assert state.detail == "its suite was red"


def test_landing_state_a_park_is_retained_and_the_next_close_lands(
    tmp_path: Path, caobunga_home: Path
) -> None:
    _repo, store, _fake, receipt = _drain(tmp_path, caobunga_home, _park_then_close)
    parked, closed = store.list_runs("app")
    assert parked.worktree is not None and closed.worktree is not None
    assert parked.worktree.landing is LandingState.RETAINED
    assert parked.worktree.detail is None
    assert closed.worktree.landing is LandingState.LANDED
    assert receipt.worktrees == [parked.worktree, closed.worktree]


def test_landing_state_a_failed_cleanup_is_landed_with_its_detail(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    monkeypatch.setattr("caobunga.landing.cleanup", lambda _ctx, _sha: "simulated refusal")
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake))) as worker:
        _invoke(store, worker)
    state = store.list_runs("app")[-1].worktree
    assert state is not None
    assert state.landing is LandingState.LANDED
    assert state.landed_sha == _head(repo)
    assert state.detail == "worktree not removed: simulated refusal"


def test_landing_state_absent_on_a_run_that_did_not_isolate(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    store.register(RegistryRecord(id="app", path=str(repo.resolve()), registered_at=utc_now()))
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        receipt = _invoke(store, worker)
    run = store.list_runs("app")[-1]
    assert run.worktree is None
    assert "worktree" not in run.model_dump(mode="json", exclude_none=True)
    assert receipt.worktrees == []


def test_worktrees_entry_is_checkpointed_at_start_with_no_landing(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    seen: list[InvocationResult] = []

    def close(_message: str) -> None:
        seen.extend(store.running_invocations("app"))
        _close_worker(_cwd(fake))

    with _closing_client(fake, close) as worker:
        _invoke(store, worker)
    assert len(seen) == 1
    (entry,) = seen[0].worktrees
    assert entry.branch == f"wt-{TASK}"
    assert entry.execution_root == str(_wt(repo))
    assert entry.landing is None


def test_worktrees_a_refusal_after_start_settles_retained(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, bootstrap=[sys.executable, "-c", "raise SystemExit(3)"])
    receipt = _invoke(store, FakeWorker())
    assert receipt.stop_reason == "worktree"
    assert store.list_runs("app") == []
    (entry,) = receipt.worktrees
    assert entry.landing is LandingState.RETAINED
    assert entry.branch == f"wt-{TASK}"


def test_worktrees_entry_is_settled_landed_before_the_record_is_written(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A death between the fast-forward and `finish` must not read as unlanded work."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    seen: list[InvocationResult] = []
    real_cleanup = landing_mod.cleanup

    def cleanup(context: object, sha: str) -> str | None:
        seen.extend(store.running_invocations("app"))
        return real_cleanup(context, sha)  # type: ignore[arg-type]

    monkeypatch.setattr("caobunga.landing.cleanup", cleanup)
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(_cwd(fake))) as worker:
        _invoke(store, worker)
    (entry,) = seen[0].worktrees
    assert entry.landing is LandingState.LANDED
    assert entry.landed_sha == _head(repo)
