"""Provider-neutral worker seam: what the run loop needs from one session.

Library, not a CLI verb. `local.LocalWorker` (a headless provider CLI subprocess)
is the shipped implementation; the `cao` HTTP adapter was retired in CBN-204
because it could not carry effort. Declares only the ops `loop.execute_run`
calls — an adapter-side convenience stays off the Protocol.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from caobunga.errors import UserError

ENV_WORKER = "CAOBUNGA_WORKER"
WORKER_LOCAL = "local"
DEFAULT_WORKER = WORKER_LOCAL

# Statuses the run loop reacts to. Adapters may report others (idle, processing);
# only these two end the close wait. `exited` is a one-shot CLI finishing cleanly
# without closing the task — a persistent session (the retired `cao` adapter)
# could never report it.
STATUS_ERROR = "error"
STATUS_EXITED = "exited"

# Actively-working statuses. The run loop waits for a worker to leave this set
# before it touches the target repo's git index (CBN-EPIC-9.5 F1); an adapter
# reaches a quiescent state from here — LocalWorker exits.
STATUS_PROCESSING = "processing"
BUSY_STATUSES = frozenset({STATUS_PROCESSING})

# tmux-safe names (inherited from the CAO era): alphanumeric/underscore start,
# then also hyphen, max 64.
_TMUX_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


class Session(BaseModel):
    """One worker session. `terminal_id` is the adapter's opaque handle."""

    model_config = ConfigDict(extra="ignore")

    session_name: str
    terminal_id: str
    provider: str | None = None


@runtime_checkable
class Worker(Protocol):
    """One task, one session. Implementations may add keyword args with defaults."""

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
        """Reserve a session rooted at `cwd`. Does not deliver a prompt.

        `effort` is the reasoning-effort rung from `taskmap.resolve_effort`. Declared
        here because `loop.execute_run` passes it (CBN-EPIC-15.3); an adapter with no
        way to carry it accepts and ignores it rather than raising (the retired
        `cao` adapter did — its control plane had no effort parameter).

        `log_path`, when given, is the file this session's stdout/stderr get teed to
        (CBN-162) — diagnostics for an operator, never read back by any report
        surface. An adapter with no raw subprocess to tee accepts and ignores it,
        the same shape `effort` takes.
        """
        ...

    def send(self, session: Session, message: str) -> None:
        """Hand the worker its prompt. Does not wait for completion."""
        ...

    def status(self, session: Session) -> str:
        """Peek at liveness. Never reads or returns worker output."""
        ...

    def exit_code(self, session: Session) -> int | None:
        """Process return code once exited, when the adapter has one.

        A number, not output: distinct from the `status()` transcript ban. `None`
        when the adapter has no such concept (a session that is not a raw
        subprocess) or the worker hasn't exited yet.
        """
        ...

    def shutdown(self, session: Session) -> None:
        """End the session. Already-gone is success (idempotent)."""
        ...

    def stderr_tail(self, session: Session) -> tuple[str, ...]:
        """Last few non-empty stderr lines, oldest first (CBN-282).

        Still a bounded fact, not a transcript: a refusal printed as multi-line
        JSON ends in a bare `}`, so the usage-limit classifier needs more than the
        last line, and the escalation summary quotes the last informative one
        (CBN-283). Empty when the adapter keeps no stderr.
        """
        ...

    def stdout_tail(self, session: Session) -> tuple[str, ...]:
        """`stderr_tail`'s stdout counterpart. Empty when the adapter keeps no stdout."""
        ...

    def close(self) -> None:
        """Release adapter-owned resources."""
        ...


def safe_session_name(name: str) -> str:
    """Make a caller-supplied name tmux-legal (dots → hyphens)."""
    cleaned = _TMUX_SAFE.sub("-", name.replace(".", "-")).strip("-")
    if not cleaned or not re.match(r"^[A-Za-z0-9_]", cleaned):
        cleaned = f"s-{cleaned}" if cleaned else "session"
    return cleaned[:64]


def resolve_cwd(cwd: Path | str) -> Path:
    """Project root for a session. Missing or non-directory is a user error."""
    raw = Path(cwd).expanduser()
    try:
        resolved = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise UserError(f"path does not exist: {cwd}") from exc
    if not resolved.is_dir():
        raise UserError(f"not a directory: {resolved}")
    return resolved


def worker_name() -> str:
    """Adapter token from CAOBUNGA_WORKER. Unknown value is a user error."""
    raw = os.environ.get(ENV_WORKER)
    if not raw or not raw.strip():
        return DEFAULT_WORKER
    name = raw.strip().casefold()
    if name != WORKER_LOCAL:
        raise UserError(f"invalid {ENV_WORKER}={raw!r}: expected {WORKER_LOCAL!r}")
    return name


def select_worker() -> Worker:
    """Build the adapter CAOBUNGA_WORKER names. Imports late — the adapter imports us."""
    worker_name()  # validates the token; `local` is the only value it accepts
    from caobunga.local import LocalWorker

    return LocalWorker()
