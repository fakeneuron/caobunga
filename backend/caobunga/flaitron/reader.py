"""Read a Flaitron project's PLAN.md: markdown → `PlanTask` / `UnparsedRow`.

The `_plan_body_lines` walk `parse_plan` and `unparsed_plan_rows` both fold, and
the `seeded_line` suggestion built on top of them. Split out at CBN-143.7;
imports only downward, from `.grammar` and `.dto`.

Tasknote reading is the sibling module `tasknote.py`, not this one: both turn a
producer artifact into a DTO, but they read different artifacts with different
grammars, and `__init__` composes them.
"""

from __future__ import annotations

from collections.abc import Iterator

from caobunga.flaitron.dto import PlanPriority, PlanTask, UnparsedRow
from caobunga.flaitron.grammar import (
    CHECKBOX_ROW,
    EXTRA_TOKEN,
    HANDOFF_MARKER,
    HEADING_LINE,
    SUGGESTED_NEXT,
    TASK_LINE,
    UNATTENDED_MARKER,
    _blank_html_comments,
    fence_mask,
    parse_blocked_by,
)

LAUNCH_BUCKETS: tuple[str, ...] = ("High", "Medium", "Low")


def _plan_body_lines(markdown: str) -> Iterator[tuple[int, str, PlanPriority, bool]]:
    """Unfenced lines under a recognized priority heading.

    Yields `(1-based number, blanked line, priority, legacy-critical)`.

    The one shared walk `parse_plan` and `unparsed_plan_rows` both fold
    (CBN-115) — a single masking pass (HTML comments blanked, fences skipped,
    headings tracked) so the two readers cannot disagree about which lines were
    candidates. `_blank_html_comments` preserves newlines, so the yielded
    numbers index the original markdown.
    """
    lines = _blank_html_comments(markdown).splitlines()
    fenced = fence_mask(lines)
    current: PlanPriority | None = None
    legacy_critical = False
    for number, (line, in_fence) in enumerate(zip(lines, fenced, strict=True), start=1):
        if in_fence:
            continue
        heading = HEADING_LINE.match(line)
        if heading:
            current, legacy_critical = _section(heading.group(1))
            continue
        if current is None:
            continue
        yield number, line, current, legacy_critical


def parse_plan(markdown: str) -> list[PlanTask]:
    originals = markdown.splitlines()
    tasks: list[PlanTask] = []
    for number, line, current, legacy_critical in _plan_body_lines(markdown):
        match = TASK_LINE.match(line)
        if not match:
            continue
        mark, task_id, crit_before, model, extras, crit_after, shortname, description = (
            match.groups()
        )
        markers = EXTRA_TOKEN.findall(extras or "")
        tasks.append(
            PlanTask(
                task_id=task_id,
                completed=mark.lower() == "x",
                priority=current,
                critical=legacy_critical or bool(crit_before or crit_after),
                model=model,
                shortname=shortname.strip() if shortname else None,
                description=(description or "").strip(),
                unattended=UNATTENDED_MARKER in markers,
                handoff=HANDOFF_MARKER in markers,
                extras=markers,
                blocked_by=parse_blocked_by(description or ""),
                # Same pairing `unparsed_plan_rows` uses: match on the blanked
                # line, report the original. `_blank_html_comments` preserves
                # newlines, so `number` indexes `originals` directly (CBN-119.3).
                line=number,
                text=originals[number - 1].rstrip(),
            )
        )
    return tasks


def seeded_line(task: PlanTask) -> str | None:
    """`task`'s PLAN line with `[unattended]` correctly placed, or None if it can't be.

    The report's actionable half (CBN-119.3). Seeding is a manual operator act
    (§"Marker coverage" — read-only in both directions), and this writes nothing;
    it produces the exact text an operator would paste over the row, at
    flaitron's own placement — after `[model]` and any suggestion glyph
    (`[light]🔧 [unattended]`, `SPEC/unattended-candidacy.md` §"Three
    postures"; CBN-343). `TASK_LINE` accepts the glyph on either side (CBN-167,
    mirroring upstream `CORE-502`), but one emitted shape is easier to verify
    and easier for an operator to recognize across rows than two equally-valid
    ones, and upstream's is the one its own filers write.

    - The insertion point is the end of `TASK_LINE`'s trailing-token group. A
      glyph directly after `[model]` fills the grammar's first glyph slot, so
      that point is already past it. A glyph after a stacked-`[model]` run
      (`[sonnet] [light]🔧`) fills the second slot, which no token may follow —
      here or in upstream `parser.ts` — so the marker lands before that glyph,
      the only placement that parses.

    - `**ID** [unattended]` with no `[model]` is the one placement that still
      fails: the marker is captured *as* the model, so the row reports
      `unmarked` forever with no trace of the attempt. A naive insertion would
      manufacture that silent failure, so this returns None instead and leaves
      the operator a hand edit they can see.

    **The result is verified by re-parsing it, never by trusting the insertion.**
    A suggestion is emitted only when the rebuilt line parses back to the same
    id and reads as marked, so it cannot promise a fix the reader would reject.
    That is the same one-predicate discipline `coverage` applies to `eligibility`:
    the grammar decides, and this asks it rather than restating it.
    """
    if task.unattended:
        return None
    match = TASK_LINE.match(task.text)
    if match is None:
        return None
    cut = match.end(5)
    candidate = f"{task.text[:cut]} [{UNATTENDED_MARKER}]{task.text[cut:]}"
    rebuilt = parse_plan(f"## {PlanPriority.HIGH.value}\n{candidate}\n")
    if len(rebuilt) != 1:
        return None
    parsed = rebuilt[0]
    if parsed.task_id != task.task_id or not parsed.unattended:
        return None
    return candidate


def unparsed_plan_rows(markdown: str) -> list[UnparsedRow]:
    """Open-looking launch-bucket rows that fail `TASK_LINE` (CBN-115).

    The rows `parse_plan` silently skips: a checkbox line under `## High` /
    `## Medium` / `## Low` (or legacy `## Critical`) that the grammar rejects —
    e.g. `[xheavy] [!unattended]`, a shape SPEC.md §"Task-line format"
    documents as a footgun no tolerance covers (the glyph-then-marker shape
    this used to name, `[xheavy]🔭 [unattended]`, now parses — CBN-167,
    mirroring upstream `CORE-502`). Checked rows count too (CBN-210): a
    checked-but-malformed row is a landed close that vanished from both
    `inspect_close` (which never sees it, since `parse_plan` drops it) and this
    reporter (which used to skip it outright) — an operator needs the same
    line-number-plus-text handle on it as on an open seeding target. Non-launch
    sections and fenced blocks are excluded by scope, and a checkbox row with
    no `*` at all is a prose checklist item, not a failed task row (CBN-209,
    mirroring the reference parser's `FE-087` gate and the `exclusions`
    fixture's "excluded silently" rows): a genuine id near-miss still carries
    the emphasis it fumbled (`*FX-402*`, `**fx-403**`) and keeps flagging.
    Matching runs on the blanked line (same text `parse_plan` saw); the
    reported `text` is the original line, since the row is the operator's to
    fix verbatim.
    """
    originals = markdown.splitlines()
    rows: list[UnparsedRow] = []
    for number, line, priority, _legacy_critical in _plan_body_lines(markdown):
        if priority.value not in LAUNCH_BUCKETS:
            continue
        box = CHECKBOX_ROW.match(line)
        if box is None or "*" not in line:
            continue
        if TASK_LINE.match(line) is not None:
            continue
        rows.append(UnparsedRow(line=number, text=originals[number - 1].strip()))
    return rows


def _suggested_next(markdown: str) -> str | None:
    match = SUGGESTED_NEXT.search(markdown)
    return match.group(1) if match else None


def _section(heading: str) -> tuple[PlanPriority | None, bool]:
    if heading == "Critical":
        return PlanPriority.HIGH, True
    try:
        return PlanPriority(heading), False
    except ValueError:
        return None, False
