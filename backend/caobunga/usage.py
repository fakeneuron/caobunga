"""Best-effort leftover-usage probes for v1 worker CLIs.

Library, not a CLI verb. Query failures degrade to leftover=unknown and never
raise WorkerError. Do not start an interactive TUI or read credential files.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict

WhichFn = Callable[[str], str | None]
RunFn = Callable[[Sequence[str]], str]

DEFAULT_TIMEOUT = 5.0

# Sentinel: "use DUMP_ARGV for this provider" vs an explicit no-dump (None).
_UNSET: object = object()


class WorkerProvider(StrEnum):
    GROK = "grok_cli"
    CLAUDE = "claude_code"
    CODEX = "codex"
    CURSOR = "cursor_cli"
    OPENCODE = "opencode_cli"


class Leftover(StrEnum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"


class ProviderUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    leftover: Leftover = Leftover.UNKNOWN
    remaining: float | None = None
    installed: bool = False


V1_PROVIDERS: tuple[WorkerProvider, ...] = (
    WorkerProvider.GROK,
    WorkerProvider.CLAUDE,
    WorkerProvider.CODEX,
    WorkerProvider.CURSOR,
    WorkerProvider.OPENCODE,
)

# First hit on PATH wins. `cursor` is the IDE; `agent` is Grok on some hosts.
BINARIES: dict[str, tuple[str, ...]] = {
    WorkerProvider.GROK: ("grok",),
    WorkerProvider.CLAUDE: ("claude",),
    WorkerProvider.CODEX: ("codex",),
    WorkerProvider.CURSOR: ("cursor-agent",),
    WorkerProvider.OPENCODE: ("opencode",),
}

# Non-interactive leftover dumps. None = no safe command (today: all workers).
DUMP_ARGV: dict[str, tuple[str, ...] | None] = {
    WorkerProvider.GROK: None,
    WorkerProvider.CLAUDE: None,
    WorkerProvider.CODEX: None,
    WorkerProvider.CURSOR: None,
    WorkerProvider.OPENCODE: None,
}


def probe_all(
    *,
    which: WhichFn | None = None,
    run: RunFn | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[ProviderUsage]:
    """Probe every v1 worker CLI. Order matches V1_PROVIDERS."""
    return [probe(provider, which=which, run=run, timeout=timeout) for provider in V1_PROVIDERS]


def probe(
    provider: str,
    *,
    which: WhichFn | None = None,
    run: RunFn | None = None,
    dump_argv: Sequence[str] | None | object = _UNSET,  # noqa: RUF036
    timeout: float = DEFAULT_TIMEOUT,
) -> ProviderUsage:
    """Report leftover usage for one provider token. Never raises WorkerError."""
    which_fn = which or shutil.which
    installed = first_binary(provider, which_fn) is not None
    if not installed:
        return ProviderUsage(provider=provider, leftover=Leftover.UNKNOWN, installed=False)

    argv: Sequence[str] | None
    if dump_argv is _UNSET:
        argv = DUMP_ARGV.get(provider)
    else:
        argv = cast("Sequence[str] | None", dump_argv)

    if argv is None:
        return ProviderUsage(provider=provider, leftover=Leftover.UNKNOWN, installed=True)

    run_fn = run or (lambda cmd: run_argv(cmd, timeout))
    try:
        stdout = run_fn(argv)
        leftover, remaining = parse_leftover(stdout)
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RuntimeError,
    ):
        leftover, remaining = Leftover.UNKNOWN, None
    return ProviderUsage(
        provider=provider,
        leftover=leftover,
        remaining=remaining,
        installed=True,
    )


def parse_leftover(stdout: str) -> tuple[Leftover, float | None]:
    """Parse a leftover JSON dump. Unknown leftover on any shape we don't own."""
    data = json.loads(stdout.strip())
    if not isinstance(data, dict):
        return Leftover.UNKNOWN, None

    remaining = _as_fraction(data.get("remaining"))
    if remaining is None:
        remaining = _as_fraction(data.get("remaining_percent"), percent=True)
    if remaining is None and "used_percent" in data:
        used = _as_fraction(data["used_percent"], percent=True)
        if used is not None:
            remaining = max(0.0, min(1.0, 1.0 - used))

    leftover = _leftover_token(data.get("leftover"))
    if leftover is None:
        if remaining is None:
            leftover = Leftover.UNKNOWN
        elif remaining <= 0.0:
            leftover = Leftover.EXHAUSTED
        else:
            leftover = Leftover.AVAILABLE
    return leftover, remaining


def _leftover_token(value: object) -> Leftover | None:
    if not isinstance(value, str):
        return None
    token = value.strip().casefold()
    for leftover in Leftover:
        if leftover.value == token:
            return leftover
    return None


def _as_fraction(value: object, *, percent: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if percent or number > 1.0:
        if 0.0 <= number <= 100.0:
            return number / 100.0
        return None
    if 0.0 <= number <= 1.0:
        return number
    return None


def first_binary(provider: str, which: WhichFn) -> str | None:
    """First PATH hit among `provider`'s known binary names, or `None`.

    Public so `pins.py`'s catalog check can share the same installed-detection
    logic as this module's leftover probe, rather than re-walking `BINARIES`.
    """
    for name in BINARIES.get(provider, ()):
        path = which(name)
        if path:
            return path
    return None


def run_argv(argv: Sequence[str], timeout: float) -> str:
    result = subprocess.run(  # noqa: S603
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        msg = f"dump exited {result.returncode}"
        raise RuntimeError(msg)
    return result.stdout
