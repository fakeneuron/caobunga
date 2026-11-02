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
_TERMINAL_ESCAPE = re.compile(
    r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"  # OSC, including hyperlinks
    r"|(?P<forward>(?:\x1b\[|\x9b)[0-9]*C)"  # cursor-forward supplies spacing
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"  # other CSI, including colour
    r"|\x1b[ -/]*[0-~]"  # charset selection and other ESC sequences
)


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


def normalize_terminal_line(line: str) -> str:
    """Plain words for one captured worker line; not a terminal screen emulator.

    Drop complete presentation escapes, treating cursor-forward as a word
    separator, then collapse Unicode whitespace. Call after stream compaction,
    before tail capture, so all readers see the same text without altering logs.
    """
    plain = _TERMINAL_ESCAPE.sub(lambda match: " " if match["forward"] else "", line)
    return " ".join(plain.split())


def clip(text: str, *, limit: int = _SUMMARY_MAX) -> str:
    """Collapse whitespace and cap at `limit`, truncating on a word boundary.

    One cap for every narrative read: a proposed follow-up is a PLAN row an
    operator will paste, so it earns the same budget the summary lede gets —
    public so that reuse does not grow a second constant. `limit` is for a
    one-line echo that must stay shorter (the `progress:` heartbeat).
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    truncated = collapsed[:limit].rsplit(" ", 1)[0]
    return f"{truncated}…"
