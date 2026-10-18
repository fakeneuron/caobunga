"""Harness hooks caobunga injects into a headless worker (CBN-212).

`guard` is a Claude Code `PreToolUse` command hook. The launcher registers it
through `--settings` on the `claude_code` recipe (`local.CLAUDE_GUARD_SETTINGS`);
Claude Code runs it before every `Bash` / file-writing call with the call's JSON
on stdin, and an exit of 2 denies the call and hands stderr back to the model as
the reason. It composes two denials:

`_foreground_only` denies `Bash` with `run_in_background` set. Every
`worker-exited` of the CBN-199 kind — CBN-167, CBN-196, a fleet repo's `CORE-11.3`,
CBN-210 — was a Sonnet worker backgrounding its suite and ending its turn to
wait for a completion that a one-shot `claude -p` session never delivers.
CBN-199 stated the fact in the prompt (`prompt.FOREGROUND_ONLY`); CBN-210 broke
it the next day. This makes the same sentence a denial rather than advice.

`_registry_guard` (CBN-247) denies a `Bash` command invoking `caobunga register`
and a Write / Edit / MultiEdit / NotebookEdit under the store home: a worker
must not retier or re-enable a project. A tripwire against a drifting worker,
not a sandbox — a shell write into the home through `Bash` is not screened.

Never raises. A payload it cannot read is not a denied call, so it exits 0 — a
bug here must not wedge a worker on every tool call.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# The instruction the prompt already carries (`prompt.FOREGROUND_ONLY`), restated
# as the denial reason so the model meets one rule at both points. Kept as a
# literal rather than imported: this module is a `python -m` entry on the
# worker's hot path and must not pull `loop`'s click / rich / fastapi graph.
DENIAL = (
    "blocked by caobunga: this session is headless and one-shot — run the "
    "command in the foreground and wait for it to finish; a background task's "
    "completion never re-invokes you, and ending your turn ends the session."
)

REGISTRY_DENIAL = (
    "blocked by caobunga: the project registry and run records are the "
    "operator's — do not run `caobunga register` or write under the caobunga "
    "home; name the change you need in your final report instead."
)

# `caobunga register` in command position: at the start or after a shell
# separator, past any `VAR=x` assignments and a launcher (`uv run …`,
# `python -m`), optionally path-qualified. Quoted text (`grep "caobunga
# register"`, a commit message) and path segments (`backend/caobunga/cli.py`)
# are not command position, so they pass.
_REGISTER = re.compile(
    r"(?:^|[;&|(`\n])\s*(?:\w+=\S*\s+)*"
    r"(?:(?:uv\s+run|uvx|python3?\s+-m|exec|env|command)\s+(?:\S+\s+)*?)?"
    r"(?:\S*/)?caobunga\s+(?:-\S+\s+)*register(?![\w-])"
)

# Tool name → the `tool_input` key holding the path it writes.
_FILE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def home() -> Path:
    """`store.default_home()`, restated: `store` pulls yaml / pydantic onto the hot path.

    Absolute always (CBN-251): a relative `$CAOBUNGA_HOME` would otherwise
    resolve against this hook's own (worker) cwd, which `local._default_spawn`
    does not share with the orchestrator process `store.default_home()` runs
    in — the pinned equivalence below is what keeps the two in lockstep.
    """
    raw = os.environ.get("CAOBUNGA_HOME")
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else Path.cwd() / path
    return Path.home() / ".caobunga"


def _read(stdin: Any) -> dict[str, Any] | None:
    try:
        payload = json.load(stdin)
    except (ValueError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input")
    return tool_input if isinstance(tool_input, dict) else {}


def _foreground_only(payload: dict[str, Any], stderr: Any) -> int:
    if payload.get("tool_name") != "Bash" or not _tool_input(payload).get("run_in_background"):
        return 0
    print(DENIAL, file=stderr)
    return 2


def _registry_guard(payload: dict[str, Any], stderr: Any) -> int:
    tool = payload.get("tool_name")
    tool_input = _tool_input(payload)
    if tool == "Bash":
        command = tool_input.get("command")
        denied = isinstance(command, str) and _REGISTER.search(command) is not None
    elif tool in _FILE_TOOLS:
        denied = _under_home(tool_input.get(_FILE_TOOLS[tool]), payload.get("cwd"))
    else:
        denied = False
    if not denied:
        return 0
    print(REGISTRY_DENIAL, file=stderr)
    return 2


def _under_home(raw: Any, cwd: Any) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute() and isinstance(cwd, str):
            path = Path(cwd) / path
        # Case-folded: macOS's default volume is case-insensitive, and
        # `realpath` keeps the caller's spelling (`~/.CAOBUNGA` is the home).
        target = Path(os.path.realpath(path).casefold())
        root = Path(os.path.realpath(home()).casefold())
    except (OSError, ValueError, RuntimeError):
        return False
    return target.is_relative_to(root)


def guard(stdin: Any = None, stderr: Any = None) -> int:
    """Exit code for one PreToolUse payload: 2 to deny, 0 to allow."""
    stdin = sys.stdin if stdin is None else stdin
    stderr = sys.stderr if stderr is None else stderr
    payload = _read(stdin)
    if payload is None:
        return 0
    return _foreground_only(payload, stderr) or _registry_guard(payload, stderr)


if __name__ == "__main__":
    sys.exit(guard())
