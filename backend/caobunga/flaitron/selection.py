"""Dispatch policy: which PLAN row launches next, and why it is or is not eligible.

`pick_next` and the two dimensions it answers on (`unattended` safety, `drain`
scope), the `eligibility` check order and every denial reason string it returns,
`in_flight_task`, `coverage`, `marker_drift`, and the sort keys the launch buckets
rank by. Split out at CBN-143.8; imports only downward, from `.dto`, `.grammar`
and `.reader`.

**This module is contract-entangled, and the entanglement is the check order.**
`docs/CONTRACT.md` §"Flaitron reader" fixes it: `[xheavy]` is refused *ahead* of
the `[unattended]` marker (CBN-114) so the reason names a fix that exists —
seeding a marker on an exploratory row would not make it dispatchable (the
`frontier` band, CBN-331, sits right behind it for the same reason) — and
CBN-120.3 confirmed that ordering live on a real queue, where the reason rather
than the allow/deny was the discriminator. The reason strings are read by
operators and pinned verbatim in `backend/tests/test_flaitron.py`; they are
contract surface, not diagnostics.

`LAUNCH_BUCKETS` deliberately lives in `reader.py`, not here, though this module
is its main consumer: three of its four uses are reader-side and `selection`
already imports `reader`, so owning it here would invert that edge into a cycle
(CBN-143.7).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable

from caobunga.flaitron.dto import (
    Coverage,
    Denial,
    NextTask,
    OpenTasknote,
    PlanTask,
    TriageClass,
    UnparsedRow,
)
from caobunga.flaitron.grammar import (
    BLOCKED_MENTION,
    FRONTIER_TOKENS,
    HANDOFF_MARKER,
    PARENT_EPIC,
    UNATTENDED_MARKER,
    XHEAVY_TOKEN,
    _blank_code_spans,
    epic_parent,
    epic_stem,
    split_park_reason,
)
from caobunga.flaitron.reader import (
    LAUNCH_BUCKETS,
    seeded_line,
)


def completed_task_ids(tasks: Iterable[PlanTask]) -> frozenset[str]:
    """Ids of checked PLAN rows — what `eligibility` resolves a blocker against."""
    return frozenset(task.task_id for task in tasks if task.completed)


def open_task_ids(tasks: Iterable[PlanTask]) -> frozenset[str]:
    """Ids of unchecked PLAN rows — what the `audit` dimension finds open siblings in."""
    return frozenset(task.task_id for task in tasks if not task.completed)


_PRIORITY_RANK = {"High": 0, "Medium": 1, "Low": 2}
_AUDIT_SORT = 10**9


def pick_next(
    tasks: list[PlanTask],
    notes: list[OpenTasknote] | None = None,
    *,
    unattended: bool = False,
    drain: bool = False,
) -> NextTask | None:
    """The task to dispatch next, or None when nothing is eligible.

    Two independent dimensions, split apart at CBN-118 after one parameter drove
    both since CBN-EPIC-17.3:

    - `unattended=True` — **this dispatch launches a headless worker**, so only
      rows carrying the `[unattended]` marker are eligible. True on every
      launching tier, `gated` included: `worker_message` sends `--unattended` /
      `--fast` whatever the tier, so no launch is ever gate-answered by a human,
      and `gated`'s stop lands *after* the worker's commit rather than before it.
    - `drain=True` — **nobody is watching the sequence**, so the in-progress
      override is dropped: a note the operator left open is their work, and
      preferring it is how an unwatched drain would race them in their own repo
      (CBN-EPIC-17.3). True on `proceed-on-green` only. The dead-session
      strand this used to cover survives without it — a worker that died mid-task
      left PLAN untouched, so spec-selection returns the same id it returned
      before, and `worker_message` still recognizes the `in-progress` note and
      sends the continue prompt (CBN-109).

    The sets stand in strict containment, so `drain` without `unattended` is
    unrepresentable rather than merely discouraged — it would silently reinstate
    the exact race CBN-EPIC-17.3 closed, the same footgun `worker_message` guards
    with a required keyword.

    The *display* half of the override moved to `in_flight_task` below, which
    every read reports regardless of posture.
    """
    if drain and not unattended:
        msg = "drain implies unattended: a drain is headless by definition"
        raise ValueError(msg)
    by_id = {note.task_id: note for note in notes or []}
    done = completed_task_ids(tasks)
    still_open = open_task_ids(tasks)
    if not drain:
        # The override is a *dispatch* preference, so it is subject to every
        # dispatch dimension — the marker one included, which is why the posture
        # is threaded here rather than hardcoded attended (CBN-118).
        # `in_flight_task` deliberately is not (it reports what is open), which
        # is why the shared chooser is re-checked here rather than the display
        # helper being reused wholesale (CBN-116).
        flight = _in_flight(tasks, by_id)
        if flight is not None and _is_eligible(
            flight, unattended=unattended, completed_ids=done, open_ids=still_open
        ):
            return _as_next(
                flight,
                by_id,
                "in-progress-override",
                unattended=unattended,
                completed_ids=done,
                open_ids=still_open,
            )
    eligible = [
        task
        for task in tasks
        if _is_eligible(
            task,
            unattended=unattended,
            completed_ids=done,
            note=by_id.get(task.task_id),
            open_ids=still_open,
        )
    ]
    if not eligible:
        return None
    return _as_next(
        min(eligible, key=_selection_key),
        by_id,
        "spec-selection",
        unattended=unattended,
        completed_ids=done,
        open_ids=still_open,
    )


def in_flight_task(
    tasks: list[PlanTask],
    notes: list[OpenTasknote] | None = None,
) -> NextTask | None:
    """The task the operator has open right now — the override's display half.

    Read on **every** posture, because it answers "what is currently in flight"
    rather than "what would be dispatched next". Splitting it out is what keeps
    the dashboard honest once the picker stops preferring in-progress notes
    (CBN-040 / CBN-011: this is the only durable current-task signal — run
    records are written at close). Eligibility here is always the attended base,
    so display never varies with the caller's posture.
    """
    by_id = {note.task_id: note for note in notes or []}
    chosen = _in_flight(tasks, by_id)
    if chosen is None:
        return None
    done = completed_task_ids(tasks)
    return _as_next(chosen, by_id, "in-progress-override", completed_ids=done)


def _in_flight(tasks: list[PlanTask], by_id: dict[str, OpenTasknote]) -> PlanTask | None:
    """The open-queue row with an `in-progress` note, by SPEC order.

    Membership, not dispatchability: a row carrying a `Blocked by` clause that
    an operator nonetheless has open is still what is open, and blanking the
    dashboard over it would be the display blind spot CBN-EPIC-17.3 split this
    out to prevent. `pick_next` re-checks full eligibility before dispatching.
    """
    flight = [
        task
        for task in tasks
        if queue_denial(task) is None
        and (note := by_id.get(task.task_id)) is not None
        and note.status == "in-progress"
    ]
    return min(flight, key=_selection_key) if flight else None


def _as_next(
    chosen: PlanTask,
    by_id: dict[str, OpenTasknote],
    reason: str,
    *,
    unattended: bool = False,
    completed_ids: Collection[str] | None = None,
    open_ids: Collection[str] | None = None,
) -> NextTask:
    note = by_id.get(chosen.task_id)
    _, why = eligibility(
        chosen, unattended=unattended, completed_ids=completed_ids, note=note, open_ids=open_ids
    )
    return NextTask(
        task_id=chosen.task_id,
        plan_model=chosen.model,
        shortname=chosen.shortname,
        priority=chosen.priority,
        critical=chosen.critical,
        description=chosen.description,
        reason=reason,
        eligibility_reason=why,
        tasknote=note,
    )


def queue_denial(task: PlanTask) -> str | None:
    """Why this row is not in the open launch queue at all, or None if it is.

    The three **membership** dimensions — checked, bucket, parent-epic tracking
    row. Split out of `eligibility` (CBN-116) because two callers need
    membership rather than dispatchability: `coverage` (whose `open` tally is
    documented as this set, and whose `marked + denied == open` invariant holds
    only if the blocked dimension denies *inside* the queue rather than
    shrinking it) and `in_flight_task` (which reports what an operator has open,
    not what may dispatch). Both were written against `eligibility` back when
    the two ideas were the same set; this keeps them byte-identical.
    """
    if task.completed:
        return "completed — row is checked"
    if task.priority.value not in LAUNCH_BUCKETS:
        return f"bucket — {task.priority.value} is outside the launch buckets"
    if PARENT_EPIC.fullmatch(task.task_id) is not None:
        return "parent-epic — tracking row; children launch instead"
    return None


def _blocked_denial(task: PlanTask, completed_ids: Collection[str] | None) -> str | None:
    """The `Blocked by [[ID]]` don't-start gate, or None when nothing blocks.

    Applies at **every** posture. Flaitron scopes the clause to no tier — it is
    the operator writing "do not start this" on the row — so an attended `gated`
    pick honors it exactly as a drain does (CBN-116).

    `SPEC/blocked.md` makes *clause removal* the act that clears the gate ("the
    task re-enters when the blocker clears — remove the `Blocked by` clause"),
    so a clause whose blockers are all already checked still denies rather than
    auto-releasing: dispatching a row whose PLAN line literally reads "Blocked
    by" would be the orchestrator overruling the operator's own text. It is
    reported as stale, though, because the fix is a one-line PLAN edit and the
    operator should not have to diff the two rows to find it.

    A blocker absent from PLAN cannot be *confirmed* complete, so it reads as
    still-blocking — the same bias-to-refuse the marker dimension applies.
    `completed_ids=None` means undetermined, which never annotates.
    """
    if not task.blocked_by:
        return None
    clause = ", ".join(f"[[{blocker}]]" for blocker in task.blocked_by)
    reason = f"blocked — Blocked by {clause}"
    if completed_ids is not None and all(b in completed_ids for b in task.blocked_by):
        return f"{reason} (completed; clause is stale, remove it)"
    return reason


def unparsed_blocked_clause(task: PlanTask) -> str | None:
    """A blocker in the description the `Blocked by [[ID]]` grammar cannot see (CBN-128).

    The `Blocked by` dimension above is not broken — it is **inert on this
    fleet**. CBN-123 seeded three rows on this report's own recommendation and
    all three carried real blockers the grammar drops: `Blocked by <prose>` with
    no wikilink, and `Blocked on [[ID]]` twice. One read *"do not start before
    the rows exist"* and was marked `[unattended]` anyway. Every clause sat past
    the 96-character elision, which CBN-119.3 sized so marker *placement* stays
    visible — and which therefore hides *eligibility* by construction.

    The test is exact rather than heuristic: the two readings of the same
    sentence **disagree**. The description mentions "blocked" outside a code
    span, and `parse_blocked_by` captured nothing from it. A row the grammar
    does handle cannot fire this, because a captured clause makes `blocked_by`
    non-empty by construction — so the signal never duplicates a `blocked —`
    reason. Code spans are excluded for the same reason `parse_blocked_by`
    excludes them: an example in backticks is literal text.

    Returns the fragment from the mention to the end of the description, not the
    whole line. Both ends earn that: a renderer that elides it spends its budget
    on the clause instead of the decorations, and the operator reads the
    producer's own words rather than a caobunga paraphrase of them.

    **Report-only, and deliberately not a gate.** Denying here — or withholding
    `seeded_line` when this fires — would make Caobunga decide that a producer's
    sentence means "blocked", which is exactly what widening the grammar was
    rejected for (§"Fleet boundary"). The upstream half is filing discipline and
    belongs in flaitron (CBN-126). Same posture as `unparsed_plan_rows` and
    `marker_drift`: state it, refuse nothing.
    """
    if task.blocked_by or not task.description:
        return None
    match = BLOCKED_MENTION.search(_blank_code_spans(task.description))
    if match is None:
        return None
    return task.description[match.start() :].strip() or None


# Flaitron's `[unattended]` candidacy predicate clause 3
# (`SPEC/unattended-candidacy.md` §"Candidacy predicate", CORE-577.2), mirrored
# rather than invented (CBN-157). Two layers (CORE-783.2, CBN-361): this tuple
# is the mechanical screen and gains no visual or raster keywords. A hit is
# `needs_judgment`. A miss is not a semantic pass, so it is not `obvious`;
# that decline belongs to an LLM filer and `/ft-prep-unattended`.
_HANDOFF_KEYWORDS: tuple[str, ...] = (
    "👁️",
    "🗄️",
    "▶️",
    "📡",
    "💻",
    "✋",
    "operator",
    "hand-off",
    "handoff",
    "manual",
    "credential",
    "secret",
    "password",
    "physical",
    "cross-repo",
    "production",
    "deploy",
    "migration",
)


def _keyword_hit(description: str) -> str | None:
    """First candidacy-predicate keyword or glyph `description` trips, or None."""
    lowered = description.casefold()
    for word in _HANDOFF_KEYWORDS:
        if word.casefold() in lowered:
            return word
    return None


def _stem_predecessor_gap(task: PlanTask, completed_ids: Collection[str] | None) -> str | None:
    """Clause 6's epic-child rule, or None when it does not deny.

    Mirrors the predicate's own declared fallback — "`.k-1` when undeclared" —
    since this reader has no access to a tasknote's `## 🌳 Fan-out` claim
    (that lives in a tasknote body; `flaitron/` reads PLAN.md and tasknote
    *frontmatter* only, never phase content). A `.1` Discovery row is never a
    candidate; a `.N` audit row is not a predecessor gap and skips this check;
    any other `.k` needs its numeric predecessor already closed. A non-numeric or
    unresolvable suffix is the predicate's own "uncertain clause" case, so it
    denies rather than guessing.
    """
    stem = epic_stem(task.task_id)
    if stem is None:
        return None
    suffix = task.task_id[len(stem) + 1 :]
    if suffix == "1":
        return "a .1 Discovery row is never a candidate"
    if suffix == "N":
        return None
    if not suffix.isdigit() or int(suffix) <= 1:
        return f"epic-child suffix '.{suffix}' has no numeric predecessor to confirm"
    predecessor = f"{stem}.{int(suffix) - 1}"
    if completed_ids is not None and predecessor in completed_ids:
        return None
    return f"stem predecessor {predecessor} is not confirmed closed"


def _unmarked_triage(
    task: PlanTask,
    completed_ids: Collection[str] | None,
    open_ids: Collection[str] | None,
) -> tuple[TriageClass, str | None]:
    """Split an `unmarked` denial by flaitron's `[unattended]` candidacy predicate.

    Only the clauses `eligibility` has not already cleared by the time a row
    reaches the `unmarked` reason are checked here: clause 5 (parent-epic) is a
    `queue_denial` membership dimension, and the marker form of clause 3
    (`[handoff]`) and clause 4's parsed half (`Blocked by [[ID]]`) both deny
    ahead of `unmarked` in `eligibility`'s own check order. What is left —
    `[model]` tier, `[!critical]`, the free-text keyword form of clause 3, the
    unparsed half of clause 4, and clause 6's epic-child rule — is exactly what
    an `unmarked` row has not yet been judged on. Conservative by the same rule
    the predicate states: the first clause that cannot confirm `obvious` decides
    `needs_judgment`, and nothing here ever authorizes a seed (§"Marker coverage"
    — recommend, never write).

    The `audit` dimension is mirrored too (CBN-353): `eligibility` checks it only
    on a *marked* row. An unmarked audit child with open epic siblings is judged
    here, ahead of the keyword-miss result, so the reason names the siblings.

    A clause 3 keyword miss is not a semantic pass (CBN-361). When every other
    mirrored clause is clean, the row is `needs_judgment` with
    `keyword — a miss is not a semantic pass`, never `obvious`. The semantic
    decline of a person-shaped row belongs to an LLM filer and
    `/ft-prep-unattended`, which this reader does not run.
    """
    if task.model is None or task.model.strip().casefold() not in ("light", "medium"):
        shown = f"[{task.model}]" if task.model else "no [model] tag"
        return TriageClass.NEEDS_JUDGMENT, f"model — {shown} is not [light]/[medium]"
    if task.critical:
        return (
            TriageClass.NEEDS_JUDGMENT,
            "critical — [!critical] pulls an operator's eye by design",
        )
    hit = _keyword_hit(task.description)
    if hit is not None:
        return TriageClass.NEEDS_JUDGMENT, f"keyword — description mentions {hit!r}"
    if unparsed_blocked_clause(task) is not None:
        return (
            TriageClass.NEEDS_JUDGMENT,
            "unparsed-blocker — description reads as blocked, no `Blocked by [[ID]]` clause parsed",
        )
    stem_gap = _stem_predecessor_gap(task, completed_ids)
    if stem_gap is not None:
        return TriageClass.NEEDS_JUDGMENT, f"epic-child — {stem_gap}"
    audit_gap = _audit_sibling_gap(task, open_ids)
    if audit_gap is not None:
        return TriageClass.NEEDS_JUDGMENT, audit_gap
    return TriageClass.NEEDS_JUDGMENT, "keyword — a miss is not a semantic pass"


def _audit_suffix(task: PlanTask) -> str | None:
    """The epic-child suffix when the row is an audit child, else None (CBN-350).

    The reserved `.N`, or a numbered child whose `| shortname` ends in the word
    `audit` / `audits` (trailing punctuation ignored) — flaitron `SPEC/epic.md`
    keeps a numeric audit child valid, and those rows carry the word only in the
    shortname (a fleet repo's `DEPLOY-EPIC-1.4 | access-gate audit`, 2026-10-12).
    `.1` is Discovery, never an audit. A false match only holds a row back, so
    the word test leans wide rather than narrow.
    """
    stem = epic_stem(task.task_id)
    if stem is None:
        return None
    suffix = task.task_id[len(stem) + 1 :]
    if suffix == "N":
        return suffix
    if (
        suffix.isdigit()
        and int(suffix) > 1
        and task.shortname is not None
        and re.search(r"(?:^|[\W_])audits?\W*$", task.shortname.casefold())
    ):
        return suffix
    return None


def _audit_sibling_gap(task: PlanTask, open_ids: Collection[str] | None) -> str | None:
    """The `audit` dimension's reason for an audit row with open siblings, or None (CBN-309).

    An audit row is `.N` or a numbered audit child (`_audit_suffix`, CBN-350).
    Siblings are the unchecked rows sharing the audit's `epic_parent`, so both
    child forms (`CBN-41.2`, `CBN-EPIC-41.2`) count as one cohort; the parent
    tracking row is not a child, so it never counts. A numbered audit waits on
    *every* open sibling, later ones and a `.N` included — `/ft-close-epic` Step 2
    bails on any of them — so an epic carrying both audit forms denies both,
    legibly, until the operator resolves the shape. `open_ids=None` cannot
    confirm the siblings closed and denies (bias-to-refuse); a caller that means
    to skip the dimension passes an empty set.
    """
    suffix = _audit_suffix(task)
    if suffix is None:
        return None
    if open_ids is None:
        return "audit — epic siblings cannot be confirmed closed"
    parent = epic_parent(task.task_id)
    siblings = sorted(
        (sid for sid in open_ids if sid != task.task_id and epic_parent(sid) == parent),
        key=_task_id_sort_key,
    )
    if not siblings:
        return None
    return (
        f"audit — epic siblings still open ({', '.join(siblings)}); "
        f"the .{suffix} audit runs after they close"
    )


def eligibility(
    task: PlanTask,
    *,
    unattended: bool = False,
    completed_ids: Collection[str] | None = None,
    note: OpenTasknote | None = None,
    open_ids: Collection[str] | None = None,
) -> tuple[bool, str]:
    """Ten-dimension launch eligibility with a legible reason (CBN-EPIC-17.2, CBN-116).

    Three membership dimensions (`queue_denial`), then the `Blocked by` gate,
    then the six safety dimensions. The last six fire only when `unattended=True`
    — the caller's declaration that this dispatch launches a headless worker, true
    on every launching tier since CBN-118.

    The **parked** dimension (CBN-252) denies a row whose open tasknote (`note`)
    is `status: blocked`. A headless worker has nothing to do there: the park is
    waiting on an operator act, and before this every re-run re-picked the same
    row, wrote a `note-blocked` record, and spent a Hermes review on it. It is
    checked first among the safety dimensions because the fix it names — resume
    attended with `/ft-task <ID>` — is the only one that clears the row whatever
    else it carries. Callers without notes (`marker_drift`) omit `note` and never
    see it; a parked row is not an evaporated marker.

    The **exploratory** dimension (CBN-114) denies any `[xheavy]` row outright:
    `SPEC/model.md` reserves that rung to the operator, so an orchestrator must
    never pick one up. It is checked *before* the marker so the reason names the
    reachable fix — seeding `[unattended]` on an `[xheavy]` row would not make it
    dispatchable, and reporting `unmarked` would send an operator reading
    `Coverage.denials` to do exactly that.

    The **frontier** dimension (CBN-331) denies the frontier band — `[frontier]`
    and the concrete `fable` / `mythos` — on the same terms. Flaitron lets an
    automated chooser reach `[frontier]`; caobunga caps itself at `[heavy]`
    because the token-heavy rungs are the operator's to authorize (operator
    decision 2026-10-11). It sits right after `exploratory`, before `handoff`
    and the marker, for the same reason: seeding a marker does not fix it.

    The **handoff** dimension (CBN-136) denies any `[handoff]` row: the operator
    declared at filing time that this task stops mid-run for a human act that is
    not another task, so a headless worker can only reach the park. It sits
    between the two for the same check-order reason CBN-114 gives — an
    `[xheavy]` row is exploratory whether or not it also hands off, and reporting
    `unmarked` here would send an operator to seed a marker that changes nothing.
    It follows `frontier` too, for the same reason.

    It denies **even when `[unattended]` is also present**, which is the one
    behaviour a report-only signal could not supply and the reason this is a gate
    rather than an annotation: CBN-123 measured an operator seeding `[unattended]`
    on a row whose own description read "do not start before the rows exist". The
    counter-argument is real and was weighed — a hand-off dispatch parks rather
    than committing unreviewed work, so CBN-119.2's "under-dispatch is not unsafe
    dispatch" rule argues for report-only — and it lost to the mis-seed case.

    Attended reads never deny on it. The declaration is about a *headless* run;
    with an operator present the hand-off is simply the work, so the reason says
    so and the next-task suggester surfaces the row rather than hiding it.

    Both tokens deny **wherever they sit on the row** (CBN-260), the `[model]`
    slot or the trailing run: the grammar captures the first token as the model
    whatever it is, so a slot-keyed check let `[handoff] [unattended]` and
    `[heavy] [xheavy] [unattended]` through as `marked`.

    The **marker** dimension denies any task not carrying the `[unattended]`
    marker (bias-to-refuse: an unmarked task costs one operator confirmation; an
    unsafe dispatch commits unreviewed work — and at `gated` as much as on a
    drain, since the stop lands after the worker's commit).

    The **audit** dimension (CBN-309) denies a *marked* `.N` row while any sibling
    in its epic is unchecked — and so a numbered audit child, shortname ending
    in `audit` (CBN-350): `/ft-close-epic` parks
    an early audit, so the worker could only reach the park — and the parked
    note it leaves dirties the tree for the next row (a fleet repo's JD-041.N,
    2026-10-08). It is checked **after** the marker, unlike CBN-114's ordering,
    because here seeding is still a valid fix:
    a cohort seeded in one pass is marked together, so an unmarked `.N` keeps its
    `unmarked` reason and its `seeded_line`. `open_ids` (build it with
    `open_task_ids`) feeds it; omitting it denies a marked audit row, and an empty
    set skips the dimension.

    Reasons use the `<code> — <prose>` shape `split_park_reason` reads.
    Every branch returns one; silence is the failure mode this epic exists to
    close, so no decision is made without saying why.

    `completed_ids` resolves blocker ids against the same PLAN (build it with
    `completed_task_ids`); omitting it only costs the staleness annotation.
    """
    denial = queue_denial(task)
    if denial is not None:
        return False, denial
    blocked = _blocked_denial(task, completed_ids)
    if blocked is not None:
        return False, blocked
    if not unattended:
        if _declares_handoff(task):
            return True, (
                f"attended — this read does not consult the marker; "
                f"[{HANDOFF_MARKER}] declared, expect a mid-run operator hand-off"
            )
        return True, "attended — this read does not consult the marker"
    if note is not None and note.status == "blocked":
        code, _ = split_park_reason(note.park_reason)
        return False, (
            f"parked — tasknote is blocked ({code or 'no park-reason'}); "
            f"resume attended with /ft-task {task.task_id}"
        )
    above_cap = _above_cap_denial(task)
    if above_cap is not None:
        return False, above_cap
    if _declares_handoff(task):
        return False, (
            f"handoff — [{HANDOFF_MARKER}] declares a mid-run operator hand-off; "
            "a headless worker can only reach the park"
        )
    if task.unattended:
        audit_gap = _audit_sibling_gap(task, open_ids)
        if audit_gap is not None:
            return False, audit_gap
        return True, f"marked — [{UNATTENDED_MARKER}] on the PLAN line"
    return False, f"unmarked — no [{UNATTENDED_MARKER}] marker on the PLAN line"


def _row_tokens(task: PlanTask) -> set[str]:
    """Every bracket token on the row: the `[model]` slot plus the trailing run (CBN-260).

    `TASK_LINE` captures whichever token comes first after the id *as* the model,
    so a denial keyed to one slot misses the same token written one position over
    — `**X** [handoff] [unattended]` read as `model="handoff"` and dispatched as
    `marked`. The denials ask this set instead. The model slot is normalized the
    way `taskmap.classify` normalizes its token, so the two agree on what counts
    as the rung rather than one matching `[XHeavy]` and the other not. The
    trailing run is normalized too: its grammar pins only the first letter
    lowercase, so `[xHeavy]` there would otherwise slip past a check the same
    token in the model slot fails.
    """
    tokens = {token.casefold() for token in task.extras}
    if task.model is not None:
        tokens.add(task.model.strip().casefold())
    return tokens


def _above_cap_denial(task: PlanTask) -> str | None:
    """The `exploratory` or `frontier` reason for a rung above caobunga's `[heavy]` cap.

    `exploratory` first (CBN-114): `[xheavy]` is manual-only whatever else the row
    carries. Then the first frontier-band token, in either slot (CBN-331).
    """
    cap = "caobunga caps dispatch at [heavy], stricter than flaitron's [frontier]"
    tokens = _row_tokens(task)
    if XHEAVY_TOKEN in tokens:
        return f"exploratory — [{XHEAVY_TOKEN}] is manual-only (SPEC/model.md); {cap}"
    frontier = next((token for token in FRONTIER_TOKENS if token in tokens), None)
    if frontier is not None:
        return f"frontier — [{frontier}] is operator-authorized; {cap}"
    return None


def _declares_handoff(task: PlanTask) -> bool:
    """Whether the row carries `[handoff]`, in either slot (CBN-260)."""
    return task.handoff or HANDOFF_MARKER in _row_tokens(task)


def dispatchable(
    tasks: list[PlanTask],
    task_id: str,
    *,
    unattended: bool = False,
    notes: list[OpenTasknote] | None = None,
) -> bool:
    """Would this posture's pick allow `task_id`? (CBN-117)

    The id-addressed form of `_is_eligible`, for callers holding a *name* rather
    than a `PlanTask` — today the run record's `next_task`, which must not claim
    a row the same posture just refused. It reuses `eligibility` for the same
    reason `coverage` does: the marker rule lives in one predicate, so a claim
    about what runs next cannot drift from what runs.

    An id absent from PLAN answers `False`. It cannot be *confirmed* eligible,
    and this package refuses the undecidable rather than guessing — the same
    bias-to-refuse the safety and `Blocked by` dimensions already apply.
    """
    task = next((item for item in tasks if item.task_id == task_id), None)
    if task is None:
        return False
    note = next((item for item in notes or [] if item.task_id == task_id), None)
    return _is_eligible(
        task,
        unattended=unattended,
        completed_ids=completed_task_ids(tasks),
        note=note,
        open_ids=open_task_ids(tasks),
    )


def _denial(
    task: PlanTask,
    reason: str,
    *,
    completed_ids: Collection[str] | None = None,
    open_ids: Collection[str] | None,
) -> Denial:
    """Build a `Denial` from the row it describes (CBN-119.3).

    One constructor for all three sites — `coverage`'s repo-wide `denials`,
    `read_project`'s posture-dependent `denied`, and `marker_drift`. They stay
    separate lists for the reasons §"Marker coverage" gives, but a `Denial`
    built two ways would be a `Denial` that means two things: a second
    construction site would carry `line=0`, a zero that reads as data. The
    seeded line is offered only for an `unmarked` denial, since that is the
    only code a marker fixes — `eligibility` checks the exploratory and frontier
    dimensions *first* precisely so this gate names a reachable fix (CBN-114,
    CBN-331), and a
    `blocked` row needs the clause removed, not a marker.

    The unparsed blocker (CBN-128) is attached unconditionally rather than by
    code: it answers *whether* to seed, which is a question every denial a marker
    could fix has, and it self-excludes on the one code it would duplicate.

    The triage split (CBN-157) is scoped to `unmarked` the same way `seeded_line`
    is, and for the same reason: it is a further read on the marker question,
    not a verdict on `blocked`, `exploratory` or `frontier` rows. `completed_ids` feeds the
    epic-child stem check the same way it feeds `eligibility`'s own `Blocked by`
    dimension; omitting it only costs that one clause's confirmation. `open_ids`
    feeds the triage's `audit` mirror (CBN-353) and is required, so a new call site
    cannot silently drop it; `None` reads an audit child as `needs-judgment`
    (bias-to-refuse), never `obvious`.
    """
    code, _ = split_park_reason(reason)
    triage, triage_reason = (
        _unmarked_triage(task, completed_ids, open_ids) if code == "unmarked" else (None, None)
    )
    return Denial(
        task_id=task.task_id,
        reason=reason,
        line=task.line,
        text=task.text,
        seeded_line=seeded_line(task) if code == "unmarked" else None,
        unparsed_blocker=unparsed_blocked_clause(task),
        triage=triage,
        triage_reason=triage_reason,
    )


def coverage(
    tasks: list[PlanTask],
    unparsed: list[UnparsedRow] | None = None,
    notes: list[OpenTasknote] | None = None,
) -> Coverage:
    """Fold the open launch queue into marked / denied counts plus deny reasons.

    Reuses `eligibility` rather than re-deriving the marker rule: one predicate
    decides both what a drain dispatches and what this reports, so the report
    cannot drift from the behavior it describes. Derived per read and never
    persisted — a cached classification would be the second source of truth
    CBN-EPIC-17.1 rules out.

    `unparsed` (CBN-115) carries the rows the grammar dropped — see
    `unparsed_plan_rows`. They ride beside the tally, never inside `open`.

    Membership is `queue_denial`, not full eligibility, so a `Blocked by` row
    stays *inside* `open` and reports as a denial with a `blocked — ...` reason
    (CBN-116). Shrinking `open` instead would have let a repo read "fully
    covered" while carrying rows still to seed — the silent-blind-spot class
    CBN-115 closed one dimension earlier.

    **`denials` is ordered by `_selection_key`, so the list *is* the seeding
    order** (CBN-119.3): seed the first entry and it is the row that dispatches
    next. Iterating `parse_plan`'s output would give PLAN document order, which
    matched dispatch order on all five repos when this was written — but by
    coincidence of how the operator happened to file, not by construction.
    Sorting on the picker's own key makes it a property instead of an artifact,
    the same reason this reuses `eligibility` rather than re-deriving the rule.
    """
    marked: list[str] = []
    denials: list[Denial] = []
    done = completed_task_ids(tasks)
    still_open = open_task_ids(tasks)
    by_id = {note.task_id: note for note in notes or []}
    # `queue_denial` first, then sort: `_selection_key` ranks the launch buckets
    # only, and membership is exactly what filters the rest out.
    queue = sorted((t for t in tasks if queue_denial(t) is None), key=_selection_key)
    for task in queue:
        allow, reason = eligibility(
            task,
            unattended=True,
            completed_ids=done,
            note=by_id.get(task.task_id),
            open_ids=still_open,
        )
        if allow:
            marked.append(task.task_id)
        else:
            denials.append(_denial(task, reason, completed_ids=done, open_ids=still_open))
    return Coverage(
        open=len(marked) + len(denials),
        marked=len(marked),
        denied=len(denials),
        marked_tasks=marked,
        denials=denials,
        unparsed=len(unparsed) if unparsed else 0,
        unparsed_rows=list(unparsed) if unparsed else [],
    )


def marker_drift(tasks: list[PlanTask], baseline: Collection[str] | None) -> list[Denial]:
    """Rows armed at `baseline` that this read no longer dispatches (CBN-125).

    The marker-evaporation guard. A flaitron line rewrite that preserves the
    visible task text silently drops the trailing `[unattended]` bracket token,
    so a seeded cohort can be disarmed between two sessions by an edit that had
    nothing to do with it — observed live **twice on the same rows inside one
    trial** (CBN-120.2 F5: one fleet repo fell 3 marked → 0 while the run was parked,
    and the same rewrite added `Blocked by` clauses on top). Coverage is a
    point-in-time figure with no memory, so until now the disarm was invisible
    until a drain refused. `baseline` is the marked set a past run recorded
    (`RunRecord.marked_tasks`); the diff is read-only and report-only — nothing
    refuses over it and nothing re-arms a row.

    **The baseline is passed in, never fetched.** This module reads producers
    (`docs/CONTRACT.md` §"Fleet boundary") and holds no handle on Caobunga's own
    store; the join happens where the two already meet, in `cli.py`.

    **Scoped to rows still in the open launch queue** (`queue_denial` is None),
    which drops three false positives by construction rather than by filtering:
    a row that closed legitimately, a row the operator re-prioritized out of the
    launch buckets, and a row `## Completed` rotation moved to `PLAN-ARCHIVE.md`
    — a file this reader does not open, so it would otherwise report as
    evaporated forever. A row whose rewrite broke the *grammar* is not lost
    either: it already has its own channel in `unparsed_plan_rows`, echoed on
    the dispatch path since CBN-119.2. What survives is exactly the claim being
    made — armed then, still here, not armed now.

    **The verdict is `eligibility`, not `task.unattended`.** One predicate
    decides dispatch, coverage, and this, so the guard cannot drift from the
    behavior it describes — and it catches the other half of the observed event,
    since a marker-only test would have reported half that repo's damage.
    `_denial` builds every entry for the reason CBN-119.3 gave: a second
    construction site would carry `line=0`, a zero that reads as data.
    """
    if not baseline:
        return []
    was_marked = frozenset(baseline)
    done = completed_task_ids(tasks)
    still_open = open_task_ids(tasks)
    drift: list[Denial] = []
    queue = sorted(
        (t for t in tasks if t.task_id in was_marked and queue_denial(t) is None),
        key=_selection_key,
    )
    # The `audit` dimension is skipped (empty set): an epic gaining or reopening
    # a sibling is sequencing, not an evaporated marker (CBN-309). The denial's
    # triage still gets the real open set — it answers whether a re-seed would
    # dispatch (CBN-353).
    for task in queue:
        allow, reason = eligibility(task, unattended=True, completed_ids=done, open_ids=())
        if not allow:
            drift.append(_denial(task, reason, completed_ids=done, open_ids=still_open))
    return drift


def _denied_rows(
    tasks: list[PlanTask],
    *,
    unattended: bool,
    completed_ids: Collection[str],
    notes: list[OpenTasknote] | None = None,
) -> list[Denial]:
    """`read_project`'s posture-dependent `denied` list (CBN-146).

    Empty on an attended read; on an unattended one, every open-queue row the
    safety dimension refuses, in dispatch order. Lifted out of `read_project`
    so the façade reaches one selection-private name instead of three —
    `CBN-143.8` declined the move at its Q2 only to keep that step's verbatim
    standard, and `CBN-143.N` F4 re-raised it once the epic had closed.

    It does not delegate to `coverage(...).denials`, although the two lists are
    equal on an unattended read today. §"Marker coverage" keeps them separate
    by design: this one is gated on the *caller's* posture, `coverage` is the
    repo-wide report every posture sees. Making one a view of the other would
    invert that relationship; sharing `_denial` and `_selection_key` is what
    keeps them from disagreeing instead (CBN-119.3).
    """
    by_id = {note.task_id: note for note in notes or []}
    still_open = open_task_ids(tasks)
    # Filtered before sorting: `_selection_key` ranks the launch buckets only,
    # and `tasks` still holds every Completed / Future row at this point.
    return [
        _denial(
            task,
            eligibility(
                task,
                unattended=True,
                completed_ids=completed_ids,
                note=by_id.get(task.task_id),
                open_ids=still_open,
            )[1],
            completed_ids=completed_ids,
            open_ids=still_open,
        )
        for task in sorted(
            (
                task
                for task in tasks
                if unattended
                and queue_denial(task) is None
                and not _is_eligible(
                    task,
                    unattended=True,
                    completed_ids=completed_ids,
                    note=by_id.get(task.task_id),
                    open_ids=still_open,
                )
            ),
            key=_selection_key,
        )
    ]


def _is_eligible(
    task: PlanTask,
    *,
    unattended: bool = False,
    completed_ids: Collection[str] | None = None,
    note: OpenTasknote | None = None,
    open_ids: Collection[str] | None = None,
) -> bool:
    return eligibility(
        task, unattended=unattended, completed_ids=completed_ids, note=note, open_ids=open_ids
    )[0]


def _selection_key(task: PlanTask) -> tuple[object, ...]:
    return (
        _PRIORITY_RANK[task.priority.value],
        0 if task.critical else 1,
        _task_id_sort_key(task.task_id),
    )


def _task_id_sort_key(task_id: str) -> tuple[tuple[int, str], ...]:
    """Natural-order key whose every element is an `(int, str)` pair (CBN-342).

    A lettered piece (`2a`) is one element, not an int followed by a bare str, so
    `X-1.2a` and `X-1.2.1` compare position-for-position instead of raising
    `TypeError` on `"a"` against `1`.
    """
    area, _, rest = task_id.partition("-")
    if rest.startswith("EPIC-"):
        rest = rest[5:]
    parts: list[tuple[int, str]] = [(0, area)]
    for piece in rest.split("."):
        if piece == "N":
            parts.append((_AUDIT_SORT, ""))
            continue
        match = re.fullmatch(r"(\d+)([a-z]?)", piece)
        if match:
            parts.append((int(match.group(1)), match.group(2)))
        else:
            parts.append((-1, piece))
    return tuple(parts)
