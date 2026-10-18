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
import importlib.util
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from caobunga.errors import Unlaunchable, UserError, WorkerError
from caobunga.store import ENV_HOME, default_home
from caobunga.text import clip
from caobunga.usage import BINARIES, WorkerProvider
from caobunga.worker import (
    STATUS_ERROR,
    STATUS_EXITED,
    STATUS_PROCESSING,
    Session,
    resolve_cwd,
    safe_session_name,
)

SpawnFn = Callable[[Sequence[str], Path, Path | None], subprocess.Popen[bytes]]
WhichFn = Callable[[str], str | None]

STATUS_IDLE = "idle"

TERMINATE_GRACE = 5.0

# Per-file cap before a single rotation (CBN-162). A wedged worker that never
# stops writing costs one rotated backup, never unbounded disk.
LOG_MAX_BYTES = 2 * 1024 * 1024

# Non-empty lines kept in memory per stream (CBN-282) — enough to hold a
# multi-line JSON refusal, still a fact rather than a transcript.
TAIL_LINES = 8


# The `tool_use` input keys worth naming on a progress line, in preference order —
# enough to say what the tool touched without dumping its payload.
_TOOL_INPUT_KEYS = ("command", "file_path", "path", "pattern", "url", "description")


def _tool_line(block: dict[str, object]) -> str:
    params = block.get("input")
    if not isinstance(params, dict):
        params = {}
    detail = next((params[key] for key in _TOOL_INPUT_KEYS if params.get(key)), "")
    return clip(f"tool: {block.get('name', '?')} {detail}".rstrip())


def compact_stream_line(line: str) -> tuple[list[str], list[str]]:
    """One claude stream-json event as `(log_only, log_and_tail)` lines (CBN-377).

    `log_only` is the progress trail: one line per tool call or assistant
    message, a `result:` header, a rejected rate limit. `log_and_tail` is what
    text mode printed — the `result` text, verbatim and line by line, or a
    non-JSON line (a CLI error printed before streaming starts) unchanged — so
    the CBN-282 usage-limit match and the CBN-200/292 stdout pick read the same
    tail they always did, never a tool command that happens to say "usage limit".
    Successful tool results, thinking, and allowed rate-limit events drop: a
    progress trail, not a transcript.
    """
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        # An event cut off mid-write by a kill is still tool output, not a CLI error.
        return ([clip(line)], []) if line.lstrip().startswith("{") else ([], [line])
    if not isinstance(event, dict):
        return [], [line]
    kind = event.get("type")
    if kind == "result":
        return _result_lines(event)
    if kind == "system":
        return [f"system: {event.get('subtype', '?')}"], []
    if kind == "rate_limit_event":
        info = event.get("rate_limit_info")
        info = info if isinstance(info, dict) else {}
        status = info.get("status", "?")
        if status == "allowed":
            return [], []
        return [f"rate-limit: {status} {info.get('rateLimitType', '?')}"], []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if kind not in ("assistant", "user") or not content:
        return [clip(f"event: {kind}")], []
    if not isinstance(content, list):
        return [clip(f"{kind}: {content}")], []
    return [kept for block in content if (kept := _block_line(block))], []


def _result_lines(event: dict[str, object]) -> tuple[list[str], list[str]]:
    """The final `result` event: a log-only header, then its text for the tail."""
    head = f"result: {event.get('subtype', '?')}"
    text = event.get("result")
    if event.get("is_error"):
        head += " (error)"
        if not text:
            # Text mode printed an error line here; keep the diagnostic.
            return [], [head]
    return [head], str(text).splitlines() if text else []


def _block_line(block: object) -> str | None:
    """One `assistant` / `user` content block as a progress line, or `None` to drop it."""
    if not isinstance(block, dict):
        return None
    kind = block.get("type")
    if kind == "text" and block.get("text"):
        return clip(f"assistant: {block['text']}")
    if kind == "tool_use":
        return _tool_line(block)
    if kind == "tool_result" and block.get("is_error"):
        return clip(f"tool-error: {_result_text(block.get('content'))}")
    return None


def _result_text(content: object) -> str:
    """A `tool_result`'s content as text: a string, or its `text` blocks joined."""
    if isinstance(content, list):
        parts = (part.get("text") for part in content if isinstance(part, dict))
        return " ".join(str(text) for text in parts if text)
    return str(content or "")


Compactor = Callable[[str], tuple[list[str], list[str]]]


@dataclass(frozen=True)
class Recipe:
    """How one provider CLI takes a one-shot prompt without a human present."""

    lead: tuple[str, ...] = ()
    # Switches stdout to one JSON event per line as the worker runs (CBN-377),
    # parsed by this row's own `compact` before the tee writes it. Empty for a
    # provider whose default output already streams, or whose flag is unverified.
    stream_argv: tuple[str, ...] = ()
    compact: Compactor | None = None
    approve: tuple[str, ...] = ()
    # Harness-side guards the CLI accepts as argv (CBN-212). Empty for a
    # provider whose verified recipe exposes no hook surface.
    guard_argv: tuple[str, ...] = ()
    model_flag: str | None = None
    prompt_flag: str | None = None
    effort_argv: tuple[str, ...] = ()


# Placeholder substituted into `Recipe.effort_argv` by `build_argv`. Internal
# convention, not a template language: it appears only in `RECIPES` rows below
# and is replaced in exactly one place. Codex needs it embedded in a `key=value`
# token rather than standing alone, which is why the slot is a tuple of fragments
# and not a flag plus a bare value.
EFFORT_SLOT = "{effort}"


# Claude Code `--settings` payload registering `caobunga.hooks.guard` as a
# `PreToolUse` command hook on `Bash` and the file-writing tools. The hook denies
# `run_in_background` with `prompt.FOREGROUND_ONLY`'s instruction as the reason —
# the CBN-199 sentence made mechanical after CBN-210 broke it the day it
# shipped (CBN-212) — and denies `caobunga register` or a file write under the
# store home, so a worker cannot retier or re-enable a project (CBN-247). The
# command is this process's own interpreter (`sys.executable`, `shlex`-quoted:
# Claude Code runs hook commands through a shell) with `-m`, so
# the worker needs nothing on PATH — the orchestrator that built the argv is
# the one whose environment `_default_spawn` inherits, `CAOBUNGA_HOME`
# re-exported absolute (CBN-251). `-P` keeps the repo cwd off `sys.path`, so a
# target repo's own `caobunga/` cannot shadow this install (as `fleet.spawn_run`
# does, CBN-312). Built once at import;
# `build_argv` stays a pure table walk. Verified live 2026-09-20 under
# `-p --permission-mode bypassPermissions` (hooks run in every permission mode;
# only `--bare` skips them, and no recipe carries it).
FOREGROUND_HOOK_COMMAND = f"{shlex.quote(sys.executable)} -P -m caobunga.hooks"
CLAUDE_GUARD_SETTINGS = json.dumps(
    {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                    "hooks": [{"type": "command", "command": FOREGROUND_HOOK_COMMAND}],
                }
            ]
        }
    },
    separators=(",", ":"),
)


# Verified 2026-08-22 from each CLI's own --help (CBN-EPIC-9.2); `effort_argv`
# verified the same way 2026-08-26 (CBN-EPIC-15.2). Auto-approve is deliberate and
# unconditional: an unattended worker that stops to ask has nobody to answer it.
# Bump a row when a CLI renames a flag; do not add a provider here without checking
# its --help first — that applies to the effort slot as much as to the rest of the row.
#
# No row carries its provider's accepted effort *values*: the sets differ per vendor
# (claude has `max`, grok does not), so no one closed enum describes them honestly,
# and choosing which value a task class gets is `taskmap.EFFORT`'s job (CBN-EPIC-15.3).
# This table states syntax only.
RECIPES: dict[str, Recipe] = {
    WorkerProvider.GROK: Recipe(
        approve=("--always-approve",),
        model_flag="-m",
        prompt_flag="-p",
        # `--effort` is an accepted alias; the canonical long form is pinned.
        effort_argv=("--reasoning-effort", EFFORT_SLOT),
    ),
    WorkerProvider.CLAUDE: Recipe(
        lead=("-p",),
        # Text mode prints only the final result, at exit, so a killed worker
        # left a 0-byte log (CBN-377). `-p` refuses stream-json without `--verbose`.
        stream_argv=("--output-format", "stream-json", "--verbose"),
        compact=compact_stream_line,
        approve=("--permission-mode", "bypassPermissions"),
        guard_argv=("--settings", CLAUDE_GUARD_SETTINGS),
        model_flag="--model",
        effort_argv=("--effort", EFFORT_SLOT),
    ),
    WorkerProvider.CODEX: Recipe(
        lead=("exec",),
        approve=("--dangerously-bypass-approvals-and-sandbox",),
        model_flag="-m",
        # Codex has no effort flag; it takes a config override (`-c key=value`).
        effort_argv=("-c", f"model_reasoning_effort={EFFORT_SLOT}"),
    ),
    WorkerProvider.CURSOR: Recipe(
        lead=("-p",),
        approve=("--force", "--trust"),
        model_flag="--model",
        # Empty on purpose: Cursor encodes effort in the model id, so routing it
        # is `taskmap.MODELS`' job and there is nothing for argv to add.
        effort_argv=(),
    ),
}


def hooks_available() -> bool:
    """Whether this interpreter can run ``python -m caobunga.hooks`` (CBN-216).

    CBN-212's ``--settings`` payload registers that command as a PreToolUse
    hook. A snapshot install that predates ``hooks.py`` still builds the
    payload (the recipe is a constant) but the worker then ``worker-exited``
    on the first Bash call. Omit the pair when the module is missing; echo
    the miss on ``status`` ``source:`` so it is visible without a dispatch.
    """
    return importlib.util.find_spec("caobunga.hooks") is not None


def _no_recipe(provider: str) -> UserError:
    """The one message both raise sites use. Names the supported set, not just the miss.

    `RECIPES` is narrower than `usage.V1_PROVIDERS` on purpose, and the registry
    allow-list is validated against the wider set (docs/CONTRACT.md §Registry), so
    this error is where an operator first learns the adapter's real reach.
    """
    supported = ", ".join(sorted(RECIPES))
    return UserError(
        f"no local worker recipe for provider {provider!r}: local supports {supported}"
    )


def build_argv(
    binary: str,
    provider: str,
    model: str | None,
    message: str,
    *,
    effort: str | None = None,
) -> list[str]:
    """Headless one-shot argv for one provider. Unsupported provider is a user error.

    `effort` is keyword-only and defaults to None, so omitting it yields exactly the
    argv this function built before CBN-EPIC-15.2. A provider whose `effort_argv` is
    empty (Cursor, which bakes effort into the model id) drops the value silently —
    the same "this provider does not take one" signal `prompt_flag=None` already uses.
    """
    recipe = RECIPES.get(provider)
    if recipe is None:
        raise _no_recipe(provider)
    argv = [binary, *recipe.lead, *recipe.stream_argv, *recipe.approve]
    if recipe.guard_argv and hooks_available():
        argv.extend(recipe.guard_argv)
    if model and recipe.model_flag:
        argv.extend([recipe.model_flag, model])
    if effort and recipe.effort_argv:
        argv.extend(part.replace(EFFORT_SLOT, effort) for part in recipe.effort_argv)
    if recipe.prompt_flag:
        argv.extend([recipe.prompt_flag, message])
    else:
        argv.append(message)
    return argv


class _RotatingLog:
    """Append-only file with one size-triggered rotation. Not a report surface.

    Real subprocess stdio needs a real fd, so this opens the file itself rather
    than wrapping a Python-level stream a `Popen` could redirect to — the tee
    threads below write into it after reading each line off the pipe.
    """

    def __init__(self, path: Path, *, max_bytes: int = LOG_MAX_BYTES) -> None:
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._fh = path.open("ab", buffering=0)
        self._size = self._fh.tell()
        self._closed = False

    def write_line(self, line: str) -> None:
        """No-op once `close()` has run — a tee thread still draining a pipe
        past `_shutdown_name`'s join timeout can call this after close instead
        of racing the closed fd (CBN-270)."""
        data = f"{line}\n".encode(errors="replace")
        with self._lock:
            if self._closed:
                return
            if self._size and self._size + len(data) > self._max_bytes:
                self._rotate()
            self._fh.write(data)
            self._size += len(data)

    def _rotate(self) -> None:
        self._fh.close()
        backup = self._path.with_name(f"{self._path.name}.1")
        try:
            self._path.replace(backup)
        except OSError:
            pass
        self._fh = self._path.open("ab", buffering=0)
        self._size = 0

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._fh.close()


def _tee_stream(
    stream: object,
    log: _RotatingLog,
    tag: str,
    on_line: Callable[[str], None] | None,
    compact: Compactor | None = None,
) -> None:
    """Read `stream` to EOF, line by line, teeing each into `log`. Runs on its own thread.

    `compact`, when given, splits each raw line into log-only progress lines
    and lines that also reach `on_line` (`compact_stream_line`, CBN-377).
    """
    readline = getattr(stream, "readline", None)
    if readline is None:
        return
    try:
        for raw in iter(readline, b""):
            line = raw.decode(errors="replace").rstrip("\n")
            progress, kept = compact(line) if compact is not None and line.strip() else ([], [line])
            for entry in progress:
                log.write_line(f"[{tag}] {entry}")
            for entry in kept:
                log.write_line(f"[{tag}] {entry}")
                if on_line is not None and entry.strip():
                    on_line(entry)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            try:
                close()
            except OSError:
                pass


@dataclass
class _Live:
    root: Path
    binary: str
    provider: str
    model: str | None
    effort: str | None = None
    log_path: Path | None = None
    proc: subprocess.Popen[bytes] | None = field(default=None)
    log: _RotatingLog | None = field(default=None)
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    stdout_tail: deque[str] = field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    log_threads: tuple[threading.Thread, ...] = field(default=())


class LocalWorker:
    """Spawn one provider-CLI process per task. Tests inject `spawn=` / `which=`."""

    def __init__(
        self,
        *,
        spawn: SpawnFn | None = None,
        which: WhichFn | None = None,
    ) -> None:
        self._spawn = spawn or _default_spawn
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
            raise _no_recipe(provider)
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
        live.proc = self._spawn(argv, live.root, live.log_path)
        stdout, stderr = getattr(live.proc, "stdout", None), getattr(live.proc, "stderr", None)
        if live.log_path is not None and stdout is not None and stderr is not None:
            live.log = _RotatingLog(live.log_path)
            stdout_thread = threading.Thread(
                target=_tee_stream,
                args=(
                    stdout,
                    live.log,
                    "stdout",
                    live.stdout_tail.append,
                    RECIPES[live.provider].compact,
                ),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_tee_stream,
                args=(stderr, live.log, "stderr", live.stderr_tail.append),
                daemon=True,
            )
            live.log_threads = (stdout_thread, stderr_thread)
            stdout_thread.start()
            stderr_thread.start()

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
            # `suite_verbs._default_run`'s timeout kill. Signal the group even
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
        # still call `log.write_line` after this — safe, since `_RotatingLog`
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
    argv: Sequence[str], cwd: Path, log_path: Path | None = None
) -> subprocess.Popen[bytes]:
    """Detached-stdio spawn. stdin is DEVNULL so a CLI never blocks on a prompt.

    stdout/stderr go to DEVNULL, unchanged, unless `log_path` is given — then
    they're piped so `LocalWorker.send` can tee them to that file (CBN-162).
    `start_new_session=True` puts the child in its own process group so
    `LocalWorker._shutdown_name` can reach its whole tree with `os.killpg`
    (CBN-EPIC-243.2), the same reasoning as `suite_verbs._default_run`.
    """
    stdio = subprocess.PIPE if log_path is not None else subprocess.DEVNULL
    # `default_home()` is absolute in this (orchestrator) process; exporting it
    # explicitly, rather than letting a relative $CAOBUNGA_HOME pass through
    # unchanged, keeps the worker's `hooks.home()` (rooted in the project cwd,
    # not this one) resolving to the same path (CBN-251). `MallocStackLogging*`
    # keys are dropped so the worker and every hook it spawns do not inherit
    # malloc's "can't turn off" noise from the orchestrator's shell (CBN-379).
    env = {k: v for k, v in os.environ.items() if not k.startswith("MallocStackLogging")}
    env[ENV_HOME] = str(default_home())
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
