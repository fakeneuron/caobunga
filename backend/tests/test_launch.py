"""Pick → launch → close wait (launch.py / wait.py): routing, re-pick, session lifecycle."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    OPEN_PLAN,
    FakeWorker,
    _BusyWorker,
    _close_worker,
    _closing_client,
    _commit,
    _ExitedWorker,
    _git_repo,
    _register,
    _run,
    _tail,
    _usage,
    _UsageLimitThenCloseWorker,
)
from watchdog.observers.polling import PollingObserver

from caobunga import wait
from caobunga.close import CloseSnapshot
from caobunga.errors import GitUnreadable, ReportError, Unlaunchable, UserError, WorkerError
from caobunga.flaitron import phase_progress
from caobunga.git import git_dirt, git_head
from caobunga.launch import _failed_from_worker
from caobunga.models import Escalation, NewRow, Outcome
from caobunga.store import Store
from caobunga.taskmap import EFFORT, TaskClass
from caobunga.usage import Leftover, ProviderUsage
from caobunga.wait import (
    ACTIVITY_MAX,
    SuiteHeartbeat,
    format_elapsed,
    settle_worker,
    wait_for_close,
)
from caobunga.worker import STATUS_ERROR, STATUS_EXITED, STATUS_PROCESSING, Session


def _row(
    provider: str,
    *,
    installed: bool = True,
    leftover: Leftover = Leftover.UNKNOWN,
    remaining: float | None = None,
) -> ProviderUsage:
    return ProviderUsage(
        provider=provider,
        installed=installed,
        leftover=leftover,
        remaining=remaining,
    )


def test_registry_pin_wins_over_leftover(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, provider="claude_code", model="opus")
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.provider == "claude_code"
    assert result.model == "opus"
    assert fake.created is not None
    assert fake.created["provider"] == "claude_code"
    assert fake.created["model"] == "opus"


def test_leftover_available_beats_v1_order(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    usage = [
        _row("grok_cli", leftover=Leftover.EXHAUSTED),
        _row("claude_code", leftover=Leftover.AVAILABLE, remaining=0.2),
        _row("codex"),
        _row("cursor_cli"),
    ]
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2, usage=usage)
    assert result is not None
    assert result.provider == "claude_code"
    assert result.model == "opus"


def test_registry_allow_list_steers_the_launch(tmp_path: Path, caobunga_home: Path) -> None:
    # Without the list, all-unknown leftover hands this to grok_cli (CBN-EPIC-9.4).
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2, usage=_usage())
    assert result is not None
    assert result.provider == "claude_code"
    assert result.model == "opus"
    assert fake.created is not None
    assert fake.created["provider"] == "claude_code"


def test_allow_list_still_yields_to_leftover(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    usage = [
        _row("claude_code"),
        _row("codex", leftover=Leftover.AVAILABLE, remaining=0.5),
    ]
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2, usage=usage)
    assert result is not None
    assert result.provider == "codex"


def test_no_eligible_member_of_allow_list_is_unlaunchable(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    fake = FakeWorker()
    usage = [
        _row("grok_cli"),  # eligible, but the operator did not declare it
        _row("claude_code", leftover=Leftover.EXHAUSTED),
        _row("codex", installed=False),
    ]
    with fake as worker, pytest.raises(UserError, match="no eligible worker in") as exc:
        _run("app", store=store, worker=worker, poll_interval=0, usage=usage)
    assert exc.value.exit_code == 5
    assert fake.created is None
    assert store.last_run("app") is None


def test_no_eligible_worker_is_unlaunchable(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    usage = [
        _row("grok_cli", installed=False),
        _row("claude_code", leftover=Leftover.EXHAUSTED),
        _row("codex", installed=False),
        _row("cursor_cli", leftover=Leftover.EXHAUSTED),
    ]
    with fake as worker, pytest.raises(UserError, match="no eligible worker") as exc:
        _run("app", store=store, worker=worker, poll_interval=0, usage=usage)
    assert exc.value.exit_code == 5
    assert fake.created is None
    assert store.last_run("app") is None


def test_uninstalled_pin_is_unlaunchable(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, provider="grok_cli")
    fake = FakeWorker()
    usage = [_row("grok_cli", installed=False)]
    with fake as worker, pytest.raises(UserError, match="not installed") as exc:
        _run("app", store=store, worker=worker, poll_interval=0, usage=usage)
    assert exc.value.exit_code == 5
    assert fake.created is None
    assert store.last_run("app") is None


class _IdleWorker:
    """Worker that stays `processing`; `wait_for_close` returns only on a close."""

    def status(self, session: Session) -> str:  # noqa: ARG002
        return STATUS_PROCESSING

    def exit_code(self, session: Session) -> int | None:  # noqa: ARG002
        return None

    def stderr_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return ()

    def stdout_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return ()

    def activity(self, session: Session) -> tuple[int, str | None]:  # noqa: ARG002
        return (0, None)


def test_wait_for_close_survives_transient_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-168: a poll whose read hits a vanished path answers "not yet", not exit 1.

    Models the 2026-09-12 CBN-159 run: `inspect_close` raised `FileNotFoundError`
    on the worker's archive move and the exception escaped the loop, so no run
    record was written and the crash's cleanup killed a worker that had finished.
    """
    calls = 0
    closed = CloseSnapshot(outcome=Outcome.CLOSED, commit_sha="abc", summary="done")

    def flaky_inspect(*args: object, **kwargs: object) -> CloseSnapshot | None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError(tmp_path / ".flaitron" / "tasknote" / "X-1.md")
        return closed

    monkeypatch.setattr("caobunga.wait.inspect_close", flaky_inspect)
    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        _IdleWorker(),  # type: ignore[arg-type]
        session,
        tmp_path,
        "X-1",
        "start",
        timeout=10.0,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert snap is closed
    assert calls == 2


def test_wait_for_close_survives_transient_git_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-269: one hung `git` read mid-run answers "not yet", not an exit-2 abort."""
    calls = 0
    closed = CloseSnapshot(outcome=Outcome.CLOSED, commit_sha="abc", summary="done")

    def flaky_inspect(*args: object, **kwargs: object) -> CloseSnapshot | None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GitUnreadable(tmp_path)
        return closed

    monkeypatch.setattr("caobunga.wait.inspect_close", flaky_inspect)
    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        _IdleWorker(),  # type: ignore[arg-type]
        session,
        tmp_path,
        "X-1",
        "start",
        timeout=10.0,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert snap is closed
    assert calls == 2


class _StatusWorker(_IdleWorker):
    """Worker pinned to one status, for the `wait_for_close` status-ladder pins."""

    def __init__(self, state: str) -> None:
        self.state = state

    def status(self, session: Session) -> str:  # noqa: ARG002
        return self.state


def test_wait_for_close_exited_keeps_a_last_action_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-shot worker whose close lands on its last action returns it, not WORKER_EXITED.

    Not a duplicate of `test_worker_exit_after_close_still_lands_closed`: that one
    closes before the first poll and never reads `status()`; this one reaches the
    EXITED re-poll branch.
    """
    calls = 0
    closed = CloseSnapshot(outcome=Outcome.CLOSED, commit_sha="abc", summary="done")

    def late_inspect(*args: object, **kwargs: object) -> CloseSnapshot | None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return closed if calls == 2 else None

    monkeypatch.setattr("caobunga.wait.inspect_close", late_inspect)
    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        _StatusWorker(STATUS_EXITED),  # type: ignore[arg-type]
        session,
        tmp_path,
        "X-1",
        "start",
        timeout=10.0,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert snap is closed
    assert calls == 2


def test_wait_for_close_stops_the_observer_on_a_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An escalation out of the loop still stops and joins the watchdog observer."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    observers: list[object] = []
    real_start_watch = wait._start_watch

    def capturing(root: Path, wake: threading.Event) -> object:
        observer = real_start_watch(root, wake)
        observers.append(observer)
        return observer

    monkeypatch.setattr(wait, "_start_watch", capturing)
    session = Session(session_name="s", terminal_id="t", provider=None)
    with pytest.raises(WorkerError) as exc:
        wait_for_close(
            _StatusWorker(STATUS_ERROR),  # type: ignore[arg-type]
            session,
            repo,
            "CBN-011",
            start,
            timeout=10.0,
            poll_interval=5.0,
            sleep=lambda _: None,
        )
    assert exc.value.escalation is Escalation.WORKER_ERROR
    (observer,) = observers
    assert observer is not None
    assert not observer.is_alive()  # type: ignore[attr-defined]


_PHASE_NAMES = {1: "Discovery", 2: "Execution", 3: "Testing & Linting", 4: "Closure"}


def _phase_note(*phases: list[bool], emoji: bool = False) -> str:
    """A minimal open tasknote with the given per-phase checkbox states."""
    parts = ["---\nstatus: in-progress\n---\n\n"]
    marks = {1: "📝", 2: "🛠️", 3: "🧪", 4: "🚀"}
    for number, boxes in enumerate(phases, start=1):
        prefix = f"{marks[number]} " if emoji else ""
        parts.append(f"## {prefix}Phase {number}: {_PHASE_NAMES[number]}\n\n")
        for done in boxes:
            tick = "x" if done else " "
            parts.append(f"- [{tick}] step\n")
        parts.append("\n")
    return "".join(parts)


def test_phase_progress_reaches_phase_1_by_heading() -> None:
    assert phase_progress(_phase_note([False], [False])) == "1 discovery"
    assert phase_progress(_phase_note([False], [False], emoji=True)) == "1 discovery"


def test_phase_progress_advances_when_previous_phase_is_complete() -> None:
    assert phase_progress(_phase_note([True, True], [False])) == "2 execution"
    assert phase_progress(_phase_note([True], [True, False], [False])) == "2 execution"
    assert phase_progress(_phase_note([True], [True], [False])) == "3 testing"
    assert phase_progress(_phase_note([True], [True], [True], [True])) == "4 closure"


def test_phase_progress_skips_ahead_on_a_later_tick() -> None:
    assert phase_progress(_phase_note([False], [False], [True])) == "3 testing"


def test_phase_progress_ignores_a_fenced_phase_heading() -> None:
    """CBN-344: a quoted `## Phase 4:` inside a fence is body, not a phase."""
    quoted = "```markdown\n## 🚀 Phase 4: Closure\n\n- [x] done\n```\n\n"
    note = _phase_note([False]) + quoted
    assert phase_progress(note) == "1 discovery"


def test_phase_progress_ignores_fenced_checkbox_lines() -> None:
    """CBN-346: a quoted `- [ ]` / `- [x]` inside a fence is not a phase checkbox."""
    head = "---\nstatus: in-progress\n---\n\n"
    unticked_quote = "```markdown\n- [ ] quoted\n```\n\n"
    ticked_quote = "```markdown\n- [x] quoted\n```\n\n"
    done_phase_1 = f"{head}## 📝 Phase 1: Discovery\n\n- [x] step\n\n{unticked_quote}"
    assert phase_progress(f"{done_phase_1}## 🛠️ Phase 2: Execution\n\n- [ ] step\n") == (
        "2 execution"
    )
    open_phase_1 = f"{head}## 📝 Phase 1: Discovery\n\n- [ ] step\n\n"
    assert phase_progress(f"{open_phase_1}## 🛠️ Phase 2: Execution\n\n{ticked_quote}") == (
        "1 discovery"
    )


def test_phase_progress_micro_tasknote_is_notes() -> None:
    body = "---\nstatus: in-progress\n---\n\n## ⚡ Notes\n\n**Implementation:** doing it\n"
    assert phase_progress(body) == "notes"
    assert phase_progress("---\nstatus: in-progress\n---\n\n# body\n") is None


def test_wait_for_close_echoes_phase_progress(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _phase_note([False], [False])})
    start = git_head(repo)
    assert start is not None
    lines: list[str] = []
    ticks = {"n": 0}

    def advance(_dt: float) -> None:
        ticks["n"] += 1
        path = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        if ticks["n"] == 1:
            path.write_text(_phase_note([True], [False]), encoding="utf-8")
        elif ticks["n"] == 2:
            path.write_text(_phase_note([True], [True]), encoding="utf-8")
        else:
            _close_worker(repo)

    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        _IdleWorker(),  # type: ignore[arg-type]
        session,
        repo,
        "CBN-011",
        start,
        timeout=10_000.0,
        poll_interval=0,
        sleep=advance,
        echo=lines.append,
        clock=lambda: 35.0 * ticks["n"],
    )
    assert snap.outcome is Outcome.CLOSED
    # CBN-EPIC-449.2: each phase line carries the wait's elapsed time.
    assert [line for line in lines if line.startswith("phase:")] == [
        "phase: 1 discovery · 0s",
        "phase: 2 execution · 35s",
    ]


def test_wait_for_close_skips_a_missing_note_then_echoes(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    lines: list[str] = []
    ticks = {"n": 0}

    def advance(_dt: float) -> None:
        ticks["n"] += 1
        path = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        if ticks["n"] == 1:
            path.write_text(_phase_note([False]), encoding="utf-8")
        else:
            _close_worker(repo)

    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        _IdleWorker(),  # type: ignore[arg-type]
        session,
        repo,
        "CBN-011",
        start,
        timeout=10.0,
        poll_interval=0,
        sleep=advance,
        echo=lines.append,
    )
    assert snap.outcome is Outcome.CLOSED
    assert [line.split(" · ")[0] for line in lines if line.startswith("phase:")] == [
        "phase: 1 discovery"
    ]


class _ActiveWorker(_IdleWorker):
    """An idle worker whose progress trail reports `activity` (CBN-EPIC-449.3)."""

    def __init__(self, activity: tuple[int, str | None]) -> None:
        self._activity = activity

    def activity(self, session: Session) -> tuple[int, str | None]:  # noqa: ARG002
        return self._activity


def _heartbeat_lines(
    repo: Path,
    start: str,
    times: list[float],
    worker: _IdleWorker | None = None,
    started: float | None = None,
) -> list[str]:
    """Run the close wait over a scripted clock; the worker closes after the last time."""
    lines: list[str] = []
    ticks = {"n": 0}

    def advance(_dt: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == len(times) - 1:
            _close_worker(repo)

    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        worker or _IdleWorker(),  # type: ignore[arg-type]
        session,
        repo,
        "CBN-011",
        start,
        timeout=10_000.0,
        poll_interval=0,
        sleep=advance,
        echo=lines.append,
        clock=lambda: times[min(ticks["n"], len(times) - 1)],
        started=started,
    )
    assert snap.outcome is Outcome.CLOSED
    return [line for line in lines if line.startswith("progress:")]


def test_wait_for_close_echoes_a_mark_due_at_the_close(tmp_path: Path) -> None:
    """CBN-EPIC-449.5: a mark due when the close lands still echoes, so the post-close
    `SuiteHeartbeat` (which resumes at the next mark) leaves no two-mark gap."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    assert _heartbeat_lines(repo, start, [0.0, 50.0, 61.0]) == [
        "progress: 1m01s elapsed · phase none yet"
    ]


def test_wait_for_close_measures_from_the_callers_start(tmp_path: Path) -> None:
    """A passed `started` is the zero `SuiteHeartbeat` shares (CBN-EPIC-449.5)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    assert _heartbeat_lines(repo, start, [0.0, 30.0], started=-750.0) == [
        "progress: 12m30s elapsed · phase none yet",
        "progress: 13m00s elapsed · phase none yet",
    ]


def test_wait_for_close_heartbeat_every_sixty_seconds(tmp_path: Path) -> None:
    """CBN-EPIC-449.2: one `progress:` line per 60s of wait, none before the first mark."""
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _phase_note([False], [False])})
    start = git_head(repo)
    assert start is not None
    times = [0.0, 25.0, 50.0, 75.0, 100.0, 125.0, 150.0]
    assert _heartbeat_lines(repo, start, times) == [
        "progress: 1m15s elapsed · phase 1 discovery",
        "progress: 2m05s elapsed · phase 1 discovery",
    ]


def test_wait_for_close_heartbeat_before_any_phase_and_after_oversleep(tmp_path: Path) -> None:
    """No note yet reads `none yet`; a poll that overslept three marks echoes once."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    times = [0.0, 59.0, 200.0, 230.0, 245.0, 250.0]
    assert _heartbeat_lines(repo, start, times) == [
        "progress: 3m20s elapsed · phase none yet",
        "progress: 4m05s elapsed · phase none yet",
    ]


@pytest.mark.parametrize(
    ("activity", "suffix"),
    [
        ((0, None), ""),
        ((1, "tool: Read a.py"), " · 1 tool call · tool: Read a.py"),
        ((14, "tool: Bash uv run pytest -q"), " · 14 tool calls · tool: Bash uv run pytest -q"),
        ((3, None), " · 3 tool calls"),
    ],
)
def test_wait_for_close_heartbeat_carries_worker_activity(
    tmp_path: Path, activity: tuple[int, str | None], suffix: str
) -> None:
    """CBN-EPIC-449.3: tool-call count + last `tool:` line trail the heartbeat; none at zero."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    assert _heartbeat_lines(repo, start, [0.0, 61.0, 62.0], _ActiveWorker(activity)) == [
        f"progress: 1m01s elapsed · phase none yet{suffix}"
    ]


def _suite_beat(
    times: list[float], stages: list[str], echo: Callable[[str], None] | None = None
) -> tuple[list[str], list[float]]:
    """Drive `SuiteHeartbeat.beat` on a fake clock started at 0: the first time is the
    close, each wait jumps the clock to the next time and sets the next stage, and the
    wait after the last time stops the beat. Returns `(lines, wait timeouts)`."""
    now = {"t": times[0]}
    lines: list[str] = []
    timeouts: list[float] = []
    beat = SuiteHeartbeat(echo or lines.append, 0.0, clock=lambda: now["t"])
    pending = list(zip(times[1:], stages, strict=True))

    def wait(timeout: float) -> bool:
        timeouts.append(timeout)
        if not pending:
            return True
        now["t"], beat.stage = pending.pop(0)
        return False

    beat.beat(wait)
    return lines, timeouts


def test_suite_heartbeat_continues_the_close_wait_marks() -> None:
    """CBN-EPIC-449.5: after a close at 12m30s, the beat resumes at 13m and names its stage."""
    lines, timeouts = _suite_beat(
        [750.0, 780.0, 840.0, 900.0], ["settle", "suite test", "suite lint"]
    )
    assert lines == [
        "progress: 13m00s elapsed · settle",
        "progress: 14m00s elapsed · suite test",
        "progress: 15m00s elapsed · suite lint",
    ]
    assert timeouts == [30.0, 60.0, 60.0, 60.0]


def test_suite_heartbeat_echoes_once_after_an_oversleep() -> None:
    lines, timeouts = _suite_beat([750.0, 1000.0, 1020.0], ["suite test", "suite test"])
    assert lines == [
        "progress: 16m40s elapsed · suite test",
        "progress: 17m00s elapsed · suite test",
    ]
    assert timeouts == [30.0, 20.0, 60.0]


def test_suite_heartbeat_stops_on_a_broken_stdout() -> None:
    def broken(_line: str) -> None:
        raise BrokenPipeError

    lines, timeouts = _suite_beat([750.0, 780.0, 840.0], ["settle", "settle"], echo=broken)
    assert lines == []
    assert timeouts == [30.0]


@pytest.mark.parametrize(("echo", "started"), [(None, 0.0), (print, None)])
def test_suite_heartbeat_is_a_no_op_without_echo_or_start(
    echo: Callable[[str], None] | None, started: float | None
) -> None:
    before = threading.active_count()

    def never(_timeout: float) -> bool:
        raise AssertionError("no beat without an echo and a start")

    with SuiteHeartbeat(echo, started) as beat:
        assert threading.active_count() == before
        beat.beat(never)


def test_suite_heartbeat_thread_stops_and_joins_on_exit() -> None:
    """The real ticker: no line before the first mark, and nothing left running after the `with`."""
    before = threading.active_count()
    lines: list[str] = []
    with SuiteHeartbeat(lines.append, time.monotonic()):
        assert threading.active_count() == before + 1
    assert threading.active_count() == before
    assert lines == []


def test_wait_for_close_heartbeat_clips_a_long_tool_line(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    long_line = "tool: Bash " + "word " * 100
    (line,) = _heartbeat_lines(repo, start, [0.0, 61.0, 62.0], _ActiveWorker((2, long_line)))
    tool = line.split(" · ")[-1]
    assert tool.startswith("tool: Bash word")
    assert tool.endswith("…")
    assert len(tool) <= ACTIVITY_MAX + 1


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (42.9, "42s"), (60, "1m00s"), (192, "3m12s"), (3599, "59m59s"), (3720, "1h02m")],
)
def test_format_elapsed(seconds: float, expected: str) -> None:
    assert format_elapsed(seconds) == expected


def test_instant_close_emits_no_phase_echo(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _phase_note([False], [False])})
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert not any(line.startswith("phase:") for line in lines)


def _effort_seen(tmp_path: Path, home: Path, plan_model: str) -> tuple[str | None, str | None]:
    """Run one gated task whose PLAN line carries `[<plan_model>]`; report launch kwargs."""
    plan = OPEN_PLAN.replace("**CBN-011** [medium]", f"**CBN-011** [{plan_model}]")
    repo = _git_repo(tmp_path, plan)
    store = Store(home)
    _register(store, repo, slug=plan_model, provider="grok_cli")
    worker = _BusyWorker(lambda: _close_worker(repo), busy_polls=0)
    result = _run(plan_model, store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    return worker.model, worker.effort


def test_loop_passes_the_class_effort_to_launch(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-EPIC-15.3. The rung reaches the worker seam, from the PLAN token and provider."""
    model, effort = _effort_seen(tmp_path, caobunga_home, "light")
    assert effort == "medium"
    assert model == "grok-4.7"


def test_grok_classes_take_distinct_rungs_on_the_flat_id(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """End to end on grok: one model id, light/medium/heavy are medium/high/xhigh.

    CBN-360 shared `high` for light and medium. CBN-416 follows the v7.4.0 cell,
    so the three classes dispatch distinct rungs again.
    """
    seen = [_effort_seen(tmp_path / c, caobunga_home, c) for c in ("light", "medium", "heavy")]
    assert {model for model, _ in seen} == {"grok-4.7"}
    assert [effort for _, effort in seen] == ["medium", "high", "xhigh"]


def test_settle_worker_gives_up_after_the_grace(tmp_path: Path) -> None:
    """CBN-EPIC-9.5 F1. A grace, not a gate — a wedged worker must not add an abort."""
    worker = _BusyWorker(lambda: None, busy_polls=10**6)
    session = worker.launch(tmp_path)
    settle_worker(worker, session, poll_interval=0.001, sleep=lambda _s: None, grace=0.05)
    assert worker.polls > 0
    assert not worker.settled


def test_settle_worker_returns_when_status_raises(tmp_path: Path) -> None:
    """A worker that cannot be polled is not a reason to abort a successful close."""

    class _Unpollable(_BusyWorker):
        def status(self, session: Session) -> str:  # noqa: ARG002
            raise WorkerError("terminal is gone")

    worker = _Unpollable(lambda: None, busy_polls=0)
    session = worker.launch(tmp_path)
    settle_worker(worker, session, poll_interval=0, sleep=lambda _s: None, grace=5)


def test_usage_limit_repick_launches_next_allow_list_member(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-213: clean-tree usage-limit on A → B runs, one closed record."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    lines: list[str] = []
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo))
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
        echo=lines.append,
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.provider == "codex"
    assert worker.launch_count == 2
    assert worker.providers == ["claude_code", "codex"]
    assert len(store.list_runs("app")) == 1
    assert "usage-limit: claude_code exhausted this invocation; re-picking" in lines
    # CBN-EPIC-449.2: each launch names its own pick.
    assert [line.split(" · ")[1] for line in lines if line.startswith("launch:")] == [
        "provider claude_code",
        "provider codex",
    ]
    assert result.exhausted_providers == ["claude_code"]
    assert store.list_runs("app")[0].exhausted_providers == ["claude_code"]


def test_usage_limit_repick_resolves_effort_for_the_new_provider(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-332: the rung is the re-picked provider's column, not the first pick's.

    The plan token is light. After CBN-416, claude and codex medium rungs match,
    so a medium row could not show the pivot.
    """
    plan = OPEN_PLAN.replace("[medium]", "[light]")
    repo = _git_repo(tmp_path, plan)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo))
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert worker.providers == ["claude_code", "codex"]
    first, second = (EFFORT[p][TaskClass.LIGHT] for p in ("claude_code", "codex"))
    assert first != second
    assert worker.efforts == [first, second]


def test_usage_limit_repick_also_pivots_on_worker_exited(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Exit 0 with a usage-limit last line is still a picker signal (CBN-162's example)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo),
        stderr="",
        stdout="Error: usage limit reached",
        code=0,
    )
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.provider == "codex"
    assert worker.launch_count == 2


def test_usage_limit_repick_reads_past_a_multiline_json_refusal(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-282: Grok's 402 is multi-line JSON whose last line is a bare `}`.

    Verbatim shape from a fleet repo's FE-104 (2026-10-06). The allow-list puts grok
    first (the default order is codex > claude > grok), so the re-pick falls to claude_code.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["grok_cli", "claude_code"])
    block = (
        "Internal error: {\n"
        '  "message": "API error (status 402 Payment Required): '
        'Grok Build usage balance exhausted",\n'
        '  "http_status": 402\n'
        "}\n"
    )
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo), stderr=block + "Error: " + block
    )
    assert _tail(block)[-1] == "}"  # the line the pre-CBN-282 matcher saw
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("grok_cli", "claude_code"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert worker.providers == ["grok_cli", "claude_code"]
    assert result.provider == "claude_code"


def test_usage_limit_repick_pivots_on_a_colour_split_refusal(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-397: a colour code inside the marker phrase still reads as a refusal."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo), stderr="Error: usage \x1b[1mlimit\x1b[0m reached"
    )
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.provider == "codex"
    assert worker.launch_count == 2
    assert result.exhausted_providers == ["claude_code"]


def test_usage_limit_match_never_spans_two_lines(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-282 review: a marker split across a line break is not a refusal — escalate."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo), stderr="exceeded usage\nlimit=3"
    )
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert worker.launch_count == 1


def test_usage_limit_repick_skips_hard_provider_pin(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, provider="claude_code")
    lines: list[str] = []
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), always=True)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
        echo=lines.append,
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert worker.launch_count == 1
    assert not any(line.startswith("usage-limit:") for line in lines)
    assert "stderr: Error: usage limit reached" in (result.summary or "")


def test_usage_limit_repick_skips_dirty_tree(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo),
        dirty=lambda: (repo / "stray.txt").write_text("left behind\n", encoding="utf-8"),
    )
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.launch_count == 1
    assert result.provider == "claude_code"


def test_usage_limit_repick_skips_a_moved_head(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-260. The worker committed, then hit its limit: clean tree, HEAD moved.

    Porcelain alone read that as "left nothing" and relaunched the task on top
    of the worker's own commit.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    start = git_head(repo)

    def commit_then_refuse() -> None:
        (repo / "work.txt").write_text("half done\n", encoding="utf-8")
        _commit(repo, "wip: CBN-011")

    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), dirty=commit_then_refuse)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert git_dirt(repo) == []
    assert git_head(repo) != start
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.launch_count == 1
    assert result.provider == "claude_code"


def test_usage_limit_repick_skips_an_unreadable_head(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-269. An unreadable HEAD is not an unmoved one: escalate, never pivot."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    reads: list[Path] = []

    def head_then_unreadable(root: Path) -> str | None:
        reads.append(root)
        if len(reads) <= 2:
            return git_head(root)
        raise GitUnreadable(root)

    # Reads 1-2 are `_run_gated`'s start sha (loop), before and after the baseline
    # suite (CBN-420); read 3 is `_should_repick`'s (launch).
    monkeypatch.setattr("caobunga.loop.git_head", head_then_unreadable)
    monkeypatch.setattr("caobunga.launch.git_head", head_then_unreadable)
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo))
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert len(reads) == 3
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.launch_count == 1


def test_usage_limit_repick_skips_non_matching_output(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo),
        stderr="waiting on a rate limit",
    )
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.launch_count == 1
    assert "rate limit" in (result.summary or "")


def test_usage_limit_repick_escalates_when_every_member_refuses(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    lines: list[str] = []
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), always=True)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
        echo=lines.append,
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert worker.launch_count == 2
    assert worker.providers == ["claude_code", "codex"]
    assert result.provider == "codex"
    assert len(store.list_runs("app")) == 1
    assert lines.count("usage-limit: claude_code exhausted this invocation; re-picking") == 1
    assert lines.count("usage-limit: codex exhausted this invocation; re-picking") == 1


class _UnlaunchableAfterWorker(_UsageLimitThenCloseWorker):
    """Launch `unlaunchable_from`-th onward raises `Unlaunchable`, as
    `LocalWorker._binary` does for a binary gone since `which` (CBN-316)."""

    def __init__(self, on_close: Callable[[], object], *, unlaunchable_from: int) -> None:
        super().__init__(on_close)
        self._unlaunchable_from = unlaunchable_from

    def launch(self, cwd: Path | str, **kwargs: object) -> Session:
        if self.launch_count + 1 >= self._unlaunchable_from:
            self.launch_count += 1
            raise Unlaunchable(f"provider {kwargs['provider']!r} is not installed")
        return super().launch(cwd, **kwargs)  # type: ignore[arg-type]


def test_usage_limit_repick_launch_unlaunchable_fails_with_the_refusal(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-316: the re-pick cannot launch — the worker that ran still gets its record."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    lines: list[str] = []
    worker = _UnlaunchableAfterWorker(lambda: _close_worker(repo), unlaunchable_from=2)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
        echo=lines.append,
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert "usage limit reached" in (result.summary or "")
    assert result.provider == "claude_code"
    assert result.exhausted_providers == ["claude_code"]
    assert len(store.list_runs("app")) == 1
    assert "usage-limit: re-pick codex cannot launch: provider 'codex' is not installed" in lines
    assert worker.events == ["launch:claude_code", "shutdown:claude_code"]


class _SendRefusesOnRepickWorker(_UsageLimitThenCloseWorker):
    """The re-pick's `launch` reserves a session, then `send` raises `UserError`."""

    def send(self, session: Session, message: str) -> None:
        if self.launch_count >= 2:
            raise UserError("message must not be empty")
        super().send(session, message)


def test_usage_limit_repick_send_usererror_retires_the_unrun_session(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-316 review: a reserved-but-unsent re-pick session is retired, not attributed."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _SendRefusesOnRepickWorker(lambda: _close_worker(repo))
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.provider == "claude_code"
    assert result.exhausted_providers == ["claude_code"]
    assert result.suite is None
    assert worker.events == [
        "launch:claude_code",
        "shutdown:claude_code",
        "launch:codex",
        "shutdown:codex",
    ]


def test_first_launch_unlaunchable_still_refuses_without_record(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-316: with no worker run yet there is nothing to record — exit 5 refusal."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UnlaunchableAfterWorker(lambda: _close_worker(repo), unlaunchable_from=1)
    with pytest.raises(Unlaunchable) as exc:
        _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            usage=_usage("claude_code", "codex"),
        )
    assert exc.value.exit_code == 5
    assert store.last_run("app") is None


# Session lifecycle (CBN-EPIC-244.4): characterization pins ahead of `.5`
# splitting `_run_gated`. Each launched session is shut down exactly once, a
# refused one before the next launch; `close()` only on a client the loop built.


def _failing_card(_store: Store, _run: object) -> Path:
    raise WorkerError("status card write failed: boom")


def test_lifecycle_repick_shuts_refused_session_before_next_launch(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo))
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    # Order, not count: the refused session goes before the next launch, and
    # the injected client is never closed by the loop.
    assert worker.events == [
        "launch:claude_code",
        "shutdown:claude_code",
        "launch:codex",
        "shutdown:codex",
    ]


def test_lifecycle_failed_run_shuts_its_session_once(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, provider="claude_code")
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), always=True)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.events == ["launch:claude_code", "shutdown:claude_code"]


def test_lifecycle_all_refuse_shuts_each_session_once(tmp_path: Path, caobunga_home: Path) -> None:
    """Every session is shut down on the re-pick path; `finally` finds none left."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), always=True)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.events == [
        "launch:claude_code",
        "shutdown:claude_code",
        "launch:codex",
        "shutdown:codex",
    ]


def test_lifecycle_finish_raising_still_shuts_the_session(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code"])
    worker = _BusyWorker(lambda: _close_worker(repo), busy_polls=0)
    with pytest.raises(ReportError, match="status card write failed"):
        _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            usage=_usage("claude_code"),
            publish_status=_failing_card,
        )
    assert worker.events == ["launch:claude_code", "shutdown:claude_code"]


def test_lifecycle_shutdown_error_is_swallowed_on_both_paths(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """The re-pick shutdown and the `finally` shutdown both raise; the run still closes."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), shutdown_fails=True)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.provider == "codex"
    assert worker.events == [
        "launch:claude_code",
        "shutdown:claude_code",
        "launch:codex",
        "shutdown:codex",
    ]


@pytest.mark.parametrize("publish_status", [None, _failing_card])
def test_lifecycle_closes_a_client_the_loop_built(
    tmp_path: Path,
    caobunga_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    publish_status: Callable[[Store, object], Path] | None,
) -> None:
    """`worker=None` → `select_worker(home)`: closed after the shutdown, even when `finish` raises.

    Patched on `caobunga.local`, which `select_worker` imports late — not on
    `caobunga.loop`, so it adds nothing to `LOOP_PATCHED`.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code"])
    built = _BusyWorker(lambda: _close_worker(repo), busy_polls=0)
    monkeypatch.delenv("CAOBUNGA_WORKER", raising=False)
    monkeypatch.setattr("caobunga.local.LocalWorker", lambda **_: built)
    try:
        _run(
            "app",
            store=store,
            poll_interval=0,
            timeout=2,
            usage=_usage("claude_code"),
            publish_status=publish_status,
        )
    except ReportError:
        assert publish_status is _failing_card
    else:
        assert publish_status is None
    assert built.events == ["launch:claude_code", "shutdown:claude_code", "close"]


def test_start_watch_returns_none_with_nothing_to_watch(tmp_path: Path) -> None:
    """No `.flaitron` / `.git` → no observer, and `wait_for_close` falls back to `sleep`."""
    assert wait._start_watch(tmp_path, threading.Event()) is None


def test_wait_for_close_wakes_on_watchdog_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`poll_interval > 0` characterization: the observer, the wake, and the stop.

    Every other `wait_for_close` caller in this file passes `poll_interval=0`,
    which never starts the observer (CBN-013 chose "close on `send` so the first
    inspect hits"). Here the close lands from another thread while the loop is
    blocked in `wake.wait`; the elapsed bound is the assertion — without a
    working wake the loop sits out the whole `poll_interval` and only then polls.
    """
    # Exercise real filesystem events with watchdog's portable backend. Native
    # FSEvents can deliver no events on a restricted macOS host (CBN-437).
    monkeypatch.setattr("watchdog.observers.Observer", lambda: PollingObserver(timeout=0.05))
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    observers: list[object] = []
    real_start_watch = wait._start_watch

    def capturing(root: Path, wake: threading.Event) -> object:
        observer = real_start_watch(root, wake)
        observers.append(observer)
        return observer

    monkeypatch.setattr(wait, "_start_watch", capturing)
    slept: list[float] = []
    poll_interval = 8.0
    closer = threading.Timer(0.3, _close_worker, args=(repo,))
    session = Session(session_name="s", terminal_id="t", provider=None)
    began = time.monotonic()
    closer.start()
    try:
        snap = wait_for_close(
            _IdleWorker(),  # type: ignore[arg-type]
            session,
            repo,
            "CBN-011",
            start,
            timeout=poll_interval,
            poll_interval=poll_interval,
            sleep=slept.append,
        )
    finally:
        closer.join()
    elapsed = time.monotonic() - began
    assert snap.outcome is Outcome.CLOSED
    assert elapsed < poll_interval / 2, elapsed
    assert slept == []
    (observer,) = observers
    assert observer is not None
    assert not observer.is_alive()  # type: ignore[attr-defined]


_BASELINE = ["CBN-011", "CBN-012"]
_FILED_PLAN = OPEN_PLAN + "\n- [ ] **CBN-099** [medium] | worker-filed — filed mid-run.\n"


def _file_row(repo: Path) -> None:
    (repo / ".flaitron" / "PLAN.md").write_text(_FILED_PLAN, encoding="utf-8")


@pytest.mark.parametrize(
    "reason",
    [Escalation.WORKER_EXITED, Escalation.TIMEOUT, Escalation.WORKER_ERROR],
)
def test_failed_snapshot_carries_rows_absent_from_the_baseline(
    tmp_path: Path, reason: Escalation
) -> None:
    """CBN-436. The three escalation reasons share one diff, the close/park one."""
    repo = _git_repo(tmp_path, _FILED_PLAN)
    snap = _failed_from_worker(WorkerError("died", reason), repo, "CBN-011", _BASELINE)
    assert snap.outcome is Outcome.FAILED
    assert snap.escalation is reason
    assert snap.new_rows == [NewRow(task_id="CBN-099")]


def test_failed_snapshot_leaves_rows_unset_when_nothing_new_appeared(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    snap = _failed_from_worker(
        WorkerError("died", Escalation.WORKER_EXITED), repo, "CBN-011", _BASELINE
    )
    assert snap.new_rows is None


def test_failed_snapshot_without_a_baseline_is_not_measured(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, _FILED_PLAN)
    snap = _failed_from_worker(WorkerError("died", Escalation.WORKER_EXITED), repo, "CBN-011", None)
    assert snap.new_rows is None


def test_failed_snapshot_survives_an_unreadable_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad PLAN must not drop the failure record (CBN-436)."""
    repo = _git_repo(tmp_path, _FILED_PLAN)
    monkeypatch.setattr("caobunga.launch.try_read_project", lambda _root: None)
    snap = _failed_from_worker(WorkerError("died", Escalation.TIMEOUT), repo, "CBN-011", _BASELINE)
    assert snap.outcome is Outcome.FAILED
    assert snap.escalation is Escalation.TIMEOUT
    assert snap.new_rows is None


def test_a_died_workers_filing_is_on_the_record_and_the_next_refusal(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """The loop threads the launch baseline, so the next refusal can name the filing."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    died = _ExitedWorker(1, "died", on_send=lambda: _file_row(repo))
    result = _run("app", store=store, worker=died, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.new_rows == [NewRow(task_id="CBN-099")]

    with FakeWorker() as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    message = str(exc.value)
    assert exc.value.exit_code == 2
    assert "flaitron filing artifacts a dispatched worker writes" in message
    assert f"git -C {repo} add -- .flaitron/PLAN.md && " in message
    assert '"chore: commit worker filing" -- .flaitron/PLAN.md — then re-run' in message
    assert worker.created is None


def test_a_row_filed_at_shutdown_is_still_on_the_failure_record(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """The post-stop re-read, not the pre-settle snapshot, is what the record keeps."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    class _FileOnShutdown(_ExitedWorker):
        def shutdown(self, session: Session) -> None:
            _file_row(repo)
            super().shutdown(session)

    result = _run(
        "app",
        store=store,
        worker=_FileOnShutdown(1, "died"),
        poll_interval=0,
        timeout=2,
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.new_rows == [NewRow(task_id="CBN-099")]
