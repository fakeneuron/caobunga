"""Read a Flaitron project's open tasknotes: YAML frontmatter → `OpenTasknote`,
and a tasknote body → its furthest-reached phase (`phase_progress`) or one of its
narrative fragments (Final Summary lede, follow-up items, De-scope verdict).

`OPEN_STATUSES` and the two functions that apply it. Split out at CBN-143.7 as a
sibling of `reader.py` rather than folded into it: the two read different artifacts
with different grammars, and `__init__` composes them. The phase-heading parser
(CBN-214) moved here from `loop.py` at CBN-EPIC-232.4: it reads a flaitron artifact,
which is this package's job; the wait loop keeps the echo that consumes it. The
narrative-fragment readers (CBN-237) moved here from `close.py` for
the same reason: each reads a tasknote body, which belongs on this module's side of
the reader/composer boundary.

**Skip-not-fail is the contract here** (CBN-EPIC-7.4): a tasknote whose frontmatter
is missing or not a mapping drops out of the open set rather than raising. One
malformed note must never cost the whole read. Unparseable YAML degrades one rung
less (CBN-186): the two keys this reader needs are re-read line-wise, so a
`park-reason:` prose that happens to contain `: ` costs nothing — the note stays
parked and the reason still reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml

from caobunga.flaitron.dto import OpenTasknote
from caobunga.flaitron.grammar import FRONTMATTER, fence_mask
from caobunga.flaitron.layout import TASKNOTE_DIR_REL
from caobunga.text import clip, read_text_or_none

OPEN_STATUSES = frozenset({"starter", "not-started", "in-progress", "blocked"})

# Line-wise fallback for the two keys read here, used only when the frontmatter
# fails `yaml.safe_load`. A `: ` inside an unquoted plain scalar is a YAML
# mapping indicator, and flaitron's `--unattended` writer emits `park-reason:`
# prose unquoted — so `visual-confirm — Phase 3 👁️ CONFIRM: header renders`
# breaks the whole block. The park is the fact; the prose must not cost it.
FRONTMATTER_KEY_LINE = re.compile(r"^(status|park-reason):[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def list_open_tasknotes(root: Path) -> list[OpenTasknote]:
    directory = root / TASKNOTE_DIR_REL
    if not directory.is_dir():
        return []
    notes: list[OpenTasknote] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        status, park_reason = _tasknote_frontmatter(path)
        if status not in OPEN_STATUSES:
            continue
        notes.append(
            OpenTasknote(
                task_id=path.stem,
                path=path.relative_to(root).as_posix(),
                status=status,
                park_reason=park_reason,
            )
        )
    return notes


def _tasknote_frontmatter(path: Path) -> tuple[str | None, str | None]:
    """`(status, park-reason)` from a tasknote's YAML frontmatter.

    Skip-not-fail throughout (CBN-EPIC-7.4): a missing or non-mapping
    frontmatter yields `(None, None)` and the note drops out of the open set.
    A non-string `park-reason` costs only the reason, never the candidate — it
    is an annotation on the park, not the park itself.

    Unparseable YAML falls back to `FRONTMATTER_KEY_LINE` (CBN-186): `status:`
    and `park-reason:` are read off their own lines, verbatim, so a value that
    breaks the YAML grammar costs at most itself. Only a block with no
    recoverable `status:` line still drops out.

    The *file* can be missing too, not just its frontmatter (CBN-168): the
    caller globs `tasknote/` and reads each hit, and a closing worker's
    `git mv` into `archive/` lands between the two exactly when the watch
    loop wakes on that event. Same answer as `layout.py`'s probes — the note
    drops out of *this* read and the next poll sees the archive.
    """
    text = read_text_or_none(path)
    if text is None:
        return None, None
    match = FRONTMATTER.match(text)
    if not match:
        return None, None
    try:
        raw = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        found = dict(FRONTMATTER_KEY_LINE.findall(match.group(1)))
        return found.get("status") or None, found.get("park-reason") or None
    if not isinstance(raw, dict):
        return None, None
    status = raw.get("status")
    park_reason = raw.get("park-reason")
    return (
        status if isinstance(status, str) else None,
        park_reason.strip() or None if isinstance(park_reason, str) else None,
    )


# Flaitron phase headings (`tasknote-template.md`). The optional leading token
# is the emoji (`📝`); the name is the remainder after `Phase N:`.
_PHASE_HEADING = re.compile(
    r"^##\s+(?:\S+\s+)?Phase\s+(\d+):\s*(.+?)\s*$",
    re.IGNORECASE,
)
_NOTES_HEADING = re.compile(r"^##\s+(?:\S+\s+)?Notes\s*$")
_SECTION_HEADING = re.compile(r"^##\s+")
_CHECKED_BOX = re.compile(r"^- \[[xX]\]")
_UNCHECKED_BOX = re.compile(r"^- \[ \]")


def _markdown_sections(text: str) -> list[tuple[str, str]]:
    """Split a tasknote body on `##` headings. First pair may have an empty heading.

    A `##` line inside a fenced block is body, not a heading: a quoted
    `## Phase 4:` must not open a phase section.
    """
    sections: list[tuple[str, str]] = []
    heading = ""
    body: list[str] = []
    lines = text.splitlines()
    for line, in_fence in zip(lines, fence_mask(lines), strict=True):
        if not in_fence and _SECTION_HEADING.match(line):
            if heading or body:
                sections.append((heading, "\n".join(body)))
            heading = line
            body = []
        else:
            body.append(line)
    if heading or body:
        sections.append((heading, "\n".join(body)))
    return sections


def _checkbox_states(section: str) -> list[bool]:
    """Checked (`True`) / unchecked (`False`) boxes in a heading's body, in order.

    A box inside a fenced block is a quoted example, not a phase checkbox.
    """
    states: list[bool] = []
    lines = section.splitlines()
    for line, in_fence in zip(lines, fence_mask(lines), strict=True):
        if in_fence:
            continue
        if _CHECKED_BOX.match(line):
            states.append(True)
        elif _UNCHECKED_BOX.match(line):
            states.append(False)
    return states


def _phase_slug(name: str) -> str:
    token = re.split(r"\W+", name.strip(), maxsplit=1)[0].casefold()
    return token or "phase"


def phase_progress(text: str) -> str | None:
    """Furthest-reached Flaitron phase in a tasknote body, as a CLI token.

    `1 discovery` … `4 closure` when `## Phase N:` headings exist. Phase 1 is
    reached by the heading existing; a later phase is reached when it contains
    a checked box or every checkbox of the previous phase is ticked. A
    micro-tasknote (`## ⚡ Notes`, no phase headings) is `notes`. `None` when
    neither shape is present. Pure parse — the wait loop decides whether to
    echo (CBN-214).
    """
    phases: list[tuple[int, str, bool, bool, bool]] = []
    saw_notes = False
    for heading, section in _markdown_sections(text):
        matched = _PHASE_HEADING.match(heading)
        if matched is not None:
            boxes = _checkbox_states(section)
            has_box = bool(boxes)
            phases.append(
                (
                    int(matched.group(1)),
                    _phase_slug(matched.group(2)),
                    any(boxes),
                    has_box and all(boxes),
                    has_box,
                )
            )
        elif _NOTES_HEADING.match(heading):
            saw_notes = True
    if not phases:
        return "notes" if saw_notes else None

    phases.sort(key=lambda item: item[0])
    token: str | None = None
    previous_complete = True
    for index, (number, slug, has_checked, all_checked, has_box) in enumerate(phases):
        if index == 0 or has_checked or previous_complete:
            token = f"{number} {slug}"
        previous_complete = bool(has_box and all_checked)
    return token


# The `**Archived:**` stamp is a template convention, not a guarantee (CBN-152):
# CBN-145's archived note ends right after its Final Summary paragraph with no
# stamp at all, which used to make this pattern fail to match and silently fall
# back to the PLAN stub. The end-of-string alternative reads a producer's actual
# output either way.
_FINAL_SUMMARY = re.compile(
    r"\*\*Final Summary:\*\*\s*(.*?)(?:\s*\*\*Archived:\*\*|\s*\Z)",
    re.DOTALL,
)
_DE_SCOPE = re.compile(r"\*\*Verdict:\*\*\s*De-scope\b", re.IGNORECASE)
# A worker's follow-up section (CBN-121). Heading-initial prefix match, not an
# exact spelling: the ticket named `### Follow-ups proposed` as an existing
# convention and it is not one — flaitron's pinned SPEC has no such heading, and
# this repo's own archive carries seven spellings across ten notes. Matching the
# prefix reads what producers actually write; requiring one spelling would be
# defining their format (§"Fleet boundary"). `### Follow-ups proposed` is the
# canonical spelling to *write*, recorded in CONTRACT, not the only one read.
_FOLLOWUP_HEADING = re.compile(r"^###\s+Follow-ups?\b", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^#{1,3}\s")
# The extraction unit. Table rows are deliberately not one: a row has no
# canonical projection to a single line, and picking a cell would be deciding
# what a producer's table means.
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_FOLLOW_UPS_MAX = 10


_DOC_DRIFT_LEAD = re.compile(r"[*_`\s]*doc-drift\b", re.IGNORECASE)


def extract_summary(text: str) -> str:
    match = _FINAL_SUMMARY.search(text)
    if not match:
        return ""
    paras = re.split(r"\n\s*\n", match.group(1).strip())
    # Skip a leading doc-drift paragraph (CBN-277): a fleet repo's FE-036 led with its
    # sweep's file list, so the record named unchanged docs, not the outcome.
    lede = next((p for p in paras if not _DOC_DRIFT_LEAD.match(p)), paras[0])
    return clip(lede)


def extract_follow_ups(text: str) -> list[str] | None:
    """Follow-up lines a closing worker proposed, or None when it proposed none.

    Reads every `### Follow-up…` section in document order and returns their list
    items — the discoveries an unattended run would otherwise leave sealed in an
    archived note (CBN-121). Capped at `_FOLLOW_UPS_MAX` items, each clipped like
    a summary; `None` rather than `[]` so `exclude_none` keeps the key off every
    record with nothing to say, the same shape `RegistryRecord.providers` uses.

    **Fenced items count, and that is the load-bearing case.** CBN-120.2's three
    ready-to-paste `CORE-XXX` lines — the follow-ups whose loss filed this work —
    live inside a ```` ```markdown ```` fence. Fence state is tracked only so a
    heading *inside* a fence cannot open a section and a fence delimiter cannot
    close one; items are read straight through it.
    """
    lines = text.splitlines()
    fenced = fence_mask(lines)
    found: list[str] = []
    inside = False
    for line, in_fence in zip(lines, fenced, strict=True):
        if not in_fence and _ANY_HEADING.match(line):
            inside = _FOLLOWUP_HEADING.match(line) is not None
            continue
        if not inside:
            continue
        item = _LIST_ITEM.match(line)
        if item is not None:
            found.append(clip(item.group(1)))
    return found[:_FOLLOW_UPS_MAX] or None


# Flaitron's Phase 3 `**External review**` box (CORE-656), ticked or not. The
# label is matched literally; what follows it is the disposition the runner
# wrote, in whatever shape (`— N/A: …`, `: /code-review medium …`, the
# template's own text, or nothing). Text *before* the label is tolerated too:
# Fleet HQ's NAT-323 marked every Phase 2/3 box `- [x] N/A (De-scope) — **…**`.
_EXTERNAL_REVIEW_BOX = re.compile(r"^\s*- \[([ xX])\] (.*?)\*\*External review\*\*(.*)$")
# The same box with its bold dropped (CBN-341: a fleet repo's FE-106.2 wrote
# `- [x] External review — N/A: …`). Unbolded, the label counts only when it
# opens the box text, so a box that merely mentions an external review is not it;
# the empty group keeps the bold form's group numbering.
_EXTERNAL_REVIEW_BOX_PLAIN = re.compile(r"^\s*- \[([ xX])\] ()External review\b(.*)$")
# The micro-tasknote's twin (flaitron v7, `CORE-778.2`): a bold-prefix line in
# `## ⚡ Notes` rather than a box, so there is no tick — an unfilled `<…>`
# template placeholder or an empty value is the unticked case (CBN-330).
_EXTERNAL_REVIEW_LABEL = re.compile(r"^\s*(?:[-*+]\s+)?\*\*External review(?::\*\*|\*\*:)(.*)$")
# A ticked box's disposition written below it, in Testing Notes (CBN-337): the
# template box ends "… or `N/A` with reason", so ten cbn notes ticked it and put
# `External review: N/A — …` underneath, as a list item, a bold label, or
# after a sentence's `. `. The colon is required, so the box's own
# `**External review** —` line never matches; the label must open the line or a
# sentence, so a phrase quoted in inline code (`` `External review: N/A` ``) is not
# the note's own disposition.
_EXTERNAL_REVIEW_BELOW = re.compile(
    r"(?:^\s*(?:[-*+]\s+)?|[.;]\s+)\*{0,2}External review(?:\*\*)?:(.*)$",
    re.IGNORECASE,
)
_DISPOSITION_LEAD = " \t—\u2013-:,`(*_"  # em dash, en dash, hyphen, emphasis, …

ExternalReviewBox = Literal["ticked", "na", "missing"]


def external_review_box(text: str) -> ExternalReviewBox:
    """How an archived note's External review box closed (CBN-257).

    `na` when the ticked box's disposition *leads* with `N/A`, on either side
    of the label — the template's own text mentions "`N/A` with a one-line
    reason" mid-sentence, so a substring match would read every untouched box
    as `na`. A ticked box whose own text does not lead with `N/A` defers to
    the first unfenced `External review:` line below it in the same `##`
    section — Testing Notes — read by the same rule (CBN-337). An unbolded box
    label counts when it opens the box text inside a Phase 3 section (CBN-341).
    `missing` when the box is unticked, or absent from a full-shape note: the
    review did not run — unless the first unfenced `External review:` line in
    a Phase 3 section leads with `N/A`, a stated disposition with the box
    dropped, which reads `na` (CBN-341).
    A v7 micro-tasknote records it as an `**External review:**` line in
    `## ⚡ Notes` instead (CBN-330), read by the same leading-`N/A` rule, with an
    empty or unfilled `<…>` value as `missing`. A micro note without that line
    predates it — v6 micro notes and the retired `/ft-micro-task`'s
    `## ✅ Recap` notes had no review by design — so its absence reads `na`.
    The label counts only on a micro note; on a full note the box decides. The
    first unfenced box or label wins; one quoted inside a fence is not the
    note's own.
    """
    lines = text.splitlines()
    fenced = fence_mask(lines)
    phased = noted = in_phase_3 = False
    label: str | None = None
    boxless: str | None = None
    for index, (line, in_fence) in enumerate(zip(lines, fenced, strict=True)):
        if in_fence:
            continue
        # Unbolded, the box counts only inside Phase 3: a fleet repo's BE-077 lists
        # `- [ ] External review against Acceptance` under Subtasks.
        box = _EXTERNAL_REVIEW_BOX.match(line) or (
            _EXTERNAL_REVIEW_BOX_PLAIN.match(line) if in_phase_3 else None
        )
        if box is not None:
            return _box_disposition(box, lines[index + 1 :], fenced[index + 1 :])
        if _SECTION_HEADING.match(line):
            phase = _PHASE_HEADING.match(line)
            in_phase_3 = phase is not None and phase.group(1) == "3"
            phased = phased or phase is not None
            noted = noted or _NOTES_HEADING.match(line) is not None
        elif in_phase_3 and boxless is None:
            boxless = _phase_3_label(line)
        labelled = _EXTERNAL_REVIEW_LABEL.match(line)
        if labelled is not None and label is None:
            label = labelled.group(1).lstrip(_DISPOSITION_LEAD)
    if phased:
        return "na" if boxless is not None and boxless.upper().startswith("N/A") else "missing"
    if not noted:
        return "missing"
    return _micro_label_disposition(label)


def _phase_3_label(line: str) -> str | None:
    """A Phase 3 `External review:` line's disposition, for a full note that dropped its box."""
    below = _EXTERNAL_REVIEW_BELOW.search(line)
    return None if below is None else below.group(1).lstrip(_DISPOSITION_LEAD)


def _micro_label_disposition(label: str | None) -> ExternalReviewBox:
    """A micro note's `**External review:**` value: absent `na`, empty or `<…>` `missing`."""
    if label is None:
        return "na"
    if not label.strip() or (label.startswith("<") and label.rstrip().endswith(">")):
        return "missing"
    return "na" if label.upper().startswith("N/A") else "ticked"


def _box_disposition(box: re.Match[str], lines: list[str], fenced: list[bool]) -> ExternalReviewBox:
    """A matched box: unticked `missing`, a leading `N/A` either side of the label `na`."""
    if box.group(1) == " ":
        return "missing"
    leads = (box.group(2).lstrip(_DISPOSITION_LEAD), box.group(3).lstrip(_DISPOSITION_LEAD))
    if any(t.upper().startswith("N/A") for t in leads):
        return "na"
    return _below_box_disposition(lines, fenced)


def _below_box_disposition(lines: list[str], fenced: list[bool]) -> ExternalReviewBox:
    """`na` when the first `External review:` line under a ticked box leads with `N/A`."""
    for line, in_fence in zip(lines, fenced, strict=True):
        if in_fence:
            continue
        if _SECTION_HEADING.match(line):
            break
        below = _EXTERNAL_REVIEW_BELOW.search(line)
        if below is not None:
            lead = below.group(1).lstrip(_DISPOSITION_LEAD)
            return "na" if lead.upper().startswith("N/A") else "ticked"
    return "ticked"


def is_descoped(text: str) -> bool:
    return _DE_SCOPE.search(text) is not None
