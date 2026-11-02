"""A local worker's stdout/stderr tee: stream compaction and the rotating log file.

Not a report surface (docs/CONTRACT.md §Non-negotiables, §Deny-list). `local`
pipes each stream through `tee_stream` into a `RotatingLog` when the caller
supplies `log_path` (CBN-162); a recipe row's `compact` (`compact_stream_line`,
CBN-377) decides which lines are a log-only progress trail and which also reach
the adapter's bounded tails.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from pathlib import Path

from caobunga.text import clip, normalize_terminal_line

# Per-file cap before a single rotation (CBN-162). A wedged worker that never
# stops writing costs one rotated backup, never unbounded disk.
LOG_MAX_BYTES = 2 * 1024 * 1024


# The `tool_use` input keys worth naming on a progress line, in preference order —
# enough to say what the tool touched without dumping its payload.
_TOOL_INPUT_KEYS = ("command", "file_path", "path", "pattern", "url", "description")
# Every tool-call progress and heartbeat activity line starts with this (CBN-EPIC-449.3).
TOOL_PREFIX = "tool: "


# The heartbeat's path allow-list (CBN-458): these tools show their path; Bash
# shows its program; every other tool, including one added later, its name only.
_PATH_TOOLS = frozenset({"Read", "Edit", "Write", "NotebookEdit", "Glob"})
_PATH_KEYS = ("file_path", "notebook_path", "path")
# A whole word `NAME=value` with no quote, escape, `$(`, or brace in the value, so
# the value cannot run on into the next word.
_PLAIN_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9_./:@%+,-]*")
_PLAIN_PROGRAM = re.compile(r"[A-Za-z0-9_./-]+")
# Control bytes `normalize_terminal_line` leaves (a backspace, a lone ESC); an
# echoed path must not rewrite the operator's terminal line.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _tool_input(block: dict[str, object]) -> dict[str, object]:
    params = block.get("input")
    return params if isinstance(params, dict) else {}


def _tool_line(block: dict[str, object]) -> str:
    params = _tool_input(block)
    detail = next((params[key] for key in _TOOL_INPUT_KEYS if params.get(key)), "")
    return clip(f"{TOOL_PREFIX}{block.get('name', '?')} {detail}".rstrip())


def _program(command: object) -> str:
    """A Bash command's program name, or `""` (CBN-455).

    A command's arguments are where a secret value sits (`curl -H "Authorization: …"`,
    `KEY=… cmd`). Fail-closed: plain leading `VAR=value` words are skipped, and the
    first other word is kept only if it is a plain name or path; anything else (a
    quoted or `+=` assignment, a subshell) keeps nothing.
    """
    for word in normalize_terminal_line(str(command or "")).split():
        if _PLAIN_ASSIGNMENT.fullmatch(word):
            continue
        return word if _PLAIN_PROGRAM.fullmatch(word) else ""
    return ""


def _activity_line(block: dict[str, object]) -> str:
    """A `tool_use` block as the heartbeat's redacted `tool:` line (CBN-458).

    Built from the block, not the log line: Bash keeps its program name, a
    `_PATH_TOOLS` tool its path, any other tool its name only — never a
    `pattern`, `url`, `command`, or `description` argument. The log keeps those.
    """
    name = str(block.get("name", "?"))
    params = _tool_input(block)
    if name == "Bash":
        detail = _program(params.get("command"))
    elif name in _PATH_TOOLS:
        detail = next((str(params[key]) for key in _PATH_KEYS if params.get(key)), "")
    else:
        detail = ""
    return clip(_CONTROL.sub("", normalize_terminal_line(f"{TOOL_PREFIX}{name} {detail}")))


def compact_stream_line(line: str) -> tuple[list[str], list[str], list[str]]:
    """One claude stream-json event as `(log_only, log_and_tail, activity)` lines (CBN-377).

    `log_only` is the progress trail: one line per tool call or assistant
    message, a `result:` header, a rejected rate limit. `log_and_tail` is what
    text mode printed — the `result` text, verbatim and line by line, or a
    non-JSON line (a CLI error printed before streaming starts) unchanged — so
    the CBN-282 usage-limit match and the CBN-200/292 stdout pick read the same
    tail they always did, never a tool command that happens to say "usage limit".
    Successful tool results, thinking, and allowed rate-limit events drop: a
    progress trail, not a transcript. `activity` is one redacted `tool:` line per
    `tool_use` block (`_activity_line`, CBN-458), what the heartbeat may echo.
    """
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        # An event cut off mid-write by a kill is still tool output, not a CLI error.
        return ([clip(line)], [], []) if line.lstrip().startswith("{") else ([], [line], [])
    if not isinstance(event, dict):
        return [], [line], []
    kind = event.get("type")
    if kind == "result":
        return _result_lines(event)
    if kind == "system":
        return [f"system: {event.get('subtype', '?')}"], [], []
    if kind == "rate_limit_event":
        info = event.get("rate_limit_info")
        info = info if isinstance(info, dict) else {}
        status = info.get("status", "?")
        if status == "allowed":
            return [], [], []
        return [f"rate-limit: {status} {info.get('rateLimitType', '?')}"], [], []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if kind not in ("assistant", "user") or not content:
        return [clip(f"event: {kind}")], [], []
    if not isinstance(content, list):
        return [clip(f"{kind}: {content}")], [], []
    tools = [
        block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    log_only = [kept for block in content if (kept := _block_line(block))]
    return log_only, [], [_activity_line(block) for block in tools]


def _result_lines(event: dict[str, object]) -> tuple[list[str], list[str], list[str]]:
    """The final `result` event: a log-only header, then its text for the tail."""
    head = f"result: {event.get('subtype', '?')}"
    text = event.get("result")
    if event.get("is_error"):
        head += " (error)"
        if not text:
            # Text mode printed an error line here; keep the diagnostic.
            return [], [head], []
    return [head], str(text).splitlines() if text else [], []


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


Compactor = Callable[[str], tuple[list[str], list[str], list[str]]]


def _write_if_available(log: RotatingLog, entry: str, *, failed: bool) -> bool:
    """Write one diagnostic entry, permanently disabling a failed tee."""
    if failed:
        return True
    try:
        log.write_line(entry)
    except OSError:
        return True
    return False


class RotatingLog:
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
        past `local.LocalWorker._shutdown_name`'s join timeout can call this
        after close instead of racing the closed fd (CBN-270)."""
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


def tee_stream(
    stream: object,
    log: RotatingLog,
    tag: str,
    on_line: Callable[[str], None] | None,
    compact: Compactor | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> None:
    """Read `stream` to EOF, line by line, teeing each into `log`. Runs on its own thread.

    `compact`, when given, splits each raw line into log-only progress lines,
    lines that also reach `on_line`, and redacted activity lines
    (`compact_stream_line`, CBN-377). `on_progress` sees only the activity
    lines, never a log or kept one, so the heartbeat (CBN-EPIC-449.3) echoes no
    tool argument the redaction dropped (CBN-458) and cannot reach the stdout tail.
    """
    readline = getattr(stream, "readline", None)
    if readline is None:
        return
    log_failed = False
    try:
        for raw in iter(readline, b""):
            line = raw.decode(errors="replace").rstrip("\n")
            progress, kept, activity = (
                compact(line) if compact is not None and line.strip() else ([], [line], [])
            )
            for entry in progress:
                log_failed = _write_if_available(log, f"[{tag}] {entry}", failed=log_failed)
            if on_progress is not None:
                for entry in activity:
                    on_progress(entry)
            for entry in kept:
                log_failed = _write_if_available(log, f"[{tag}] {entry}", failed=log_failed)
                if on_line is not None and entry.strip():
                    on_line(entry)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            try:
                close()
            except OSError:
                pass
