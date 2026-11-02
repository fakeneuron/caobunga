"""Post-close tail (finish.py): settle, suite measure, status card, follow-ups and filed rows."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    CLOSED_PLAN,
    OPEN_PLAN,
    FakeWorker,
    _amend,
    _BusyWorker,
    _close_worker,
    _closing_client,
    _commit,
    _git,
    _git_repo,
    _green_suite,
    _note,
    _register,
    _run,
    _usage,
)

from caobunga import finish
from caobunga.close import CloseSnapshot
from caobunga.errors import ReportError, UserError, WorkerError
from caobunga.git import git_head
from caobunga.loop import execute_run
from caobunga.models import (
    Escalation,
    ExecutionTier,
    ForeignCommit,
    NewRow,
    Outcome,
    RunRecord,
    compact_utc,
)
from caobunga.store import Store
from caobunga.suite import SuiteRun
from caobunga.wait import SuiteHeartbeat
from caobunga.worker import Session


def test_gated_writes_the_status_card(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-188.2: the last-run card lands under `$CAOBUNGA_HOME/status/`, not the target repo."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
        )
    assert result is not None
    assert not (repo / "caobunga-status.md").exists()
    status = store.status_file("app")
    assert status.is_file()
    body = status.read_text(encoding="utf-8")
    assert body.startswith("# Caobunga status\n")
    assert "- **task:** CBN-011\n" in body
    assert "- **outcome:** closed\n" in body
    assert result.summary in body
    assert "## 📝" not in body
    assert any(line.startswith("card: ") for line in lines)
    head_after = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_after == result.commit_sha


def test_gated_records_and_echoes_a_foreign_commit(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-172: a commit landing mid-window is recorded and echoed, not swallowed."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []

    def _close_with_foreign_commit(_msg: str) -> None:
        (repo / "scratch.txt").write_text("filed CBN-999\n", encoding="utf-8")
        _commit(repo, "chore: unrelated sweep filing", author="Someone Else")
        _close_worker(repo)

    with _closing_client(fake, _close_with_foreign_commit) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
        )
    assert result is not None
    assert result.foreign_commits is not None
    assert len(result.foreign_commits) == 1
    commit = result.foreign_commits[0]
    assert commit.subject == "chore: unrelated sweep filing"
    assert commit.author == "Someone Else"
    assert any(line == f"foreign: {commit.sha} {commit.author} {commit.subject}" for line in lines)


def test_gated_echoes_a_workers_own_filing_as_filed_not_foreign(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-299: the worker's `/ft-file-followup` commit is `new_rows`' account, not foreign."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []

    def _file_then_close(_msg: str) -> None:
        plan = repo / ".flaitron" / "PLAN.md"
        plan.write_text(
            OPEN_PLAN.replace(
                "## Medium\n\n(none)",
                "## Medium\n\n- [ ] **CBN-014** [light] | worker-filed — filed mid-run.",
            ),
            encoding="utf-8",
        )
        _commit(repo, "chore: file CBN-014 follow-up")
        _close_worker(repo, plan=FILED_PLAN)

    with _closing_client(fake, _file_then_close) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.foreign_commits is None
    assert result.new_rows == [NewRow(task_id="CBN-014", unattended=False)]
    assert any(line.startswith("filed: ") and "file CBN-014 follow-up" in line for line in lines)
    assert not any(line.startswith("foreign: ") for line in lines)


def test_observe_does_not_write_status_card(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.OBSERVE)
    fake = FakeWorker()
    with fake as worker:
        result = execute_run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
        )
    assert result is None
    assert not (repo / "caobunga-status.md").exists()
    assert not store.status_file("app").exists()


def test_status_card_failure_degrades_only_that_surface(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []

    def boom(_store: Store, _run: object) -> Path:
        raise WorkerError("status card write failed: boom")

    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        with pytest.raises(ReportError, match="status card write failed") as exc:
            _run(
                "app",
                store=store,
                worker=worker,
                poll_interval=0,
                timeout=2,
                publish_status=boom,
                echo=lines.append,
            )
    # CBN-EPIC-9.5 F4: a card failure costs that surface alone — the record stays
    # written and carried on the error, and the escalation stands.
    assert exc.value.exit_code == 4
    assert exc.value.record.task_id == "CBN-011"
    loaded = store.last_run("app")
    assert loaded is not None
    assert loaded.outcome is Outcome.CLOSED
    assert "card: failed: status card write failed: boom" in lines


def test_worker_settles_before_caobunga_touches_the_repo(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-EPIC-9.5 F1. No git work in the target repo while the worker is alive."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _BusyWorker(lambda: _close_worker(repo), busy_polls=3)
    busy_at_publish: list[bool] = []

    def publish(_store: Store, _run: object) -> Path:
        busy_at_publish.append(not worker.settled)
        return store.status_file("app")

    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        sleep=lambda _s: None,
        publish_status=publish,
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert worker.polls > 3
    assert busy_at_publish == [False]


class _SettleEditWorker(_BusyWorker):
    """Rewrites the repo on its first settle poll, while it still reports busy (CBN-275)."""

    def __init__(self, on_send: Callable[[], object], on_settle: Callable[[], object]) -> None:
        super().__init__(on_send, busy_polls=1)
        self._on_settle = on_settle

    def status(self, session: Session) -> str:
        if self.polls == 0:
            self._on_settle()
        return super().status(session)


def _settle_edit_run(
    tmp_path: Path, home: Path, edit: Callable[[Path], object]
) -> tuple[Path, str | None, RunRecord | None]:
    """Run one gated close whose worker calls `edit(repo)` mid-settle; return the detected sha."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(home)
    _register(store, repo)
    detected: list[str | None] = []

    def on_settle() -> None:
        detected.append(git_head(repo))
        edit(repo)

    worker = _SettleEditWorker(lambda: _close_worker(repo), on_settle)
    result = _run(
        "app", store=store, worker=worker, poll_interval=0, timeout=2, sleep=lambda _s: None
    )
    return repo, detected[0], result


def test_settle_rereads_a_close_the_worker_amended(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-275. A fleet repo's FE-038: an amend after detection left `commit_sha` off-branch."""

    def amend(repo: Path) -> None:
        note = repo / ".flaitron" / "tasknote" / "archive" / "cbn" / "CBN-011.md"
        text = note.read_text(encoding="utf-8")
        note.write_text(text.replace("The reader picks", "The amend picks"), encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "--amend", "--no-edit")

    repo, detected, result = _settle_edit_run(tmp_path, caobunga_home, amend)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.commit_sha == git_head(repo)
    assert result.commit_sha != detected
    assert result.summary.startswith("The amend picks the next task.")
    assert result.foreign_commits is None


def test_settle_keeps_the_close_under_a_stacked_commit(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-275. A later commit leaves the close on-branch — it is not re-attributed."""

    def stack(repo: Path) -> None:
        (repo / "after.txt").write_text("after\n", encoding="utf-8")
        _commit(repo, "chore: after the close")

    repo, detected, result = _settle_edit_run(tmp_path, caobunga_home, stack)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.commit_sha == detected
    assert git_head(repo) != detected


def test_run_record_carries_the_worker_s_proposed_follow_ups(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-121: the one link between extraction and every reader that renders it."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    section = "### Follow-ups proposed\n\n- **CBN-999** | guard — the discovery.\n\n"

    def close(_message: str) -> None:
        _close_worker(repo, follow_ups=section)

    with _closing_client(fake, close) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.follow_ups == ["**CBN-999** | guard — the discovery."]
    # And it survives the round trip through disk, not only the in-memory record.
    persisted = store.last_run("app")
    assert persisted is not None
    assert persisted.follow_ups == ["**CBN-999** | guard — the discovery."]


def test_run_record_omits_follow_ups_when_the_worker_proposed_none(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(repo)

    with _closing_client(fake, close) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.follow_ups is None


# A row the worker files into `## Medium` mid-run — unmarked, as every
# worker-filed row is: caobunga never seeds and flaitron never writes the marker.
FILED_PLAN = CLOSED_PLAN.replace(
    "## Medium\n\n(none)",
    "## Medium\n\n- [ ] **CBN-014** [light] | worker-filed — filed mid-run.",
)


def test_run_record_carries_the_rows_the_worker_filed(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-138.3: the launch baseline reaches `inspect_close` and the diff reaches disk."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(repo, plan=FILED_PLAN)

    with _closing_client(fake, close) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.new_rows == [NewRow(task_id="CBN-014", unattended=False)]
    persisted = store.last_run("app")
    assert persisted is not None
    assert persisted.new_rows == [NewRow(task_id="CBN-014", unattended=False)]


def test_run_record_omits_new_rows_when_the_worker_filed_none(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(repo)

    with _closing_client(fake, close) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.new_rows is None


def test_closed_run_carries_the_suite_and_saves_its_log(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-EPIC-202.3: the suite rides the single write at v2, the log pairs by stamp,
    and one `suite:` line is echoed. `gated` is the default tier here — every
    launching tier runs it; only the continue is `proceed-on-green`'s."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    seen: list[tuple[Path, Store]] = []

    def runner(root: Path, st: Store) -> SuiteRun:
        seen.append((root, st))
        return _green_suite(root, st)

    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            run_suite=runner,
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    # The pre-launch baseline (CBN-420), then the close.
    assert seen == [(repo.resolve(), store)] * 2
    assert result.schema_version == 2
    assert result.suite is not None
    assert result.suite.verdict.value == "green"
    assert [verb.verb for verb in result.suite.verbs] == ["test", "lint", "typecheck"]
    on_disk = store.last_run("app")
    assert on_disk is not None
    assert on_disk.suite == result.suite
    log = store.suite_file("app", compact_utc(result.closed_at), "CBN-011")
    assert log.read_text(encoding="utf-8") == "verdict: green\n\ntest: fake\n"
    assert log.stem == store.run_file(result).stem
    assert "suite: green — test 0 1.5s · lint 0 0.2s · typecheck 0 0.4s" in lines


def test_suite_log_failure_does_not_abort_a_closed_run(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-431: suite output is diagnostic and must not undo a completed run."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def no_space(*_args: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_suite", no_space)
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    persisted = store.last_run("app")
    assert persisted is not None
    assert persisted.outcome is Outcome.CLOSED


def test_post_settle_tree_line_is_read_after_the_worker_stops(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-352: a rotation the worker writes after close detection (here during the
    suite) is on the record, and the session is shut down once, before the write."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    written_at_shutdown: list[bool] = []
    shutdown = fake.shutdown

    def recording_shutdown(session: Session) -> None:
        written_at_shutdown.append(store.last_run("app") is not None)
        shutdown(session)

    fake.shutdown = recording_shutdown  # type: ignore[method-assign]

    def runner(root: Path, st: Store) -> SuiteRun:
        if fake.launches:  # past the pre-launch baseline (CBN-420)
            (repo / ".flaitron" / "PLAN-ARCHIVE.md").write_text("rotated\n", encoding="utf-8")
        return _green_suite(root, st)

    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, run_suite=runner
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    summary = result.summary or ""
    assert "tree: 1 file(s) left (?? .flaitron/PLAN-ARCHIVE.md; no open tasknote)" in summary
    assert summary.count(" — tree: ") == 1
    assert written_at_shutdown == [False]
    assert len(fake.deleted) == 1


def _post_suite_run(
    tmp_path: Path, caobunga_home: Path, during_suite: Callable[[Path], None]
) -> tuple[RunRecord, Path]:
    """Close CBN-011, run `during_suite` on the repo inside the suite, return the record."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    fake = FakeWorker()

    def runner(root: Path, st: Store) -> SuiteRun:
        if fake.launches:  # past the pre-launch baseline (CBN-420)
            during_suite(repo)
        return _green_suite(root, st)

    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, run_suite=runner
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.suite is not None
    assert result.suite.verdict == "green"
    return result, repo


def test_post_suite_stacked_commit_is_foreign(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-356: a commit stacked on the close during the suite (a fleet repo's rotation)
    is on the record as foreign; the close keeps its attribution."""
    closes: list[str | None] = []

    def rotate(repo: Path) -> None:
        # A real rotation edits PLAN too, so the close walk sees this commit.
        closes.append(git_head(repo))
        plan = repo / ".flaitron" / "PLAN.md"
        plan.write_text(plan.read_text(encoding="utf-8") + "\n<!-- rotated -->\n", encoding="utf-8")
        (repo / ".flaitron" / "PLAN-ARCHIVE.md").write_text("rotated\n", encoding="utf-8")
        _commit(repo, "chore: rotate Completed")

    result, _repo = _post_suite_run(tmp_path, caobunga_home, rotate)
    assert result.commit_sha == closes[0]
    assert result.foreign_commits is not None
    assert [c.subject for c in result.foreign_commits] == ["chore: rotate Completed"]


def test_post_suite_amend_follows_the_close(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-356: an amend during the suite leaves the record's `commit_sha` on-branch."""
    result, repo = _post_suite_run(tmp_path, caobunga_home, _amend)
    assert result.commit_sha == git_head(repo)
    assert result.foreign_commits is None


def test_post_settle_refresh_passes_since_to_the_footprint_split(tmp_path: Path) -> None:
    """CBN-452: the post-suite re-read is the line the record keeps, so it must split too."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / "stray.txt").write_text("x\n", encoding="utf-8")
    closed = CloseSnapshot(
        outcome=Outcome.CLOSED, summary="done — tree: 0 file(s) left (no open tasknote)"
    )
    fresh = finish.refresh_tree(closed, repo, "CBN-011", since=sha)
    assert fresh.summary == (
        "done — tree: 1 file(s) left (1 outside worker footprint: ?? stray.txt; no open tasknote)"
    )


def test_post_settle_refresh_excludes_the_snapshots_foreign_commits(tmp_path: Path) -> None:
    """CBN-457: the re-read gets the foreign shas from the snapshot it replaces."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / "theirs.txt").write_text("1\n", encoding="utf-8")
    _commit(repo, "concurrent session")
    foreign = git_head(repo)
    assert foreign is not None
    (repo / "theirs.txt").write_text("2\n", encoding="utf-8")
    closed = CloseSnapshot(
        outcome=Outcome.CLOSED,
        summary="done — tree: 0 file(s) left (no open tasknote)",
        foreign_commits=[
            ForeignCommit(sha=foreign, author="Someone", subject="concurrent session")
        ],
    )
    fresh = finish.refresh_tree(closed, repo, "CBN-011", since=sha)
    assert fresh.summary == (
        "done — tree: 1 file(s) left (1 outside worker footprint: M theirs.txt; no open tasknote)"
    )


def test_post_settle_refresh_keeps_the_mismatch_and_skips_free_text(tmp_path: Path) -> None:
    """CBN-352: only a folded tail is swapped — the CBN-328 mismatch ahead of it
    stays, and a summary that never folded is left alone even if it reads alike."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / "stray.txt").write_text("x\n", encoding="utf-8")
    exited = CloseSnapshot(
        outcome=Outcome.FAILED,
        escalation=Escalation.WORKER_EXITED,
        summary="exit 0 — park-reason present but status in-progress, not blocked"
        " — tree: 0 file(s) left (no open tasknote)",
    )
    fresh = finish.refresh_tree(exited, repo, "CBN-011")
    assert fresh.summary == (
        "exit 0 — park-reason present but status in-progress, not blocked"
        " — tree: 1 file(s) left (?? stray.txt; no open tasknote)"
    )
    parked = CloseSnapshot(
        outcome=Outcome.BLOCKED,
        escalation=Escalation.WORKER_PARKED,
        summary="blocked on X — tree: needs rework",
    )
    assert finish.refresh_tree(parked, repo, "CBN-011") is parked
    # Omitting `baseline` must not wipe a diff the failure snapshot already took.
    filed = exited.model_copy(update={"new_rows": [NewRow(task_id="CBN-099")]})
    assert finish.refresh_tree(filed, repo, "CBN-011").new_rows == [NewRow(task_id="CBN-099")]


def test_refresh_tree_picks_up_a_row_filed_before_the_worker_stopped(tmp_path: Path) -> None:
    """CBN-436. The post-stop re-read sees a row the pre-settle snapshot missed."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    timed_out = CloseSnapshot(
        outcome=Outcome.FAILED,
        escalation=Escalation.TIMEOUT,
        summary="timed out — tree: 0 file(s) left (no open tasknote)",
    )
    (repo / ".flaitron" / "PLAN.md").write_text(
        OPEN_PLAN + "\n- [ ] **CBN-099** [medium] | worker-filed — filed mid-run.\n",
        encoding="utf-8",
    )
    fresh = finish.refresh_tree(timed_out, repo, "CBN-011", baseline=["CBN-011", "CBN-012"])
    assert fresh.new_rows == [NewRow(task_id="CBN-099")]
    assert fresh.summary.startswith("timed out — tree: 1 file")


def test_refresh_tree_keeps_rows_when_the_plan_cannot_be_reread(tmp_path: Path) -> None:
    """An unreadable re-read must not drop a diff the failure snapshot already took."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    filed = CloseSnapshot(
        outcome=Outcome.FAILED,
        escalation=Escalation.TIMEOUT,
        summary="timed out",
        new_rows=[NewRow(task_id="CBN-099")],
    )
    (repo / ".flaitron" / "PLAN.md").unlink()
    assert finish.refresh_tree(filed, repo, "CBN-011", baseline=["CBN-011", "CBN-012"]) is filed


def test_a_queued_suite_echoes_the_wait_live(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default suite is handed an `on_wait` that echoes before it blocks (CBN-310)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []

    def queued(
        root: Path,
        st: Store,
        *,
        on_wait: Callable[[], None] | None = None,
        on_verb: Callable[[str], None] | None = None,  # noqa: ARG001
    ) -> SuiteRun:
        assert on_wait is not None
        on_wait()
        return _green_suite(root, st)

    monkeypatch.setattr("caobunga.finish.run_fleet_suite", queued)
    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            run_suite=None,
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert lines.index("suite: waiting for suite.lock") < next(
        i for i, line in enumerate(lines) if line.startswith("suite: green")
    )


def test_post_close_heartbeat_spans_settle_and_suite(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-EPIC-449.5: one heartbeat from the close wait's start, staged settle → suite <verb>."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    starts: list[float | None] = []
    stages: list[str] = []

    class Recording(SuiteHeartbeat):
        def __init__(self, echo: Callable[[str], None] | None, started: float | None) -> None:
            starts.append(started)
            super().__init__(echo, started)

        def __setattr__(self, name: str, value: object) -> None:
            if name == "stage":
                stages.append(str(value))
            super().__setattr__(name, value)

    def fleet(
        root: Path,
        st: Store,
        *,
        on_wait: Callable[[], None] | None = None,  # noqa: ARG001
        on_verb: Callable[[str], None] | None = None,
    ) -> SuiteRun:
        if on_verb is not None:
            on_verb("test")
            on_verb("lint")
        return _green_suite(root, st)

    monkeypatch.setattr("caobunga.finish.SuiteHeartbeat", Recording)
    monkeypatch.setattr("caobunga.finish.run_fleet_suite", fleet)
    with _closing_client(FakeWorker(), lambda _m: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2, run_suite=None)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    (started,) = starts
    assert isinstance(started, float)
    assert stages == ["settle", "suite", "suite test", "suite lint"]


def test_parked_run_measures_no_suite(tmp_path: Path, caobunga_home: Path) -> None:
    """A park leaves no closed tree: the runner is called only for the pre-launch
    baseline (CBN-420), and there is no field, no log, no `suite:` echo."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    calls: list[Path] = []

    def runner(root: Path, st: Store) -> SuiteRun:
        calls.append(root)
        return _green_suite(root, st)

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked"), encoding="utf-8")

    lines: list[str] = []
    with _closing_client(fake, park) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            run_suite=runner,
        )
    assert result is not None
    assert result.outcome is Outcome.BLOCKED
    assert calls == [repo.resolve()]
    assert result.suite is None
    # A green baseline saves nothing: only a refusal keeps its log.
    assert not store.suites_dir("app").exists()
    assert not any(line.startswith("suite:") for line in lines)


def test_default_runner_refuses_on_a_repo_without_just(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No injection → the real runner, which is a `refused` result on a bare tmp repo
    with no subprocess. Since CBN-420 the pre-launch baseline meets it first, so the
    repo is refused before any worker launches and no task record is written."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: None)
    fake = FakeWorker()
    with (
        _closing_client(fake, lambda _m: _close_worker(repo)) as worker,
        pytest.raises(UserError, match="baseline is refused on the clean tree"),
    ):
        execute_run("app", store=store, worker=worker, poll_interval=0, timeout=2, usage=_usage())
    assert not fake.launches
    assert store.last_run("app") is None
    (log,) = store.suites_dir("app").glob("*-CBN-011-baseline.log")
    assert "refused" in log.read_text(encoding="utf-8")
