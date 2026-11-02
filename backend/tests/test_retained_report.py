"""Retained isolated work on the read-only reports (CBN-EPIC-450.6).

One fixture, every surface: an isolated project whose `CBN-011` closed on
`wt-CBN-011` and did not land. `status` (fleet and detail), the digest, the
last-run card, the Status API and `observe` must all skip that id the way the
drain does, and name the same leftover with the same landing state.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from conftest import (
    OPEN_PLAN,
    FakeWorker,
    _close_worker,
    _closing_client,
    _git,
    _git_repo,
    _green_suite,
    _usage,
)
from fastapi.testclient import TestClient
from test_store import green_suite

from caobunga import loop
from caobunga.cli import cli
from caobunga.digest import format_digest
from caobunga.errors import UserError
from caobunga.git import git_head
from caobunga.models import (
    ExecutionTier,
    LandingState,
    Outcome,
    RegistryRecord,
    RetainedWorktree,
    RunRecord,
    WorktreeRun,
    utc_now,
)
from caobunga.recovery import orphan_recovery
from caobunga.retained import report_retained, retained_worktrees
from caobunga.status_card import format_status
from caobunga.status_render import format_retained, retained_footer, retained_state
from caobunga.store import Store
from caobunga.worktree import start, worktree_path
from main import app

TASK = "CBN-011"
WHY = "the main checkout moved since the worktree started"


def _register(store: Store, repo: Path, *, tier: ExecutionTier, isolate: bool = True) -> None:
    store.register(
        RegistryRecord(
            id="app",
            path=str(repo.resolve()),
            tier=tier,
            registered_at=utc_now(),
            isolate=isolate,
        )
    )


def _unlanded(store: Store, repo: Path, *, tier: ExecutionTier) -> RunRecord:
    """`CBN-011` closed on its retained branch; main still shows the row open."""
    _register(store, repo, tier=tier)
    context = start(repo, TASK, git_head(repo) or "")
    run = RunRecord(
        project="app",
        task_id=TASK,
        outcome=Outcome.CLOSED,
        commit_sha="a" * 40,
        closed_at=utc_now() - timedelta(minutes=1),
        summary="closed on its branch",
        suite=green_suite(),
        worktree=WorktreeRun(
            branch=context.branch,
            worker_root=str(context.worker_root),
            execution_root=str(context.execution_root),
            landing_root=str(context.landing_root),
            base_sha=context.base_sha,
            landing=LandingState.UNLANDED,
            detail=WHY,
        ),
    )
    store.write_run(run)
    return run


@pytest.fixture
def project(tmp_path: Path, caobunga_home: Path) -> tuple[Path, Store, RunRecord]:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    run = _unlanded(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    return repo, store, run


def test_every_surface_skips_and_names_the_same_retained_id(
    project: tuple[Path, Store, RunRecord],
) -> None:
    repo, store, run = project
    wt = str(worktree_path(repo.resolve(), TASK))

    detail = CliRunner().invoke(cli, ["status", "app"])
    assert detail.exit_code == 0, detail.output
    assert "dispatch of CBN-012" in detail.output
    assert f"{TASK}  deny: retained — wt-{TASK} exists" in detail.output
    assert "retained: 1 worktree(s) — the drain skips these ids" in detail.output
    assert f"  wt-{TASK} at {wt} — unlanded: {WHY}" in detail.output
    assert f"recovery: Retained, not landed: branch wt-{TASK} at {wt}" in detail.output

    fleet = CliRunner().invoke(cli, ["status"])
    assert fleet.exit_code == 0, fleet.output
    assert "closed (not landed)" in fleet.output
    assert f"retained: app wt-{TASK} (unlanded: {WHY}) — see caobunga status app" in fleet.output

    body = TestClient(app, base_url="http://127.0.0.1").get("/api/projects/app").json()
    assert body["tree"]["candidate"] == "CBN-012"
    assert TASK in {denial["task_id"] for denial in body["coverage"]["denials"]}
    (entry,) = body["retained_worktrees"]
    assert entry["task_id"] == TASK
    assert entry["branch"] == f"wt-{TASK}"
    assert entry["path"] == wt
    assert entry["landing"] == "unlanded"
    assert entry["detail"] == WHY
    assert entry["recovery"].startswith(f"Retained, not landed: branch wt-{TASK}")
    assert body["last_run"]["worktree"]["landing"] == "unlanded"

    digest = format_digest(store, now=run.closed_at)
    assert f"- **{TASK}** (`closed`) [not landed] — closed on its branch" in digest
    assert f"- **retained** `wt-{TASK}` (unlanded: {WHY}) at {wt}" in digest

    assert f"- **worktree:** wt-{TASK} not landed — {WHY}" in format_status(run)


def test_observe_eligibility_echo_denies_the_retained_id(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _unlanded(store, repo, tier=ExecutionTier.OBSERVE)
    lines: list[str] = []
    loop.execute_invocation("app", store=store, echo=lines.append, usage=_usage())
    # `next:` stays the attended pick; the per-task echo is the as-if-unattended one.
    assert any(line.startswith(f"eligibility: {TASK} deny: retained") for line in lines), lines
    assert any(line.startswith("eligibility: CBN-012 allow: ") for line in lines), lines


def test_a_closing_records_next_task_skips_a_retained_suggestion(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """The PLAN's suggested next is `CBN-011`, but its branch is retained."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    _git(repo, "branch", f"wt-{TASK}")
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.GATED)
    fake = FakeWorker()

    def close(_message: str) -> None:
        root = Path(str(fake.created["cwd"])) if fake.created else repo
        plan = root / ".flaitron" / "PLAN.md"
        text = plan.read_text(encoding="utf-8").replace("- [ ] **CBN-012**", "- [x] **CBN-012**")
        _close_worker(root, "CBN-012", text)

    with _closing_client(fake, close) as worker:
        loop.execute_invocation(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            usage=_usage(),
            run_suite=_green_suite,
        )
    (run,) = store.list_runs("app")
    assert run.task_id == "CBN-012"
    assert run.outcome is Outcome.CLOSED
    assert run.next_task is None


def test_unreadable_git_degrades_every_surface(
    project: tuple[Path, Store, RunRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, store, _run = project

    def refuse(_root: Path) -> frozenset[str]:
        raise UserError("cannot read branches")

    monkeypatch.setattr("caobunga.retained.retained_ids", refuse)
    record = store.get("app")
    assert report_retained(record) is None
    assert retained_worktrees(record, None, list) is None

    detail = CliRunner().invoke(cli, ["status", "app"])
    assert detail.exit_code == 0, detail.output
    assert "retained: unreadable (git could not list wt- branches and worktrees)" in detail.output
    fleet = CliRunner().invoke(cli, ["status"])
    assert "retained: app unreadable" in fleet.output
    body = TestClient(app, base_url="http://127.0.0.1").get("/api/projects/app").json()
    assert body["retained_worktrees"] is None
    assert "- **retained** unreadable" in format_digest(store)


def test_a_project_that_does_not_isolate_lists_nothing(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    _git(repo, "branch", f"wt-{TASK}")
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.GATED, isolate=False)
    record = store.get("app")
    assert report_retained(record) == frozenset()

    detail = CliRunner().invoke(cli, ["status", "app"])
    assert "retained:" not in detail.output
    fleet = CliRunner().invoke(cli, ["status"])
    assert "retained:" not in fleet.output
    body = TestClient(app, base_url="http://127.0.0.1").get("/api/projects/app").json()
    assert body["retained_worktrees"] == []
    assert body["tree"]["candidate"] == TASK
    assert "**retained**" not in format_digest(store)


@pytest.mark.parametrize(
    ("record", "starts"),
    [
        (None, f"wt-{TASK} has no run record naming it"),
        (LandingState.RETAINED, f"Retained, not landed: branch wt-{TASK}"),
        (LandingState.LANDED, f"{TASK} landed on the main checkout at"),
    ],
)
def test_each_entry_gets_the_recovery_its_record_calls_for(
    tmp_path: Path, caobunga_home: Path, record: LandingState | None, starts: str
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.GATED)
    context = start(repo, TASK, git_head(repo) or "")
    runs: list[RunRecord] = []
    if record is not None:
        landed = record is LandingState.LANDED
        runs.append(
            RunRecord(
                project="app",
                task_id=TASK,
                outcome=Outcome.CLOSED if landed else Outcome.BLOCKED,
                commit_sha="b" * 40 if landed else None,
                closed_at=utc_now(),
                summary="s",
                suite=green_suite() if landed else None,
                worktree=WorktreeRun(
                    branch=context.branch,
                    worker_root=str(context.worker_root),
                    execution_root=str(context.execution_root),
                    landing_root=str(context.landing_root),
                    base_sha=context.base_sha,
                    landing=record,
                    landed_sha="b" * 40 if landed else None,
                    detail="worktree not removed: busy" if landed else None,
                ),
            )
        )
    entries = retained_worktrees(store.get("app"), frozenset({TASK}), lambda: runs)
    assert entries is not None
    (entry,) = entries
    assert entry.landing is record
    assert entry.recovery.startswith(starts), entry.recovery


def test_a_fully_cleaned_landing_does_not_describe_a_later_branch(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """A clean landing removed its branch, so a `wt-<ID>` now is a later dispatch's."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.GATED)
    landed = RunRecord(
        project="app",
        task_id=TASK,
        outcome=Outcome.CLOSED,
        commit_sha="c" * 40,
        closed_at=utc_now(),
        summary="s",
        suite=green_suite(),
        worktree=WorktreeRun(
            branch=f"wt-{TASK}",
            worker_root="/gone",
            execution_root="/gone",
            landing_root=str(repo),
            base_sha="d" * 40,
            landing=LandingState.LANDED,
            landed_sha="c" * 40,
        ),
    )
    entries = retained_worktrees(store.get("app"), frozenset({TASK}), lambda: [landed])
    assert entries is not None
    assert entries[0].landing is None
    assert entries[0].recovery.startswith(f"wt-{TASK} has no run record naming it")


def test_renderers_name_none_and_each_state_form(tmp_path: Path) -> None:
    record = RegistryRecord(id="app", path=str(tmp_path), registered_at=utc_now(), isolate=True)
    assert format_retained(record, []) == ["retained: none"]
    assert retained_footer(record, []) == []

    def entry(landing: LandingState | None, detail: str | None) -> RetainedWorktree:
        return RetainedWorktree(
            task_id=TASK,
            branch=f"wt-{TASK}",
            path="/wt",
            landing=landing,
            detail=detail,
            recovery="r",
        )

    assert retained_state(entry(None, None)) == "no run record"
    assert retained_state(entry(LandingState.RETAINED, None)) == "retained"
    assert retained_state(entry(LandingState.UNLANDED, WHY)) == f"unlanded: {WHY}"


def test_a_record_for_an_earlier_branch_does_not_describe_a_later_one(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Same branch name, different base: the old park's record is not this branch's."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.GATED)
    first = start(repo, TASK, git_head(repo) or "")
    parked = RunRecord(
        project="app",
        task_id=TASK,
        outcome=Outcome.BLOCKED,
        closed_at=utc_now(),
        summary="s",
        worktree=WorktreeRun(
            branch=first.branch,
            worker_root=str(first.worker_root),
            execution_root=str(first.execution_root),
            landing_root=str(first.landing_root),
            base_sha=first.base_sha,
            landing=LandingState.RETAINED,
        ),
    )
    _git(repo, "worktree", "remove", "--force", str(first.worker_root))
    _git(repo, "branch", "-D", first.branch)
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "later")
    start(repo, TASK, git_head(repo) or "")

    entries = retained_worktrees(store.get("app"), frozenset({TASK}), lambda: [parked])
    assert entries is not None
    assert entries[0].landing is None
    assert entries[0].recovery.startswith(f"wt-{TASK} has no run record naming it")


def test_observe_says_when_the_retained_read_is_unreadable(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _unlanded(store, repo, tier=ExecutionTier.OBSERVE)

    def refuse(_root: Path) -> frozenset[str]:
        raise UserError("cannot read branches")

    monkeypatch.setattr("caobunga.retained.retained_ids", refuse)
    lines: list[str] = []
    loop.execute_invocation("app", store=store, echo=lines.append, usage=_usage())
    assert any(line.startswith("retained: unreadable") for line in lines), lines


def test_a_branch_without_a_reflog_keeps_its_records_guidance(
    project: tuple[Path, Store, RunRecord],
) -> None:
    """No reflog to read: an ancestor base still matches, so `unlanded` survives."""
    repo, store, _run = project
    log = repo / ".git" / "logs" / "refs" / "heads" / f"wt-{TASK}"
    assert log.is_file()
    log.unlink()
    entries = retained_worktrees(
        store.get("app"), frozenset({TASK}), lambda: store.list_runs("app")
    )
    assert entries is not None
    assert entries[0].landing is LandingState.UNLANDED
    assert entries[0].recovery.startswith(f"Retained, not landed: branch wt-{TASK}")


def test_orphan_recovery_forces_the_worktree_removal() -> None:
    step = orphan_recovery(task_id=TASK, branch=f"wt-{TASK}", worker_root="/wt", landing_root="/m")
    assert "worktree remove --force /wt" in step
