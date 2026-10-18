"""Local subprocess worker: argv recipes and session lifecycle. Never spawns a real CLI."""

from __future__ import annotations

import itertools
import json
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

import caobunga.local as local_module
from caobunga.errors import Unlaunchable, UserError, WorkerError
from caobunga.local import (
    CLAUDE_GUARD_SETTINGS,
    EFFORT_SLOT,
    FOREGROUND_HOOK_COMMAND,
    RECIPES,
    LocalWorker,
    build_argv,
    compact_stream_line,
    hooks_available,
)
from caobunga.usage import BINARIES, WorkerProvider
from caobunga.wait import is_usage_limit_line
from caobunga.worker import STATUS_ERROR, STATUS_EXITED, Session

_FAKE_PIDS = itertools.count(90000)


class FakeProc:
    """Stands in for subprocess.Popen. `code` None means still running."""

    def __init__(
        self, code: int | None = None, *, stubborn: bool = False, wait_forever: bool = False
    ) -> None:
        self.pid = next(_FAKE_PIDS)
        self.code = code
        self.stubborn = stubborn
        self.wait_forever = wait_forever
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        if not self.stubborn:
            self.code = -15

    def kill(self) -> None:
        self.killed = True
        if not self.wait_forever:
            self.code = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.code is None:
            raise subprocess.TimeoutExpired("worker", timeout or 0)
        return self.code


class Spawner:
    """Records every argv/cwd/log_path triple and hands back a scripted process."""

    def __init__(self, *procs: FakeProc) -> None:
        self.queue = list(procs) or [FakeProc()]
        self.calls: list[tuple[list[str], Path, Path | None]] = []

    def __call__(
        self, argv: Sequence[str], cwd: Path, log_path: Path | None = None
    ) -> subprocess.Popen[bytes]:
        self.calls.append((list(argv), cwd, log_path))
        proc = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        # FakeProc is a structural stand-in (poll/terminate/kill/wait), not a
        # Popen subclass; the cast is what lets it satisfy `SpawnFn`.
        return cast(subprocess.Popen[bytes], proc)


def _which(name: str) -> str:
    return f"/usr/local/bin/{name}"


def _patch_killpg(monkeypatch: pytest.MonkeyPatch, *procs: FakeProc) -> None:
    """`_shutdown_name` now calls `os.killpg`, not `proc.terminate/kill`
    directly (CBN-EPIC-243.2). `FakeProc` has no real process group, so tests
    that drive it through `shutdown()` stand in for the OS call and translate
    signal → the same fake method the old assertions check."""
    by_pid = {p.pid: p for p in procs}

    def fake_killpg(pgid: int, sig: int) -> None:
        proc = by_pid.get(pgid)
        if proc is None:
            raise ProcessLookupError(pgid)
        (proc.terminate if sig == signal.SIGTERM else proc.kill)()

    monkeypatch.setattr(local_module.os, "killpg", fake_killpg)


def _worker(spawner: Spawner | None = None) -> LocalWorker:
    return LocalWorker(spawn=spawner or Spawner(), which=_which)


def _session(
    worker: LocalWorker,
    root: Path,
    provider: str,
    model: str | None = None,
    effort: str | None = None,
) -> Session:
    return worker.launch(
        root,
        provider=provider,
        model=model,
        effort=effort,
        session_name="app-CBN-001",
    )


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (
            WorkerProvider.GROK,
            ["grok", "--always-approve", "-m", "grok-4.6", "-p", "GO"],
        ),
        (
            WorkerProvider.CLAUDE,
            [
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "bypassPermissions",
                "--settings",
                CLAUDE_GUARD_SETTINGS,
                "--model",
                "opus",
                "GO",
            ],
        ),
        (
            WorkerProvider.CODEX,
            ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "-m", "gpt", "GO"],
        ),
        (
            WorkerProvider.CURSOR,
            ["cursor-agent", "-p", "--force", "--trust", "--model", "composer-2.5", "GO"],
        ),
    ],
)
def test_build_argv_matches_the_verified_recipe(
    provider: str,
    expected: list[str],
) -> None:
    model = expected[expected.index("-m") + 1] if "-m" in expected else expected[-2]
    assert build_argv(expected[0], provider, model, "GO") == expected


def test_claude_guard_registers_the_hook_on_bash_and_file_tools() -> None:
    """The `--settings` payload is a PreToolUse command hook matched on `Bash`
    (CBN-212) and the file-writing tools (CBN-247), whose command is this
    interpreter running `caobunga.hooks` — no
    PATH assumption, since the worker inherits the orchestrator's environment.
    Every other recipe carries no guard: their verified CLIs expose no hook surface.
    """
    settings = json.loads(CLAUDE_GUARD_SETTINGS)
    [entry] = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash|Write|Edit|MultiEdit|NotebookEdit"
    [hook] = entry["hooks"]
    assert hook == {"type": "command", "command": FOREGROUND_HOOK_COMMAND}
    assert shlex.split(FOREGROUND_HOOK_COMMAND) == [sys.executable, "-P", "-m", "caobunga.hooks"]
    assert set(settings) == {"hooks"}
    for provider, recipe in RECIPES.items():
        assert bool(recipe.guard_argv) == (provider == WorkerProvider.CLAUDE), provider


def test_hooks_available_is_true_in_this_install() -> None:
    """This checkout ships `hooks.py`; `find_spec` is the same check `-m` uses."""
    assert hooks_available() is True


def test_build_argv_omits_claude_guard_when_hooks_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale snapshot without `hooks.py` must not pass `--settings` (CBN-216).

    The hook command would fail on every Bash PreToolUse and surface as
    `worker-exited` rather than as a missing install. Other providers have
    no guard to drop.
    """
    monkeypatch.setattr(local_module, "hooks_available", lambda: False)
    argv = build_argv("claude", WorkerProvider.CLAUDE, "opus", "GO")
    assert "--settings" not in argv
    assert CLAUDE_GUARD_SETTINGS not in argv
    assert argv == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        "opus",
        "GO",
    ]
    for provider, binary in (
        (WorkerProvider.GROK, "grok"),
        (WorkerProvider.CODEX, "codex"),
        (WorkerProvider.CURSOR, "cursor-agent"),
    ):
        unguarded = build_argv(binary, provider, "m", "GO")
        assert "--settings" not in unguarded, provider


def test_build_argv_omits_the_model_flag_when_unpinned() -> None:
    argv = build_argv("claude", WorkerProvider.CLAUDE, None, "GO")
    assert "--model" not in argv
    assert argv[-1] == "GO"


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (WorkerProvider.GROK, ["--reasoning-effort", "high"]),
        (WorkerProvider.CLAUDE, ["--effort", "high"]),
        (WorkerProvider.CODEX, ["-c", "model_reasoning_effort=high"]),
    ],
)
def test_build_argv_emits_each_providers_verified_effort_syntax(
    provider: str,
    expected: list[str],
) -> None:
    """Verified 2026-08-26 from each CLI's own --help (CBN-EPIC-15.2).

    Codex is why the slot is a tuple of fragments rather than a flag plus a bare
    value: its effort rides inside a `-c key=value` override, not its own flag.
    """
    argv = build_argv("bin", provider, "m", "GO", effort="high")
    start = argv.index(expected[0])
    assert argv[start : start + len(expected)] == expected


def test_build_argv_drops_effort_for_cursor_which_bakes_it_into_the_model_id() -> None:
    """Cursor's empty `effort_argv` is a deliberate no-op, not an unfilled row."""
    with_effort = build_argv(
        "cursor-agent", WorkerProvider.CURSOR, "composer-2.5", "GO", effort="high"
    )
    without = build_argv("cursor-agent", WorkerProvider.CURSOR, "composer-2.5", "GO")
    assert with_effort == without
    assert "high" not in with_effort


@pytest.mark.parametrize("provider", sorted(RECIPES))
def test_omitting_effort_leaves_every_argv_byte_identical(provider: str) -> None:
    """The no-behavior-change guarantee (CBN-EPIC-15.2), asserted per recipe.

    Nothing passes `effort=` until CBN-EPIC-15.3 routes one, so every live argv
    must be exactly what it was before the slot existed.
    """
    assert build_argv("bin", provider, "m", "GO", effort=None) == build_argv(
        "bin", provider, "m", "GO"
    )


def test_no_recipe_leaves_the_effort_placeholder_unsubstituted() -> None:
    """Every non-empty `effort_argv` must actually carry the slot, or it is inert."""
    for provider, recipe in RECIPES.items():
        if not recipe.effort_argv:
            continue
        joined = " ".join(recipe.effort_argv)
        assert EFFORT_SLOT in joined, provider


def _assert_names_the_supported_set(message: str, rejected: str) -> None:
    """The recipe-less error must carry its own remedy (CBN-097).

    `RECIPES` is narrower than the registry allow-list's `V1_PROVIDERS`, so this
    message is where an operator learns which providers the local adapter can run.
    Asserted against the table, not a frozen string, so adding a recipe cannot
    leave the message stale.
    """
    assert rejected in message
    for supported in RECIPES:
        assert supported in message


def test_build_argv_rejects_a_provider_without_a_recipe() -> None:
    with pytest.raises(UserError) as excinfo:
        build_argv("opencode", WorkerProvider.OPENCODE, None, "GO")
    _assert_names_the_supported_set(str(excinfo.value), WorkerProvider.OPENCODE)


def test_every_recipe_provider_has_a_known_binary() -> None:
    assert set(RECIPES).issubset(set(BINARIES))


def test_launch_resolves_the_binary_and_reserves_the_session(tmp_path: Path) -> None:
    worker = _worker()
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    assert session.session_name == "app-CBN-001"
    assert session.terminal_id == session.session_name
    assert session.provider == WorkerProvider.GROK
    assert worker.status(session) == "idle"


def test_launch_refuses_an_uninstalled_provider(tmp_path: Path) -> None:
    worker = LocalWorker(spawn=Spawner(), which=lambda _name: None)
    with pytest.raises(Unlaunchable, match="not installed") as excinfo:
        _session(worker, tmp_path, WorkerProvider.GROK)
    assert excinfo.value.exit_code == 5


def test_launch_refuses_a_missing_provider_or_recipe(tmp_path: Path) -> None:
    worker = _worker()
    with pytest.raises(UserError):
        worker.launch(tmp_path)
    with pytest.raises(UserError) as excinfo:
        _session(worker, tmp_path, WorkerProvider.OPENCODE)
    _assert_names_the_supported_set(str(excinfo.value), WorkerProvider.OPENCODE)
    assert excinfo.value.exit_code == 2  # a registration fact, not the machine (CBN-EPIC-313.5)


def test_launch_refuses_a_missing_cwd(tmp_path: Path) -> None:
    worker = _worker()
    with pytest.raises(UserError):
        worker.launch(tmp_path / "gone", provider=WorkerProvider.GROK)


def test_send_spawns_the_cli_in_the_project_cwd(tmp_path: Path) -> None:
    spawner = Spawner(FakeProc(code=None))
    worker = _worker(spawner)
    session = _session(worker, tmp_path, WorkerProvider.CLAUDE, model="opus")
    worker.send(session, "/ft-task CBN-001 --unattended")
    argv, cwd, _ = spawner.calls[0]
    assert argv[0].endswith("claude")
    assert argv[-1] == "/ft-task CBN-001 --unattended"
    assert "bypassPermissions" in argv
    assert cwd == tmp_path.resolve()
    assert worker.status(session) == "processing"


def test_launch_carries_effort_through_to_the_spawned_argv(tmp_path: Path) -> None:
    """The whole seam in one assertion: launch holds it, send emits it."""
    spawner = Spawner(FakeProc(code=None))
    worker = _worker(spawner)
    session = _session(worker, tmp_path, WorkerProvider.CLAUDE, model="opus", effort="low")
    worker.send(session, "GO")
    argv, _, _ = spawner.calls[0]
    assert argv[argv.index("--effort") + 1] == "low"


def test_launch_without_effort_spawns_the_pre_existing_argv(tmp_path: Path) -> None:
    """Default path is untouched — no effort kwarg, no effort tokens in argv."""
    spawner = Spawner(FakeProc(code=None))
    worker = _worker(spawner)
    session = _session(worker, tmp_path, WorkerProvider.CLAUDE, model="opus")
    worker.send(session, "GO")
    argv, _, _ = spawner.calls[0]
    assert "--effort" not in argv


def test_send_rejects_an_empty_message(tmp_path: Path) -> None:
    worker = _worker()
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    with pytest.raises(UserError):
        worker.send(session, "")


def test_send_refuses_a_second_prompt(tmp_path: Path) -> None:
    worker = _worker(Spawner(FakeProc(code=None)))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "first")
    with pytest.raises(WorkerError):
        worker.send(session, "second")


def test_send_refuses_reuse_after_the_process_exits(tmp_path: Path) -> None:
    worker = _worker(Spawner(FakeProc(code=0)))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "first")
    with pytest.raises(WorkerError) as excinfo:
        worker.send(session, "second")
    assert "one task, one session" in str(excinfo.value)


def test_send_on_an_unknown_session_is_a_worker_error(tmp_path: Path) -> None:  # noqa: ARG001
    worker = _worker()
    ghost = Session(session_name="never-launched", terminal_id="never-launched")
    with pytest.raises(WorkerError):
        worker.send(ghost, "GO")


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, STATUS_EXITED), (1, STATUS_ERROR), (-9, STATUS_ERROR)],
)
def test_status_maps_the_exit_code(tmp_path: Path, code: int, expected: str) -> None:
    worker = _worker(Spawner(FakeProc(code=code)))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    assert worker.status(session) == expected


def test_exit_code_is_none_before_send(tmp_path: Path) -> None:
    worker = _worker()
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    assert worker.exit_code(session) is None


def test_exit_code_is_none_while_running(tmp_path: Path) -> None:
    worker = _worker(Spawner(FakeProc(code=None)))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    assert worker.exit_code(session) is None


def test_exit_code_reports_the_process_return_code(tmp_path: Path) -> None:
    worker = _worker(Spawner(FakeProc(code=17)))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    assert worker.exit_code(session) == 17


def test_shutdown_terminates_a_running_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProc(code=None)
    _patch_killpg(monkeypatch, proc)
    worker = _worker(Spawner(proc))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    worker.shutdown(session)
    assert proc.terminated
    assert not proc.killed


def test_shutdown_kills_a_process_that_ignores_terminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProc(code=None, stubborn=True)
    _patch_killpg(monkeypatch, proc)
    worker = _worker(Spawner(proc))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    worker.shutdown(session)
    assert proc.terminated
    assert proc.killed


def test_shutdown_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(code=0)
    _patch_killpg(monkeypatch, proc)
    worker = _worker(Spawner(proc))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    worker.shutdown(session)
    worker.shutdown(session)


def test_shutdown_signals_the_group_even_after_the_leader_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leader that has already exited (`poll()` returns a code) may still
    leave a lingering grandchild in its process group (CBN-262) — `killpg`
    must still fire rather than short-circuiting on `proc.poll() is None`."""
    proc = FakeProc(code=0)
    _patch_killpg(monkeypatch, proc)
    worker = _worker(Spawner(proc))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    worker.shutdown(session)
    assert proc.terminated


def test_shutdown_suppresses_timeout_on_the_post_sigkill_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lingering grandchild can leave the leader's `wait()` timing out even
    after SIGKILL (e.g. stuck reaping); `shutdown` must not propagate that
    `TimeoutExpired` (CBN-262)."""
    proc = FakeProc(code=None, stubborn=True, wait_forever=True)
    _patch_killpg(monkeypatch, proc)
    worker = _worker(Spawner(proc))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    worker.shutdown(session)
    assert proc.terminated
    assert proc.killed


def test_shutdown_kills_the_group_when_the_grace_wait_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A force-quit ^C mid-grace (CBN-375) still SIGKILLs the group before it
    propagates: the session is already popped, so nothing could reach it later."""
    proc = FakeProc(code=None, stubborn=True)

    def interrupted(timeout: float | None = None) -> int:  # noqa: ARG001
        raise KeyboardInterrupt

    proc.wait = interrupted
    _patch_killpg(monkeypatch, proc)
    worker = _worker(Spawner(proc))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    with pytest.raises(KeyboardInterrupt):
        worker.shutdown(session)
    assert proc.terminated
    assert proc.killed


def test_close_reaps_every_live_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(code=None)
    _patch_killpg(monkeypatch, proc)
    spawner = Spawner(proc)
    with LocalWorker(spawn=spawner, which=_which) as worker:
        session = _session(worker, tmp_path, WorkerProvider.GROK)
        worker.send(session, "GO")
    assert proc.terminated


def test_shutdown_still_signals_the_group_right_after_status_reaped_the_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal completion path polls `status()` to STATUS_EXITED just
    before `shutdown()` runs (`wait.wait_for_close` / `settle_worker` →
    `launch.retire_session` / `_run_gated`'s `finally`) — `killpg` must still
    fire there. An earlier CBN-270 draft gated this on how long ago the
    leader was reaped, but `_run_gated`'s own deterministic-suite run
    (`finish.settle_and_measure` → `run_fleet_suite`, up to ~1020s by
    `suite_verbs`' own timeouts) sits between that poll and `shutdown()` on
    every gated/proceed-on-green completion — the codebase's first-ship
    tiers — so no fixed staleness window can tell that apart from a
    genuinely stale reap. Reverted; this pins the regression that draft
    would have reintroduced (External review)."""
    proc = FakeProc(code=0)
    _patch_killpg(monkeypatch, proc)
    worker = _worker(Spawner(proc))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    worker.status(session)
    worker.shutdown(session)
    assert proc.terminated


@pytest.mark.parametrize("code", [0, None])
def test_shutdown_treats_killpg_eperm_as_group_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int | None
) -> None:
    """macOS answers `killpg` with `EPERM` for a group left with only zombie
    members; that escaped `shutdown` as `orchestrator-error` on a clean close
    (CBN-349). Both the SIGTERM and the post-grace SIGKILL must tolerate it."""
    proc = FakeProc(code=code, wait_forever=True)
    signals: list[int] = []

    def eperm_killpg(pgid: int, sig: int) -> None:  # noqa: ARG001
        signals.append(sig)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(local_module.os, "killpg", eperm_killpg)
    worker = _worker(Spawner(proc))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    worker.shutdown(session)
    expected = [signal.SIGTERM] if code is not None else [signal.SIGTERM, signal.SIGKILL]
    assert signals == expected
    with pytest.raises(WorkerError):
        worker.status(session)


def test_rotating_log_write_after_close_is_a_safe_no_op(tmp_path: Path) -> None:
    """A tee thread still draining a pipe past `_shutdown_name`'s join timeout
    can call `write_line` after `close()` — it must not raise (CBN-270,
    External review)."""
    from caobunga.local import _RotatingLog

    log = _RotatingLog(tmp_path / "worker.log")
    log.write_line("before close")
    log.close()
    log.write_line("after close")  # must not raise
    assert "after close" not in (tmp_path / "worker.log").read_text()


def test_shutdown_closes_the_log_even_while_a_tee_thread_is_still_draining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tee thread still blocked on `readline()` must not keep the log fd
    open forever — `_shutdown_name` closes it regardless, relying on
    `_RotatingLog` to make a late write from that thread a safe no-op rather
    than deferring the close (CBN-270, External review)."""

    class _BlockingStream:
        def __init__(self) -> None:
            self.released = threading.Event()

        def readline(self) -> bytes:
            self.released.wait()
            return b""

        def close(self) -> None:
            pass

    class _HangingProc:
        def __init__(self) -> None:
            self.pid = next(_FAKE_PIDS)
            self.stdout = _BlockingStream()
            self.stderr = _BlockingStream()

        def poll(self) -> int | None:
            return 0

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
            return 0

    def _fake_killpg(pgid: int, sig: int) -> None:  # noqa: ARG001
        return

    def spawn(
        argv: Sequence[str],  # noqa: ARG001
        cwd: Path,  # noqa: ARG001
        log_path: Path | None = None,  # noqa: ARG001
    ) -> subprocess.Popen[bytes]:
        return cast(subprocess.Popen[bytes], proc)

    monkeypatch.setattr(local_module, "TERMINATE_GRACE", 0.05)
    monkeypatch.setattr(local_module.os, "killpg", _fake_killpg)

    proc = _HangingProc()
    worker = LocalWorker(spawn=spawn, which=_which)
    session = worker.launch(
        tmp_path,
        provider=WorkerProvider.GROK,
        session_name="app-CBN-270",
        log_path=tmp_path / "w.log",
    )
    worker.send(session, "GO")
    # `_shutdown_name` pops the session, so grab the `_Live` record now.
    live = worker._sessions[session.session_name]

    worker.shutdown(session)

    assert any(t.is_alive() for t in live.log_threads)
    assert live.log is not None
    assert live.log._closed  # no leaked fd, even with a straggler thread
    live.log.write_line("late")  # must not raise: closed writes are dropped

    proc.stdout.released.set()
    proc.stderr.released.set()
    for thread in live.log_threads:
        thread.join(timeout=2)


def test_shutdown_kills_the_whole_process_group_of_a_real_worker() -> None:
    """`_default_spawn`'s `start_new_session=True` plus `_shutdown_name`'s
    `os.killpg` (CBN-EPIC-243.2) must reach a real worker's grandchildren —
    a timed-out or terminated worker's shells, MCP servers, and test runners
    that today outlive the run record. Mirrors
    `test_suite_verbs.test_default_run_detaches_stdin_and_kills_the_whole_tree_on_timeout`.

    Matched by process-group id, not by a marker string in the command line:
    a marker only `:`-echoed by the *parent* shell would still vanish from
    `pgrep -f` if only the parent were killed, leaving a false-passing test
    that could not tell a group kill from a leaf-only one.
    """
    from caobunga.local import _default_spawn

    holder: dict[str, subprocess.Popen[bytes]] = {}

    def spawn(
        argv: Sequence[str],  # noqa: ARG001
        cwd: Path,
        log_path: Path | None = None,
    ) -> subprocess.Popen[bytes]:
        proc = _default_spawn(["sh", "-c", "sleep 30 & sleep 30 & wait"], cwd, log_path)
        holder["proc"] = proc
        return proc

    worker = LocalWorker(spawn=spawn, which=_which)
    session = worker.launch(Path.cwd(), provider=WorkerProvider.GROK, session_name="app-CBN-243")
    worker.send(session, "GO")
    pgid = holder["proc"].pid  # `start_new_session=True` makes pid == pgid

    def _group_size() -> int:
        argv = ["pgrep", "-g", str(pgid)]
        listed = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
        return len(listed.stdout.split())

    for _ in range(200):
        if _group_size() >= 3:  # the sh plus both sleep grandchildren
            break
        time.sleep(0.01)
    else:
        raise AssertionError("worker's grandchildren never started")

    worker.shutdown(session)

    assert _group_size() == 0, "worker's process group survived shutdown"


def test_relaunching_a_live_session_name_is_refused(tmp_path: Path) -> None:
    worker = _worker(Spawner(FakeProc(code=None)))
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    with pytest.raises(WorkerError):
        _session(worker, tmp_path, WorkerProvider.GROK)


def test_default_spawn_discards_the_transcript() -> None:
    """stdout/stderr must be DEVNULL — transcripts are never read or stored."""
    from caobunga.local import _default_spawn

    proc = _default_spawn(["/bin/sh", "-c", "echo out; echo err >&2"], Path.cwd())
    assert proc.stdout is None
    assert proc.stderr is None
    assert proc.stdin is None
    assert proc.wait(timeout=10) == 0


def test_default_spawn_exports_an_absolute_caobunga_home_to_the_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CBN-251: the worker's hook resolves a relative `$CAOBUNGA_HOME` against its
    own (project) cwd, not the orchestrator's — so the spawn must export the
    already-absolute value rather than let a relative one pass through unchanged."""
    from caobunga.local import _default_spawn
    from caobunga.store import ENV_HOME, default_home

    monkeypatch.setenv(ENV_HOME, "relative-home")
    expected = str(default_home())
    proc = _default_spawn(
        [sys.executable, "-c", f"import os; print(os.environ[{ENV_HOME!r}])"],
        tmp_path,
        tmp_path / "spawn.log",
    )
    assert proc.stdout is not None
    out = proc.stdout.read().decode().strip()
    proc.wait(timeout=10)
    proc.stdout.close()
    assert proc.stderr is not None
    proc.stderr.close()
    assert out == expected
    assert Path(out).is_absolute()


def test_default_spawn_strips_malloc_stack_logging_keys_from_the_child_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CBN-379: a `MallocStackLogging*` key in the orchestrator's environment must not
    reach the worker (or the hooks it spawns); unrelated keys and `CAOBUNGA_HOME` stay."""
    from caobunga.local import _default_spawn
    from caobunga.store import ENV_HOME, default_home

    for key in ("MallocStackLogging", "MallocStackLoggingNoCompact", "MallocStackLoggingDirectory"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("MallocScribble", "1")
    monkeypatch.setenv("CBN_379_KEEP", "kept")
    code = "import json, os; print(json.dumps(dict(os.environ)))"
    proc = _default_spawn([sys.executable, "-c", code], tmp_path, tmp_path / "spawn.log")
    out, _ = proc.communicate(timeout=10)
    child_env = json.loads(out.decode())
    assert not [key for key in child_env if key.startswith("MallocStackLogging")]
    assert child_env["MallocScribble"] == "1"
    assert child_env["CBN_379_KEEP"] == "kept"
    assert child_env[ENV_HOME] == str(default_home())


def test_default_spawn_reports_a_missing_binary(tmp_path: Path) -> None:
    from caobunga.local import _default_spawn

    with pytest.raises(WorkerError):
        _default_spawn([str(tmp_path / "no-such-binary")], tmp_path)


def test_default_spawn_pipes_stdio_when_a_log_path_is_given(tmp_path: Path) -> None:
    """CBN-162: DEVNULL stays the default; a log path switches both streams to PIPE."""
    from caobunga.local import _default_spawn

    proc = _default_spawn(
        ["/bin/sh", "-c", "echo out; echo err >&2"], Path.cwd(), tmp_path / "worker.log"
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    proc.wait(timeout=10)
    proc.stdout.close()
    proc.stderr.close()


def _wait_exited(worker: LocalWorker, session: Session, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if worker.exit_code(session) is not None:
            return
        time.sleep(0.01)
    raise AssertionError("process never exited")


def _wait_last_stderr(worker: LocalWorker, session: Session, *, attempts: int = 200) -> str:
    """Exit alone doesn't prove the tee thread has drained the pipe yet."""
    for _ in range(attempts):
        tail = worker.stderr_tail(session)
        if tail:
            return tail[-1]
        time.sleep(0.01)
    raise AssertionError("no stderr line ever captured")


def _wait_last_stdout(worker: LocalWorker, session: Session, *, attempts: int = 200) -> str:
    """Exit alone doesn't prove the tee thread has drained the pipe yet."""
    for _ in range(attempts):
        tail = worker.stdout_tail(session)
        if tail:
            return tail[-1]
        time.sleep(0.01)
    raise AssertionError("no stdout line ever captured")


def test_send_tees_stdout_and_stderr_to_the_log_path(tmp_path: Path) -> None:
    """CBN-162: a real subprocess's output lands in the log, tagged by stream.

    A custom `spawn=` stands in for `_default_spawn` so the test drives a fixed
    shell command instead of one of `RECIPES`' real argvs — `send`'s teeing is
    what is under test here, not `build_argv`.
    """

    def spawn(
        argv: Sequence[str],  # noqa: ARG001
        cwd: Path,
        log_path: Path | None = None,
    ) -> subprocess.Popen[bytes]:
        stdio = subprocess.PIPE if log_path is not None else subprocess.DEVNULL
        return subprocess.Popen(
            ["/bin/sh", "-c", "echo out; echo err >&2"],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=stdio,
            stderr=stdio,
        )

    worker = LocalWorker(spawn=spawn, which=_which)
    log_path = tmp_path / "logs" / "20260101T000000000Z-CBN-162.log"
    session = worker.launch(
        tmp_path,
        provider=WorkerProvider.GROK,
        session_name="app-CBN-162",
        log_path=log_path,
    )
    worker.send(session, "GO")
    _wait_exited(worker, session)
    assert _wait_last_stderr(worker, session) == "err"
    assert _wait_last_stdout(worker, session) == "out"
    worker.shutdown(session)
    contents = log_path.read_text()
    assert "[stdout] out" in contents
    assert "[stderr] err" in contents


def test_send_keeps_a_bounded_ordered_tail_per_stream(tmp_path: Path) -> None:
    """CBN-282: the last `TAIL_LINES` non-empty lines, oldest first; blanks skipped."""
    from caobunga.local import TAIL_LINES

    script = "; ".join(f"echo e{i} >&2" for i in range(TAIL_LINES + 2)) + "; echo >&2; echo o"

    def spawn(
        argv: Sequence[str],  # noqa: ARG001
        cwd: Path,
        log_path: Path | None = None,  # noqa: ARG001
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(  # noqa: S603
            ["/bin/sh", "-c", script],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    worker = LocalWorker(spawn=spawn, which=_which)
    session = worker.launch(
        tmp_path,
        provider=WorkerProvider.GROK,
        session_name="app-CBN-282",
        log_path=tmp_path / "w.log",
    )
    worker.send(session, "GO")
    _wait_exited(worker, session)
    for _ in range(200):
        last = worker.stderr_tail(session)[-1:]
        if last == (f"e{TAIL_LINES + 1}",) and worker.stdout_tail(session):
            break
        time.sleep(0.01)
    assert worker.stderr_tail(session) == tuple(f"e{i}" for i in range(2, TAIL_LINES + 2))
    assert worker.stdout_tail(session) == ("o",)
    worker.shutdown(session)


class _LaggingStream:
    """A pipe whose last line arrives only after a delay — the tee lags `poll()`."""

    def __init__(self, lines: Sequence[bytes], *, lag: float) -> None:
        self._lines = list(lines)
        self._lag = lag

    def readline(self) -> bytes:
        if not self._lines:
            return b""
        if len(self._lines) == 1:
            time.sleep(self._lag)
        return self._lines.pop(0)


def test_tail_read_right_after_exit_waits_for_the_tee_to_drain(tmp_path: Path) -> None:
    """CBN-282 review: `poll()` reports exit before the tee hits EOF; the read must not race."""
    proc = FakeProc(code=1)
    proc.stdout = _LaggingStream([], lag=0)  # type: ignore[attr-defined]
    proc.stderr = _LaggingStream(  # type: ignore[attr-defined]
        [b"Internal error: {\n", b'"message": "usage balance exhausted"\n', b"}\n"], lag=0.2
    )
    worker = LocalWorker(spawn=Spawner(proc), which=_which)
    session = worker.launch(
        tmp_path,
        provider=WorkerProvider.GROK,
        session_name="app-drain",
        log_path=tmp_path / "w.log",
    )
    worker.send(session, "GO")
    assert worker.stderr_tail(session)[-1] == "}"
    worker.shutdown(session)


def test_send_without_a_log_path_leaves_both_tails_empty(tmp_path: Path) -> None:
    spawner = Spawner(FakeProc(code=0))
    worker = _worker(spawner)
    session = _session(worker, tmp_path, WorkerProvider.GROK)
    worker.send(session, "GO")
    assert worker.stderr_tail(session) == ()
    assert worker.stdout_tail(session) == ()


def test_rotating_log_rotates_once_past_its_cap(tmp_path: Path) -> None:
    from caobunga.local import _RotatingLog

    path = tmp_path / "worker.log"
    log = _RotatingLog(path, max_bytes=10)
    log.write_line("first line")
    log.write_line("second line")
    log.close()
    assert path.with_name("worker.log.1").exists()
    assert "second line" in path.read_text()


def test_only_claude_streams_json_events() -> None:
    """CBN-377: text-mode `claude -p` prints nothing until exit. The other rows'
    stream flags are unverified (codex already streams), so they stay empty —
    and a row that streams names its own parser."""
    for provider, recipe in RECIPES.items():
        streams = provider == WorkerProvider.CLAUDE
        assert bool(recipe.stream_argv) == streams, provider
        assert (recipe.compact is compact_stream_line) == streams, provider


def _event(**fields: object) -> str:
    return json.dumps(fields)


def _assistant(*blocks: dict[str, object], role: str = "assistant") -> str:
    return _event(type=role, message={"role": role, "content": list(blocks)})


def test_compact_logs_one_line_per_tool_call_and_assistant_message() -> None:
    line = _assistant(
        {"type": "thinking", "thinking": "hmm"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi", "description": "x"}},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py", "old_string": "x"}},
        {"type": "tool_use", "name": "TodoWrite", "input": {"todos": []}},
        {"type": "text", "text": "done\nreally"},
    )
    assert compact_stream_line(line) == (
        ["tool: Bash echo hi", "tool: Edit a.py", "tool: TodoWrite", "assistant: done really"],
        [],
    )


def test_compact_drops_successful_tool_results_and_logs_errors() -> None:
    ok = _assistant({"type": "tool_result", "content": "big output"}, role="user")
    bad = _assistant(
        {"type": "tool_result", "is_error": True, "content": [{"type": "text", "text": "boom"}]},
        role="user",
    )
    assert compact_stream_line(ok) == ([], [])
    assert compact_stream_line(bad) == (["tool-error: boom"], [])


def test_compact_tails_only_the_result_text_verbatim_line_by_line() -> None:
    """The lines text mode printed, so the CBN-200/292 stdout pick reads the same tail."""
    line = _event(type="result", subtype="success", is_error=False, result="Closed.\nFollow-up: x")
    assert compact_stream_line(line) == (["result: success"], ["Closed.", "Follow-up: x"])
    quiet = _event(type="result", subtype="success", is_error=False)
    assert compact_stream_line(quiet) == (["result: success"], [])


def test_compact_tails_an_error_result_without_text() -> None:
    """Text mode printed an error line for max-turns; the stdout diagnostic must survive."""
    empty = _event(type="result", subtype="error_max_turns", is_error=True)
    assert compact_stream_line(empty) == ([], ["result: error_max_turns (error)"])


def test_compact_keeps_a_truncated_event_out_of_the_tail() -> None:
    """A kill mid-write cuts an event short; its tool output must not read as a refusal."""
    cut = _assistant(
        {"type": "tool_result", "is_error": True, "content": "usage limit docs"}, role="user"
    )[:40]
    progress, tail = compact_stream_line(cut)
    assert progress == [cut]
    assert tail == []


def test_compact_logs_a_string_content_message() -> None:
    line = _event(type="user", message={"role": "user", "content": "GO"})
    assert compact_stream_line(line) == (["user: GO"], [])


def test_compact_keeps_a_usage_limit_refusal_classifiable() -> None:
    """CBN-282: the re-pick reads the tail per line; the refusal must survive compaction."""
    line = _event(
        type="result", subtype="success", is_error=True, result="You've hit your limit · resets 3pm"
    )
    _, tail = compact_stream_line(line)
    assert any(is_usage_limit_line(k) for k in tail)


def test_compact_never_tails_a_tool_call_that_names_a_usage_limit() -> None:
    """CBN-377 review: a worker grepping for "usage limit" must not read as exhausted."""
    line = _assistant(
        {"type": "tool_use", "name": "Bash", "input": {"command": 'grep -n "usage limit" a.py'}},
        {"type": "text", "text": "I cannot find the session limit handling"},
    )
    progress, tail = compact_stream_line(line)
    assert any(is_usage_limit_line(p) for p in progress)
    assert tail == []


def test_compact_drops_allowed_rate_limit_events_and_logs_the_rest() -> None:
    allowed = _event(type="rate_limit_event", rate_limit_info={"status": "allowed"})
    rejected = _event(
        type="rate_limit_event",
        rate_limit_info={"status": "rejected", "rateLimitType": "five_hour"},
    )
    assert compact_stream_line(allowed) == ([], [])
    assert compact_stream_line(rejected) == (["rate-limit: rejected five_hour"], [])


def test_compact_passes_non_json_lines_through_to_the_tail() -> None:
    """A CLI error printed before streaming starts is plain text."""
    error = "Error: When using --print, --output-format=stream-json requires --verbose"
    assert compact_stream_line(error) == ([], [error])
    assert compact_stream_line("[1, 2]") == ([], ["[1, 2]"])
    assert compact_stream_line("[" * 100_000) == ([], ["[" * 100_000])
    assert compact_stream_line(_event(type="system", subtype="init")) == (["system: init"], [])
    assert compact_stream_line(_event(type="mystery")) == (["event: mystery"], [])


class _GatedStream:
    """Yields its first line at once, then blocks until released — a worker mid-task."""

    def __init__(self, first: bytes, rest: Sequence[bytes]) -> None:
        self._lines = [first, *rest]
        self.release = threading.Event()

    def readline(self) -> bytes:
        if not self._lines:
            return b""
        if len(self._lines) > 1 or self.release.is_set():
            return self._lines.pop(0)
        self.release.wait(timeout=10)
        return self._lines.pop(0)


def test_stream_json_progress_reaches_the_log_while_the_worker_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-377: a worker killed mid-task leaves a trail, not a 0-byte log."""
    tool = _assistant({"type": "tool_use", "name": "Bash", "input": {"command": "just test"}})
    result = _event(type="result", subtype="success", is_error=False, result="Closed CBN-1.")
    proc = FakeProc()
    stdout = _GatedStream(f"{tool}\n".encode(), [f"{result}\n".encode()])
    proc.stdout = stdout  # type: ignore[attr-defined]
    proc.stderr = _LaggingStream([], lag=0)  # type: ignore[attr-defined]
    _patch_killpg(monkeypatch, proc)
    worker = LocalWorker(spawn=Spawner(proc), which=_which)
    log_path = tmp_path / "w.log"
    session = worker.launch(
        tmp_path, provider=WorkerProvider.CLAUDE, session_name="app-live", log_path=log_path
    )
    worker.send(session, "GO")
    for _ in range(200):
        if log_path.read_text():
            break
        time.sleep(0.01)
    assert worker.exit_code(session) is None
    assert log_path.read_text() == "[stdout] tool: Bash just test\n"
    stdout.release.set()
    proc.code = 0
    assert worker.stdout_tail(session) == ("Closed CBN-1.",)
    worker.shutdown(session)
    assert log_path.read_text() == (
        "[stdout] tool: Bash just test\n[stdout] result: success\n[stdout] Closed CBN-1.\n"
    )


def test_a_non_streaming_recipe_tees_stdout_raw(tmp_path: Path) -> None:
    """Compaction is the claude row's alone; a grok line that looks like JSON is kept as-is."""
    proc = FakeProc(code=0)
    proc.stdout = _LaggingStream([b'{"type": "result", "result": "x"}\n'], lag=0)  # type: ignore[attr-defined]
    proc.stderr = _LaggingStream([], lag=0)  # type: ignore[attr-defined]
    worker = LocalWorker(spawn=Spawner(proc), which=_which)
    session = worker.launch(
        tmp_path, provider=WorkerProvider.GROK, session_name="app-raw", log_path=tmp_path / "w.log"
    )
    worker.send(session, "GO")
    assert worker.stdout_tail(session) == ('{"type": "result", "result": "x"}',)
    worker.shutdown(session)
