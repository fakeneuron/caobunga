"""The grammar Caobunga reads producers through: PLAN task lines and tasknote frontmatter.

Every regex, marker token, and pure string->value helper over them, in one place.
A leaf module — `re` only, no sibling import — split out at CBN-143.5.

**This file is the per-pin-bump re-verify surface.** `docs/CONTRACT.md` §"Parse
contract" requires `TASK_LINE` to be re-checked against flaitron's `SPEC.md`
§"Task-line format" and its `viz/src/parser.ts` twin on every `.flaitron/core`
bump; isolating the grammar here makes that a one-file read rather than a scan
through the reader and the picker. Widening a pattern is a pin-bump decision
governed by that section, never a refactor one — CBN-127 declined upstream's
before-tokens suggestion-glyph slot; CBN-167 reversed that decline once a real
fleet row (flaitron's own `CORE-579`) hit it, so a glyph-then-marker row now
parses on either side of the trailing-token run, exactly as
`viz/src/parser.ts` has done since upstream `CORE-502`.
"""

from __future__ import annotations

import re
from itertools import accumulate, pairwise

# Task-level opt-in for unattended dispatch (CBN-EPIC-17.2). Lowercase, placed
# AFTER `[model]` on the PLAN line — `TASK_LINE` captures the token run in that
# position. Placed before (or without) `[model]` it is captured *as* the model,
# and a leading-`!` shape (`[!unattended]`) matches no slot and drops the whole
# line from the parse: both silent failures, both pinned by tests. Caobunga
# reads the marker now; flaitron formalizes the grammar later (CBN-EPIC-17.4).
UNATTENDED_MARKER = "unattended"
# Task-level declaration that this row will stop mid-run for an operator act that
# is not another task — a scoped cross-repo filing prompt, a physical-access step,
# a credential (CBN-136). Rides the same trailing-token run as `[unattended]` and
# carries the same two placement footguns: written before (or without) `[model]`
# it is captured *as* the model, and `[!handoff]` matches no slot and drops the
# whole line. Flaitron tolerates-and-drops it upstream, so reading it here needs
# no grammar move — the CBN-EPIC-17.2 posture, filed upstream separately.
HANDOFF_MARKER = "handoff"
# Flaitron's exploratory `[model]` rung, which `SPEC/model.md` §"The `xheavy` rung
# is manual-only" reserves to the operator: "an orchestrator or any other automated
# chooser must never assign `[xheavy]` to a task nor pick up an `[xheavy]`-tagged
# one". Caobunga is that orchestrator, so `eligibility` denies it at launch (CBN-114).
# Not a `taskmap` concern — `classify("xheavy")` still answers HEAVY (CBN-112) for
# the attended reads that legitimately need a tier.
XHEAVY_TOKEN = "xheavy"  # noqa: S105
# The frontier band: flaitron's chooser-assignable `[frontier]` rung (v6.1.0) and
# the concrete tokens `SPEC/model.md` bands there (`fable`, `mythos`), plus `astra`,
# which flaitron v8.2.0's caller runner cap names beside them (CBN-448). Flaitron's
# own automated-chooser cap is `[frontier]`; caobunga's is stricter at `[heavy]`
# (operator decision 2026-10-11, CBN-331), so `eligibility` denies these at launch
# beside `[xheavy]`. Ordered: the first one a row carries names the denial. The
# model names past `frontier` are `taskmap.FRONTIER_FAMILIES` (the runtime-id cap).
FRONTIER_TOKENS = ("frontier", "fable", "mythos", "astra")
EXTRA_TOKEN = re.compile(r"\[([a-z][\w.-]*)\]")

TASK_ID_BODY = r"[A-Z]+(?:-EPIC)?-\d+(?:\.(?:\d+[a-z]?|N))*"
# SPEC.md §"Long-description conventions" reserves `Blocked by [[ID]]` in a PLAN
# long description as the canonical don't-start gate — the signal the reference
# viz parser extracts into `Task.blockedBy`. These three mirror
# `viz/src/parser.ts` (`CODE_SPAN` / `WIKILINK_PATTERN` / `BLOCKED_BY_BLOCK`)
# rather than restating the grammar loosely: the repo already pins the task-line
# grammar to a flaitron SPEC version (CBN-EPIC-13.2), and a second, more
# tolerant wikilink grammar here would be a fresh divergence surface. Wikilink
# form only — a bare `Blocked by: CBN-115` does not parse, upstream or here.
CODE_SPAN = re.compile(r"`[^`]*`")
WIKILINK = re.compile(rf"\[\[({TASK_ID_BODY})\]\]")
BLOCKED_BY_BLOCK = re.compile(
    rf"Blocked by\s+(\[\[{TASK_ID_BODY}\]\](?:\s*,\s*\[\[{TASK_ID_BODY}\]\])*)"
)
# The loose mention `BLOCKED_BY_BLOCK` is the strict reading of (CBN-128). A row
# saying "Blocked" that yields no clause above is a blocker this reader cannot
# see — `Blocked on [[ID]]`, `Blocked by <prose>`. Reported beside the denial,
# never parsed into a gate: deciding that a producer's sentence means "blocked"
# is defining their format, which is what widening the grammar above was
# rejected for (§"Fleet boundary").
BLOCKED_MENTION = re.compile(r"\bblocked\b", re.IGNORECASE)
PARENT_EPIC = re.compile(r"^[A-Z]+-EPIC-\d+$")
SUGGESTED_NEXT = re.compile(
    rf"^\*\*Suggested next task:\*\*\s+\*\*({TASK_ID_BODY})\*\*",
    re.MULTILINE,
)
HEADING_LINE = re.compile(r"^##\s+(.+?)\s*$")
FENCE_DELIMITER = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")
# CommonMark's code span: a backtick run closed by a run of the same length. The
# comment walk needs this, not `CODE_SPAN`, so a ``` run with no closer stays
# literal text instead of pairing with a later single backtick (CBN-409). An
# escaped backtick opens no span; inside one, a backslash escapes nothing.
RUN_CODE_SPAN = re.compile(r"(?<![`\\])(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)")
FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
# SPEC.md §"Tasknote frontmatter" separates a `park-reason:` code from its
# prose with ` — `. The dash class matches TASK_LINE's own tolerance above,
# so a hand-typed hyphen still yields a code rather than a sentence.
PARK_SEPARATOR = re.compile(r"\s+[—–-]\s+")  # noqa: RUF001


def split_park_reason(value: str | None) -> tuple[str | None, str | None]:
    """Split a `park-reason:` value into `(code, prose)`.

    Flaitron writes `<code> — <prose>`, and `SPEC.md` §"Tasknote frontmatter"
    is explicit that a caller splits on the first separator to read the code and
    never parses the prose. The code stays an opaque string here: flaitron owns
    that closed set, and importing it into a caobunga enum would make a flaitron
    change a caobunga contract change (CBN-EPIC-13.2 fleet boundary).

    A value with no separator is a bare code. A blank value is no reason at all.
    """
    if value is None:
        return None, None
    parts = PARK_SEPARATOR.split(value.strip(), maxsplit=1)
    code = parts[0].strip() or None
    prose = parts[1].strip() or None if len(parts) > 1 else None
    return code, prose


# SPEC §Task-line format + viz tolerances (status glyph, stacked [model],
# suggestion glyph on either side of the trailing-token run (CBN-167,
# mirroring upstream CORE-502), [!critical] before or after [model],
# indented children).
TASK_LINE = re.compile(
    r"^\s*-\s+\[([ xX])\]\s+"
    r"(?:(?:🟢|⏸|✅|⚪|🌱)\uFE0F?\s+)?"
    rf"\*\*({TASK_ID_BODY})\*\*"
    r"(?:\s+\[(!critical)\])?"
    r"(?:\s+\[([a-z][\w.-]*)\])?"
    r"(?:\s*(?:🧠|🔧|🧩|💎|🔭)\uFE0F?)?"
    r"((?:\s+\[[a-z][\w.-]*\])*)"
    r"(?:\s*(?:🧠|🔧|🧩|💎|🔭)\uFE0F?)?"
    r"(?:\s+\[(!critical)\])?"
    r"(?:\s+\|\s+([^\n]+?))?"
    r"(?:\s+[—-]\s+([^\n]+?))?"
    r"\s*$"
)

# The cheap "meant to be a task row" probe for lines `TASK_LINE` rejects
# (CBN-115): the checkbox prefix alone. Anything looser (bare `- ` bullets)
# would flag ordinary prose bullets as broken tasks.
CHECKBOX_ROW = re.compile(r"^\s*-\s+\[([ xX])\]\s")


def epic_stem(task_id: str) -> str | None:
    """Parent stem for epic children. Standalone ids and parent epic rows return None.

    Both child forms Flaitron accepts are handled: `CBN-009.1` → `CBN-009` (the
    `SPEC/epic.md` filing shape) and `CBN-EPIC-9.1` → `CBN-EPIC-9` (the shape some
    adopters, including this repo, actually use). The second used to return None,
    which silently dropped those children from their cohort (CBN-EPIC-9.5 F6).
    """
    if PARENT_EPIC.fullmatch(task_id) or "." not in task_id:
        return None
    area, sep, rest = task_id.partition("-")
    if not sep or "." not in rest:
        return None
    head = rest.split(".", 1)[0]
    number = head[len("EPIC-") :] if head.startswith("EPIC-") else head
    if not number.isdigit():
        return None
    return f"{area}-{head}"


def epic_parent(task_id: str) -> str | None:
    """PLAN parent-row id for an epic child. Non-children return None.

    Both child forms fold onto the one `AREA-EPIC-N` row that actually appears in
    PLAN: `DOC-20.1` and `DOC-EPIC-20.1` alike → `DOC-EPIC-20`. Cohort keying goes
    through here so every cohort reader derives the same key (CBN-EPIC-9.5 F6).
    """
    stem = epic_stem(task_id)
    if stem is None:
        return None
    area, _, head = stem.partition("-")
    return stem if head.startswith("EPIC-") else f"{area}-EPIC-{head}"


def parse_blocked_by(description: str) -> list[str]:
    """Blocker ids cited by `Blocked by [[ID]]` clauses in a PLAN long description.

    A port of `extractBlockedBy` in `viz/src/parser.ts`, tolerance for tolerance:
    code spans are stripped first (so a documentation example in backticks stays
    literal text), only wikilinks inside a `Blocked by` block count, a single
    clause may carry a comma-separated list, and ids are deduped in source order.

    Deliberately *not* extended to tasknote YAML `blocked-by:`. `SPEC/blocked.md`
    is explicit that the YAML key is "a *planning* claim ..., not a don't-start
    gate and not a park" — reading it as a gate here would enforce something
    flaitron defines as non-gating (CBN-116).
    """
    cleaned = CODE_SPAN.sub("", description)
    ids: dict[str, None] = {}
    for block in BLOCKED_BY_BLOCK.finditer(cleaned):
        for match in WIKILINK.finditer(block.group(1)):
            ids[match.group(1)] = None
    return list(ids)


def _blank_code_spans(text: str) -> str:
    """Code spans blanked in place, preserving every offset.

    `parse_blocked_by` deletes them, which is right for extraction — it only
    reads the surviving text. A caller that reports *where* a mention sits needs
    offsets that still index the original, so this blanks instead. Same idiom as
    `_masked_lines` over the PLAN body, for the same reason.
    """
    return CODE_SPAN.sub(lambda match: " " * len(match.group(0)), text)


# Every break `str.splitlines` splits on. Blanking keeps them, so the blanked
# text has the same lines as the original and the walk's mask indexes both.
_NOT_LINE_BREAK = re.compile("[^\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")


def _masked_lines(markdown: str) -> tuple[list[str], list[bool]]:
    """The PLAN's lines with comments blanked, and the matching fence mask.

    One walk decides both (CBN-408), so the reader never re-derives fence state
    from text that the comment scan saw differently.
    """
    spans, fenced = _comment_walk(markdown)
    return _masked_text(markdown, spans).splitlines(), fenced


def _masked_text(markdown: str, spans: list[tuple[int, int]]) -> str:
    pieces: list[str] = []
    last = 0
    for start, end in spans:
        pieces.append(markdown[last:start])
        pieces.append(_NOT_LINE_BREAK.sub(" ", markdown[start:end]))
        last = end
    pieces.append(markdown[last:])
    return "".join(pieces)


def _comment_walk(markdown: str) -> tuple[list[tuple[int, int]], list[bool]]:
    """`(start, end)` offsets of each HTML comment, and the per-line fence mask.

    Comments and fences are read in one pass, CommonMark's way: whichever opens
    first holds until it closes. A ``` line inside a comment opens no fence
    (CBN-402), and a `<!--` on any fenced line — opener, content, or closer — is
    literal text, so an unclosed one in a fenced example cannot swallow the next
    live comment (CBN-408). So is a `<!--` inside an inline code span (CBN-409).
    A line that begins inside a comment is never a fence delimiter. A comment's
    `-->` closes it wherever it falls.
    """
    spans: list[tuple[int, int]] = []
    fenced: list[bool] = []
    open_fence: str | None = None
    covered = 0  # end offset of the last comment
    closable = True  # False once an opener found no `-->`: none after it can close
    bounds = pairwise(
        accumulate((len(line) for line in markdown.splitlines(keepends=True)), initial=0)
    )
    for line, (line_start, line_end) in zip(markdown.splitlines(), bounds, strict=True):
        if covered > line_start:
            fenced.append(False)  # begins inside a comment: never a delimiter
        else:
            in_fence, open_fence = _fence_step(open_fence, line)
            fenced.append(in_fence)
            if in_fence:
                continue
        cursor = max(covered, line_start)
        while closable and (opener := markdown.find("<!--", cursor, line_end)) != -1:
            # Spans are read from `cursor`, so a backtick inside an earlier
            # comment on this line cannot pair with one after it.
            span = next(
                (m for m in RUN_CODE_SPAN.finditer(markdown, cursor, line_end) if m.end() > opener),
                None,
            )
            if span is not None and span.start() < opener:
                cursor = span.end()
                continue
            match = HTML_COMMENT.match(markdown, opener)
            if match is None:
                closable = False
                break
            spans.append(match.span())
            covered = cursor = match.end()
    return spans, fenced


def fence_mask(lines: list[str]) -> list[bool]:
    """Per-line "inside a fenced block" mask, delimiter lines included.

    Public because `tasknote.py` reads tasknote *bodies* with the same fence
    rules this module applies to PLAN.md (CBN-121; those readers moved there
    from `close.py` at CBN-237) — the alternative was a second fence-state walk
    that could disagree with this one about where a block ends.
    """
    mask: list[bool] = []
    open_fence: str | None = None
    for line in lines:
        in_fence, open_fence = _fence_step(open_fence, line)
        mask.append(in_fence)
    return mask


def _fence_step(open_fence: str | None, line: str) -> tuple[bool, str | None]:
    """One line of `fence_mask`'s walk: (line is fenced, fence open after it)."""
    match = FENCE_DELIMITER.match(line)
    if open_fence is None:
        if match is not None and not (match.group(1)[0] == "`" and "`" in match.group(2)):
            return True, match.group(1)
        return False, None
    closes = (
        match is not None
        and match.group(1)[0] == open_fence[0]
        and len(match.group(1)) >= len(open_fence)
        and match.group(2).strip() == ""
    )
    return True, None if closes else open_fence


def unfenced_comments(markdown: str) -> list[str]:
    """Inner text of each HTML comment that opens on an unfenced PLAN line.

    The same spans the reader's PLAN walk blanks (CBN-115, CBN-408), so the two
    agree on what is a comment. Unlike that walk it does not skip text above the
    first priority heading. The epic rollup reads its range declarations from
    these (CBN-402).
    """
    return [markdown[start + 4 : end - 3] for start, end in _comment_walk(markdown)[0]]
