"""The close wait: poll a worker until its task closes, it dies, or the clock runs out.

`caobunga.wait` owns the "is the worker done, and how did it end" question —
the watchdog-woken close poll (`wait_for_close`), the post-close settle grace
(`settle_worker`), the phase echo the poll rides (`_echo_phase_progress`), and
the exit-detail fragment folded into every `WorkerError` it raises
(`_exit_detail`). A leaf module: `close`, `errors`, the flaitron façade,
`models`, `text`, and `worker` cross in, and nothing calls back into
`loop.py`. Split out of `loop.py` at CBN-EPIC-232.5.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from caobunga.close import CloseSnapshot, inspect_close
from caobunga.errors import GitUnreadable, WorkerError
from caobunga.flaitron import PLAN_REL, TASKNOTE_DIR_REL, phase_progress
from caobunga.models import Escalation
from caobunga.text import clip, read_text_or_none, strip_ansi
from caobunga.worker import (
    BUSY_STATUSES,
    STATUS_ERROR,
    STATUS_EXITED,
    Session,
    Worker,
)

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

DEFAULT_POLL = 1.0
# How long to let a worker finish its own post-closure git work before Caobunga
# touches the same repo (CBN-EPIC-9.5 F1). A grace, not a gate: on expiry the run
# proceeds, so a wedged worker degrades to the old behavior instead of aborting.
SETTLE_GRACE = 30.0


# Closed phrase list for a usage/session-limit refusal (CBN-213). Case-insensitive,
# per-line substring match against the stderr/stdout tails — not just the last line, since
# a refusal printed as multi-line JSON ends in a bare `}` (CBN-282). Generic
# "rate limit" is a temporary 429, not leftover-exhausted, and is deliberately
# absent; a 402 "payment required" is billing, which no retry on that provider fixes.
USAGE_LIMIT_MARKERS = (
    "usage limit",
    "usage-limit",
    "session limit",
    "session-limit",
    "out of extra usage",
    "hit your limit",
    "quota exceeded",
    "quota has been reached",
    "usage balance exhausted",
    "payment required",
)

# Closed phrase list for a cause-bearing stdout line (CBN-292). A worker's final
# message often ends in a `Follow-up:` aside after the line naming why it stopped
# (a fleet repo's TEST-004). "cause" is deliberately absent: that aside said it, and
# `_ASIDE_PREFIX` skips asides but cannot catch every label. So are "error" and
# "failed": a passing run prints "0 errors" / "0 failed". Each marker must start
# a word (CBN-300), so `prefix:` is not `fix:` and `unblocked` is not `blocked`.
STDOUT_CAUSE_MARKERS = (
    "fix:",
    "stopped",
    "blocked",
    "cannot",
    "can't",
    "can\u2019t",  # typographic apostrophe
)

# A trailing aside is commentary on the cause, not the cause (CBN-300): a worker's
# `Follow-up:` line can itself say "can't" or "blocked". Bullet, quote and bold
# markers, a `\u2022` bullet, or a `1.` / `1)` number may precede the label.
_ASIDE_PREFIX = re.compile(
    r"^(?:[\s>*_#\u2022-]|\d+[.)])*(?:follow[- ]?ups?|notes?|next steps?)\b[\s*_]*:", re.I
)


def _has_marker(lowered: str, markers: tuple[str, ...], *, word_start: bool = False) -> bool:
    """True when `lowered` (already lowercased) holds any marker.

    `word_start` requires a non-letter before the match, so `fix:` is not `prefix:`.
    """
    if word_start:
        return any(re.search(rf"(?<![a-z]){re.escape(marker)}", lowered) for marker in markers)
    return any(marker in lowered for marker in markers)


def is_usage_limit_line(line: str) -> bool:
    """True when `line` names a usage/session-limit refusal (`USAGE_LIMIT_MARKERS`)."""
    return _has_marker(line.lower(), USAGE_LIMIT_MARKERS)


def _summary_line(tail: tuple[str, ...], causes: tuple[str, ...] = ()) -> str | None:
    """The tail line worth quoting in an escalation summary, or `None`.

    ANSI is stripped first, so a bare colour reset is not a line. A multi-line
    JSON error ends in a bare `}` (CBN-283, a fleet repo's FE-104) and may trail
    `"type": "billing"` after the message, so the newest informative line is the
    last resort: prefer one that matched a usage-limit marker (the same phrase
    list `launch._is_usage_limit_refusal` keys on), then one matching a `causes`
    marker that is not a trailing aside (CBN-292, CBN-300; stdout passes them,
    stderr none), then a `"message"` line (CBN-285).
    """
    plain = (strip_ansi(line) for line in tail)
    lines = [(line, line.lower()) for line in plain if any(c.isalnum() for c in line)]
    for hit in (
        lambda _ln, low: _has_marker(low, USAGE_LIMIT_MARKERS),
        lambda ln, low: not _ASIDE_PREFIX.match(ln) and _has_marker(low, causes, word_start=True),
        lambda ln, _low: '"message"' in ln,
    ):
        match = next((ln for ln, low in reversed(lines) if hit(ln, low)), None)
        if match:
            return match
    return lines[-1][0] if lines else None


def _exit_detail(client: Worker, session: Session) -> str:
    """Exit code plus the worker's last informative stderr/stdout lines, parenthesized — or `""`.

    Shared by the `worker-error`, `worker-exited`, and `timeout` escalations
    (CBN-162; stdout added CBN-200; worker-error folded CBN-213): those used to
    carry nothing beyond a fixed sentence, so a usage-limit refusal, a rejected
    CLI arg, and a crash were indistinguishable on the run record. On a clean
    exit (0) stderr is usually empty and the model's final message — the real
    diagnostic — is in stdout instead, preferring a cause-bearing line (CBN-292).
    Each line is picked from the adapter's bounded tail (`_summary_line`,
    CBN-283). Neither value is a transcript — one is a number, the other two
    are `clip()`ped like every other summary fragment — so this does not
    reopen the "no field stores a worker transcript" line (CONTRACT.md §Run record).
    """
    parts = []
    code = client.exit_code(session)
    if code is not None:
        parts.append(f"exit code {code}")
    last_stderr = _summary_line(client.stderr_tail(session))
    if last_stderr:
        parts.append(f"stderr: {clip(last_stderr)}")
    last_stdout = _summary_line(client.stdout_tail(session), STDOUT_CAUSE_MARKERS)
    if last_stdout:
        parts.append(f"stdout: {clip(last_stdout)}")
    if not parts:
        return ""
    return " (" + "; ".join(parts) + ")"


def _echo_phase_progress(
    root: Path,
    task_id: str,
    last: str | None,
    echo: Callable[[str], None] | None,
) -> str | None:
    """Echo `phase: …` when the open tasknote has reached a new phase.

    Skip-not-fail (CBN-168's vanished-path posture): a missing or unreadable
    note leaves `last` unchanged. Echo-only — nothing is stored.
    """
    if echo is None:
        return last
    text = read_text_or_none(root / TASKNOTE_DIR_REL / f"{task_id}.md")
    if text is None:
        return last
    token = phase_progress(text)
    if token is None or token == last:
        return last
    echo(f"phase: {token}")
    return token


def _status_verdict(
    client: Worker,
    session: Session,
    task_id: str,
    poll: Callable[[], CloseSnapshot | None],
) -> CloseSnapshot | None:
    """Read the worker's status: raise on ERROR or a closeless exit, else keep waiting.

    Returns a snapshot only when an exited worker's close landed on its last
    action; `None` means the worker is still running and the loop should wait.
    """
    status = client.status(session)
    if status == STATUS_ERROR:
        raise WorkerError(
            f"worker {session.terminal_id} reached ERROR{_exit_detail(client, session)}",
            Escalation.WORKER_ERROR,
        )
    if status == STATUS_EXITED:
        # A one-shot worker can finish without closing. Re-check once — the
        # close may have landed on its last action — then escalate.
        snap = poll()
        if snap is not None:
            return snap
        raise WorkerError(
            f"worker exited without closing {task_id}{_exit_detail(client, session)}",
            Escalation.WORKER_EXITED,
        )
    return None


def wait_for_close(
    client: Worker,
    session: Session,
    root: Path,
    task_id: str,
    start_sha: str,
    *,
    timeout: float,
    poll_interval: float,
    sleep: Callable[[float], None],
    unattended: bool = False,
    drain: bool = False,
    baseline: list[str] | None = None,
    echo: Callable[[str], None] | None = None,
) -> CloseSnapshot:
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    observer = _start_watch(root, wake) if poll_interval > 0 else None
    last_phase: str | None = None

    def poll() -> CloseSnapshot | None:
        # A poll reads a tree the worker is mutating. A path that vanishes
        # between a check and its read — the worker's archive `git mv`, the
        # PLAN's `is_file()` → `read_text` gap — is a transient state, not a
        # verdict, so it answers "not closed yet" and the next poll re-reads
        # (CBN-168). `UserError` is deliberately not caught: a PLAN missing at
        # `is_file()` time is the registration error the contract names. Its
        # one exception is `GitUnreadable` (CBN-269): a `git` read that hung past
        # its timeout mid-run is transient too, and aborting would kill a live
        # worker with no run record; a worker that exits while git stays
        # unreadable still escalates below.
        try:
            return inspect_close(
                root, task_id, start_sha, unattended=unattended, drain=drain, baseline=baseline
            )
        except (OSError, GitUnreadable):
            return None

    try:
        while True:
            snap = poll()
            if snap is not None:
                return snap
            last_phase = _echo_phase_progress(root, task_id, last_phase, echo)
            snap = _status_verdict(client, session, task_id, poll)
            if snap is not None:
                return snap
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError(
                    f"timed out after {int(timeout)}s waiting for {task_id} to close"
                    f"{_exit_detail(client, session)}",
                    Escalation.TIMEOUT,
                )
            wait_for = min(poll_interval, remaining) if poll_interval > 0 else 0.0
            if observer is not None:
                wake.wait(timeout=wait_for)
                wake.clear()
            else:
                sleep(wait_for)
    finally:
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)


def settle_worker(
    client: Worker,
    session: Session,
    *,
    poll_interval: float,
    sleep: Callable[[float], None],
    grace: float = SETTLE_GRACE,
) -> None:
    """Wait for the worker to stop working before Caobunga touches its repo.

    Deliberately a grace, not a gate: expiry returns normally rather than raising,
    so a wedged worker costs one `SETTLE_GRACE` pause instead of adding a new abort
    path. Portable across adapters because it waits to leave `BUSY_STATUSES` — a
    local subprocess exits, a CAO session goes idle, and neither reports the other.
    """
    deadline = time.monotonic() + grace
    step = poll_interval if poll_interval > 0 else DEFAULT_POLL
    while True:
        try:
            status = client.status(session)
        except WorkerError:
            return
        if status not in BUSY_STATUSES:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        sleep(min(step, remaining))


def _start_watch(root: Path, wake: threading.Event) -> BaseObserver | None:
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None

    class _Wake(FileSystemEventHandler):
        def on_any_event(self, event: object) -> None:  # noqa: ARG002
            wake.set()

    observer = Observer()
    handler = _Wake()
    watched = False
    for path in (root / PLAN_REL.parent, root / ".git"):
        if path.is_dir():
            observer.schedule(handler, str(path), recursive=True)
            watched = True
    if not watched:
        return None
    observer.start()
    return observer
