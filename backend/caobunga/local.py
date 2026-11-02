"""Local-subprocess worker: run an already-authenticated provider CLI headless.

Library, not a CLI verb. One task, one process, rooted in the project cwd — the
reference (and only shipped) `worker.Worker` implementation. Close
detection stays git + PLAN based (`close.inspect_close`), so this adapter only
has to launch, be pollable for liveness, and shut down.

Transcripts are disposable in the sense that matters — no report surface reads
one (docs/CONTRACT.md §Non-negotiables, §Deny-list). When the caller supplies
`log_path` (CBN-162), stdout and stderr are teed to that file, capped and
rotated, so a `worker-exited` / `timeout` escalation is diagnosable after the
fact; without one, both streams still go to DEVNULL exactly as before. Either
way nothing here reads the tee back except to pull the bounded tails
`stderr_tail` / `stdout_tail` report — a fact each, not a dump.
"""

from __future__ import annotations

import contextlib
import functools
import os
import shutil
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from caobunga.errors import Unlaunchable, UserError, WorkerError
from caobunga.recipes import RECIPES, build_argv, no_recipe
from caobunga.text import normalize_terminal_line
from caobunga.usage import BINARIES
from caobunga.worker import (
    STATUS_ERROR,
    STATUS_EXITED,
    STATUS_PROCESSING,
    Session,
    resolve_cwd,
    safe_session_name,
)
from caobunga.worker_log import RotatingLog, tee_stream

SpawnFn = Callable[[Sequence[str], Path, Path | None], subprocess.Popen[bytes]]
WhichFn = Callable[[str], str | None]

STATUS_IDLE = "idle"

TERMINATE_GRACE = 5.0

# Non-empty lines kept in memory per stream (CBN-282) — enough to hold a
# multi-line JSON refusal, still a fact rather than a transcript.
TAIL_LINES = 8

# `store.ENV_HOME`, restated so the adapter stays off `store`'s import graph — the
# same restating as `hooks.home()`; the spawn tests pin the two together.
_ENV_HOME = "CAOBUNGA_HOME"


def _capture_tail(tail: deque[str], line: str) -> None:
    """Normalize once at capture; presentation-only lines do not consume the tail."""
    if plain := normalize_terminal_line(line):
        tail.append(plain)


def _record_tool(live: _Live, line: str) -> None:
    """Count a redacted heartbeat `tool:` line and keep it as the latest (CBN-EPIC-449.3).

    Runs on the stdout tee thread; one tuple assignment, so a reader never sees
    a count from one call paired with the line of another. `worker_log` builds
    the line from the `tool_use` block, already redacted (CBN-458); the log
    keeps the full call.
    """
    live.activity = (live.activity[0] + 1, line)


@dataclass
class _Live:
    root: Path
    binary: str
    provider: str
    model: str | None
    effort: str | None = None
    log_path: Path | None = None
    proc: subprocess.Popen[bytes] | None = field(default=None)
    log: RotatingLog | None = field(default=None)
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    stdout_tail: deque[str] = field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    log_threads: tuple[threading.Thread, ...] = field(default=())
    activity: tuple[int, str | None] = (0, None)


class LocalWorker:
    """Spawn one provider-CLI process per task. Tests inject `spawn=` / `which=`.

    `home` is the store home the default spawn exports to the worker as
    `$CAOBUNGA_HOME` (CBN-251), passed in so the adapter never imports `store`.
    """

    def __init__(
        self,
        *,
        home: Path,
        spawn: SpawnFn | None = None,
        which: WhichFn | None = None,
    ) -> None:
        self._spawn = spawn or functools.partial(_default_spawn, home=home)
        self._which = which or shutil.which
        self._sessions: dict[str, _Live] = {}

    def close(self) -> None:
        for name in list(self._sessions):
            self._shutdown_name(name)

    def __enter__(self) -> LocalWorker:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def launch(
        self,
        cwd: Path | str,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_name: str | None = None,
        log_path: Path | None = None,
    ) -> Session:
        """Reserve a session. No process starts until `send` supplies the prompt.

        `effort` is held for `send` and reaches the CLI through the recipe's
        `effort_argv`; it is not validated here, because the accepted values
        differ per vendor. `log_path` is held the same way and decides, at
        `send` time, whether stdout/stderr are teed (CBN-162) or discarded to
        DEVNULL as before.
        """
        root = resolve_cwd(cwd)
        if not provider:
            raise UserError("local worker requires a provider")
        if provider not in RECIPES:
            raise no_recipe(provider)
        binary = self._binary(provider)
        name = safe_session_name(session_name or f"local-{provider}")
        existing = self._sessions.get(name)
        if existing is not None and existing.proc is not None and existing.proc.poll() is None:
            raise WorkerError(f"session {name} is already running")
        self._sessions[name] = _Live(
            root=root,
            binary=binary,
            provider=provider,
            model=model,
            effort=effort,
            log_path=log_path,
        )
        return Session(
            session_name=name,
            terminal_id=name,
            provider=provider,
        )

    def send(self, session: Session, message: str) -> None:
        """Spawn the CLI with `message` as its one-shot prompt. Does not wait."""
        if not message:
            raise UserError("message must not be empty")
        live = self._live(session)
        if live.proc is not None:
            if live.proc.poll() is None:
                raise WorkerError(
                    f"session {session.session_name} is processing; wait before sending"
                )
            raise WorkerError(f"session {session.session_name} already ran; one task, one session")
        argv = build_argv(live.binary, live.provider, live.model, message, effort=live.effort)
        if live.log_path is not None:
            try:
                live.log = RotatingLog(live.log_path)
            except OSError:
                # Diagnostics must not prevent a task from running. Passing no
                # path keeps `_default_spawn` on its long-standing DEVNULL path.
                live.log_path = None
        try:
            live.proc = self._spawn(argv, live.root, live.log_path)
        except BaseException:
            if live.log is not None:
                with contextlib.suppress(OSError):
                    live.log.close()
                live.log = None
            raise
        stdout, stderr = getattr(live.proc, "stdout", None), getattr(live.proc, "stderr", None)
        if live.log is not None and stdout is not None and stderr is not None:
            stdout_thread = threading.Thread(
                target=tee_stream,
                args=(
                    stdout,
                    live.log,
                    "stdout",
                    functools.partial(_capture_tail, live.stdout_tail),
                    RECIPES[live.provider].compact,
                    functools.partial(_record_tool, live),
                ),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=tee_stream,
                args=(
                    stderr,
                    live.log,
                    "stderr",
                    functools.partial(_capture_tail, live.stderr_tail),
                ),
                daemon=True,
            )
            live.log_threads = (stdout_thread, stderr_thread)
            stdout_thread.start()
            stderr_thread.start()
        elif live.log is not None:
            with contextlib.suppress(OSError):
                live.log.close()
            live.log = None

    def status(self, session: Session) -> str:
        """Liveness only. Never reads the process's output."""
        live = self._live(session)
        if live.proc is None:
            return STATUS_IDLE
        code = live.proc.poll()
        if code is None:
            return STATUS_PROCESSING
        return STATUS_EXITED if code == 0 else STATUS_ERROR

    def exit_code(self, session: Session) -> int | None:
        """Return code once the process has exited. `None` before `send` or while running."""
        live = self._live(session)
        if live.proc is None:
            return None
        return live.proc.poll()

    def stderr_tail(self, session: Session) -> tuple[str, ...]:
        """Last `TAIL_LINES` non-empty stderr lines, oldest first. Empty without logging."""
        return tuple(self._drained(session).stderr_tail)

    def stdout_tail(self, session: Session) -> tuple[str, ...]:
        """Last `TAIL_LINES` non-empty stdout lines, oldest first. Empty without logging."""
        return tuple(self._drained(session).stdout_tail)

    def activity(self, session: Session) -> tuple[int, str | None]:
        """Tool calls so far and the last `tool:` line; `(0, None)` without a compacted log."""
        return self._live(session).activity

    def _drained(self, session: Session) -> _Live:
        """`session`'s live state, its tee threads joined once the process has exited.

        `poll()` reports exit before the tee threads read the pipe to EOF, and the
        loop classifies a refusal before `shutdown` joins them (CBN-282) — so the
        lines a CLI printed just before exiting would otherwise race the read.
        """
        live = self._live(session)
        if live.proc is not None and live.proc.poll() is not None:
            for thread in live.log_threads:
                thread.join(timeout=TERMINATE_GRACE)
        return live

    def shutdown(self, session: Session) -> None:
        """Terminate, then kill, the whole process group. Unknown or already-gone
        session is success."""
        self._shutdown_name(session.session_name)

    def _shutdown_name(self, name: str) -> None:
        live = self._sessions.pop(name, None)
        if live is None:
            return
        proc = live.proc
        if proc is not None:
            # `_default_spawn`'s `start_new_session=True` makes `proc.pid` the
            # process group id too, so `killpg` reaches the whole tree (shells,
            # MCP servers, test runners) a timed-out or terminated worker may
            # have spawned — not just the direct child. Mirrors
            # `spawn.run_detached`'s timeout kill. Signal the group even
            # when the leader has already exited: the pgid stays valid as long
            # as any grandchild in it is still alive. macOS answers `EPERM`, not
            # `ESRCH`, for a group left with only zombie members — gone too
            # (CBN-349).
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=TERMINATE_GRACE)
            except BaseException:
                # A force-quit ^C mid-grace (CBN-375): the session is already
                # popped, so nothing else could reach this group later.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                raise
        for thread in live.log_threads:
            thread.join(timeout=TERMINATE_GRACE)
        # A thread still alive here is still blocked on `readline()` and may
        # still call `log.write_line` after this — safe, since `RotatingLog`
        # drops writes once closed (CBN-270) instead of raising on the fd.
        if live.log is not None:
            live.log.close()

    def _live(self, session: Session) -> _Live:
        live = self._sessions.get(session.session_name)
        if live is None:
            raise WorkerError(f"unknown session {session.session_name!r}")
        return live

    def _binary(self, provider: str) -> str:
        for candidate in BINARIES.get(provider, ()):
            found = self._which(candidate)
            if found:
                return found
        raise Unlaunchable(f"provider {provider!r} is not installed")


def _default_spawn(
    argv: Sequence[str], cwd: Path, log_path: Path | None = None, *, home: Path
) -> subprocess.Popen[bytes]:
    """Detached-stdio spawn. stdin is DEVNULL so a CLI never blocks on a prompt.

    stdout/stderr go to DEVNULL, unchanged, unless `log_path` is given — then
    they're piped so `LocalWorker.send` can tee them to that file (CBN-162).
    `start_new_session=True` puts the child in its own process group so
    `LocalWorker._shutdown_name` can reach its whole tree with `os.killpg`
    (CBN-EPIC-243.2), the same reasoning as `spawn.run_detached`.
    """
    stdio = subprocess.PIPE if log_path is not None else subprocess.DEVNULL
    # `home` is made absolute in this (orchestrator) process; exporting it
    # explicitly, rather than letting a relative $CAOBUNGA_HOME pass through
    # unchanged, keeps the worker's `hooks.home()` (rooted in the project cwd,
    # not this one) resolving to the same path (CBN-251). `MallocStackLogging*`
    # keys are dropped so the worker and every hook it spawns do not inherit
    # malloc's "can't turn off" noise from the orchestrator's shell (CBN-379).
    env = {k: v for k, v in os.environ.items() if not k.startswith("MallocStackLogging")}
    env[_ENV_HOME] = str(home.absolute())
    try:
        return subprocess.Popen(  # noqa: S603
            list(argv),
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdio,
            stderr=stdio,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise WorkerError(f"worker binary not found: {argv[0]}") from exc
    except OSError as exc:
        raise WorkerError(f"worker launch failed: {exc}") from exc
