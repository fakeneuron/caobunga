"""Generic text-shaping helpers shared across the fleet's narrative reads.

`read_text_or_none` is the plain skip-not-fail file read those surfaces share
(CBN-318), so its `(OSError, UnicodeDecodeError)` fallback is written once.
`Store._load_run` keeps its own `except`: it folds a YAML parse failure into
the same fallback.

`clip` has no Flaitron-grammar dependency — it collapses whitespace and caps
length for any excerpt an operator will read (a summary lede, a follow-up
line, a worker's stderr tail). It lived on `flaitron.tasknote`
and, transitively, on `close.py` until CBN-238 split it out: neither module's
concern is text shaping, and the generic helper's presence on both was exactly
the "beside close classification" bundling that task's PLAN line named.
"""

from __future__ import annotations

import re
from pathlib import Path

_SUMMARY_MAX = 500
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def read_text_or_none(path: Path) -> str | None:
    """`path` read as UTF-8; `None` when it cannot be opened or is not UTF-8.

    The skip-not-fail read (CBN-318): every caller treats an unreadable file as
    absent, so a non-UTF-8 byte is no worse than a missing path.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def strip_ansi(text: str) -> str:
    """Drop CSI escape sequences (colour, cursor) from a captured terminal line."""
    return _ANSI.sub("", text)


def clip(text: str) -> str:
    """Collapse whitespace and cap at `_SUMMARY_MAX`, truncating on a word boundary.

    One cap for every narrative read: a proposed follow-up is a PLAN row an
    operator will paste, so it earns the same budget the summary lede gets —
    public so that reuse does not grow a second constant.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= _SUMMARY_MAX:
        return collapsed
    truncated = collapsed[:_SUMMARY_MAX].rsplit(" ", 1)[0]
    return f"{truncated}…"
