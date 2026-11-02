"""TTY-only emoji + color for the `<word>:` lines `caobunga run` echoes.

The one styling seam (CBN-EPIC-449.4). `loop.py`, `wait.py`, and the
formatters keep emitting plain `prefix: …` text; `cli.run` wraps its echo
with `run_echo()`, which styles only on an interactive stdout with
`NO_COLOR` unset and `TERM` not `dumb`. Piped output — `caobunga fleet`
children, the `/caobunga` skill, tests — stays byte-identical.

A known prefix gets its emoji and, by meaning, a colored prefix word:
green ok, yellow wait/park, red stop/fail; info lines get the emoji alone.
Mixed prefixes read the verdict right after the prefix (first rule that
matches wins). An unknown prefix passes through untouched.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping
from typing import Literal

import click

Tone = Literal["ok", "wait", "stop", "info"]

TONE_COLOR: dict[Tone, str | None] = {"ok": "green", "wait": "yellow", "stop": "red", "info": None}

_LINE = re.compile(r"^([a-z][a-z-]*):(.*)$", re.DOTALL)

# StopReason values (CONTRACT §"Run loop" drain table): `incomplete` waits on a
# denied row, an `outcome` stop that parked or de-scoped waits; the rest stop.
_DRAIN_RULES: tuple[tuple[str, Tone], ...] = (
    (r"complete\b", "ok"),
    (r"incomplete\b", "wait"),
    (r"outcome — \S+ outcome (blocked|de-scoped|no-tasknote)\b", "wait"),
)
# InvocationStatus values; anything else (refused, interrupted, …) stops.
# `observed` never reaches here: loop.py echoes no `invocation:` on observe.
_INVOCATION_RULES: tuple[tuple[str, Tone], ...] = (
    (r"complete\b", "ok"),
    (r"incomplete\b", "wait"),
    (r"empty\b", "info"),
    (r"running\b", "wait"),
)
# Suite verdicts; the other `suite:` / `baseline:` lines (lock wait, HEAD moved) wait.
_SUITE_RULES: tuple[tuple[str, Tone], ...] = (
    (r"green\b", "ok"),
    (r"(red|timeout|refused)\b", "stop"),
)

# prefix → (emoji, default tone, verdict rules). Rules match the text after `prefix: `.
STYLES: dict[str, tuple[str, Tone, tuple[tuple[str, Tone], ...]]] = {
    "run": ("📄", "info", ()),
    "id": ("🔹", "info", ()),
    "tier": ("🔹", "info", ()),
    "enabled": ("🔹", "info", ()),
    "last": ("🔹", "info", ()),
    "next": ("🔹", "info", ()),
    "suite-fitness": ("🧪", "stop", ((r"fit\b", "ok"),)),
    "eligibility": (
        "🔎",
        "info",
        ((r"\S+ allow withdrawn\b", "stop"), (r"\S+ allow\b", "ok"), (r"\S+ deny\b", "wait")),
    ),
    "unparsed": ("❓", "wait", ()),
    "stale-receipt": ("👻", "wait", ((r"(\S+ not rewritten|landed: unreadable)\b", "stop"),)),
    "stale-receipt-record": ("📁", "info", ()),
    "retained": ("🧳", "wait", ((r"unreadable\b", "stop"),)),
    "baseline": ("🧪", "wait", _SUITE_RULES),
    "worktree": ("🌳", "info", ()),
    "bootstrap": ("📦", "stop", ((r"ok\b", "ok"),)),
    "usage-limit": ("🔋", "wait", ((r"re-pick .* cannot launch\b", "stop"),)),
    "launch": ("🚀", "ok", ()),
    "phase": ("📍", "info", ()),
    "progress": ("⏳", "wait", ()),
    "foreign": ("👤", "wait", ()),
    "filed": ("📝", "info", ()),
    "suite": ("🧪", "wait", _SUITE_RULES),
    "landing": (
        "🛬",
        "ok",
        ((r"\S+ not landed\b", "stop"), (r".* not (updated|removed)\b", "wait")),
    ),
    "card": ("📇", "info", ((r"failed\b", "stop"),)),
    "rollup": ("📊", "info", ((r"skipped\b", "wait"),)),
    "park": ("💤", "wait", ()),
    "parked": ("💤", "wait", ()),
    "drain": ("🏁", "stop", _DRAIN_RULES),
    "invocation": ("🧾", "stop", _INVOCATION_RULES),
    "invocation-record": ("📁", "info", ()),
    "recovery": ("🩹", "wait", ()),
}


def tone_of(prefix: str, rest: str) -> Tone | None:
    """The meaning of a `prefix: rest` line, or `None` for an unknown prefix."""
    style = STYLES.get(prefix)
    if style is None:
        return None
    _emoji, default, rules = style
    body = rest.lstrip()
    for pattern, tone in rules:
        if re.match(pattern, body):
            return tone
    return default


def style_line(line: str) -> str:
    """`🚀 launch: …` with the prefix word colored by tone; unknown lines unchanged."""
    match = _LINE.match(line)
    if match is None:
        return line
    prefix, rest = match.groups()
    tone = tone_of(prefix, rest)
    if tone is None:
        return line
    emoji = STYLES[prefix][0]
    color = TONE_COLOR[tone]
    word = f"{prefix}:"
    if color is not None:
        word = click.style(word, fg=color, bold=True)
    return f"{emoji} {word}{rest}"


def should_style(isatty: bool, env: Mapping[str, str]) -> bool:
    """Interactive TTY, `NO_COLOR` unset or empty (no-color.org), and `TERM` not `dumb`."""
    return isatty and not env.get("NO_COLOR") and env.get("TERM") != "dumb"


def run_echo() -> Callable[[str], None]:
    """`click.echo`, styled when stdout is an interactive terminal; plain otherwise."""
    if not should_style(sys.stdout.isatty(), os.environ):
        return click.echo
    return lambda line: click.echo(style_line(line))
