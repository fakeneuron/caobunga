"""The reader's own type vocabulary: the PLAN/tasknote DTOs `caobunga.flaitron` returns.

`PlanPriority`, the private `_Dto` base, and the seven pydantic records every other
part of this package produces or consumes. A leaf module — `enum` + `pydantic` only,
no sibling import — split out at CBN-143.6.

Deliberately **not** folded into `caobunga/models.py`, which owns the store/registry
domain (`RegistryRecord`, `RunRecord`, `ExecutionTier`, `Outcome`). `docs/CONTRACT.md`
draws that boundary from the other side: `NewRow` lives in `models.py` "rather than
`flaitron/` ... the record layer mirrors a producer's vocabulary instead of importing
the producer reader." Merging these in would be that edge inverted, joining two
contract-separated domains (§"Registry" / §"Run record" vs §"Flaitron reader").

Field names here are contract-pinned: `backend/tests/test_contract.py` asserts them
against `docs/CONTRACT.md`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PlanPriority(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    FUTURE = "Future Opportunities"
    COMPLETED = "Completed"


class TriageClass(StrEnum):
    """Split of an `unmarked` `Denial`, mirroring flaitron's `[unattended]`
    candidacy predicate (`SPEC/unattended-candidacy.md` §"Candidacy predicate",
    CORE-577.2) over what `eligibility` has not already cleared by the time a
    row reaches `unmarked` (CBN-157). `obvious` means every mirrored clause the
    predicate still applies came back clean; `needs_judgment` means at least one
    did not, or could not be determined — the same conservative bias the
    predicate itself declares: an uncertain clause is never `obvious`.
    A clause 3 keyword miss is not a clean clause (CBN-361): this reader
    returns `needs_judgment` for it and does not produce `obvious` from it.
    """

    OBVIOUS = "obvious"
    NEEDS_JUDGMENT = "needs-judgment"


class _Dto(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PlanTask(_Dto):
    task_id: str
    completed: bool
    priority: PlanPriority
    critical: bool = False
    model: str | None = None
    shortname: str | None = None
    description: str = ""
    # `[unattended]` marker present after `[model]` (CBN-EPIC-17.2).
    unattended: bool = False
    # `[handoff]` marker present in the same token run (CBN-136). Orthogonal to
    # `unattended`: that one answers "may this run headless", this one answers
    # "will this stop mid-run for a human act". A row may carry both, and the
    # `handoff` dimension denies anyway — see `eligibility`.
    handoff: bool = False
    # Every bracket token in the trailing run after `[model]`, verbatim (CBN-260).
    # `unattended` / `handoff` above are named reads of this list; the list itself
    # is what lets `eligibility` find `[xheavy]` / `[handoff]` wherever the operator
    # put them, rather than only in the slot the grammar happens to capture them in.
    extras: list[str] = []
    # Ids cited by `Blocked by [[ID]]` in the long description (CBN-116), the
    # same list the reference viz parser exposes as `Task.blockedBy`. Mirrors
    # the line rather than interpreting it; `eligibility` applies the gate.
    blocked_by: list[str] = []
    # Where the row lives and what it says, verbatim (CBN-119.3). The operator
    # seeds by editing this line, so a report that names only the id sends them
    # to search for it. `text` is the *original* markdown line, not the blanked
    # one `parse_plan` matched against — the row is theirs to edit as written.
    # Defaulted rather than required: a hand-built `PlanTask` in a test has no
    # line to point at, and 0 reads as "unlocated" everywhere it surfaces.
    line: int = 0
    text: str = ""


class OpenTasknote(_Dto):
    task_id: str
    path: str
    status: str
    # Verbatim `park-reason:` frontmatter value (`<code> — <prose>`), when the
    # producer wrote one. Split it with `split_park_reason`; this mirrors the
    # file rather than interpreting it.
    park_reason: str | None = None


class NextTask(_Dto):
    task_id: str
    plan_model: str | None = None
    shortname: str | None = None
    priority: PlanPriority
    critical: bool = False
    description: str = ""
    reason: str
    # Why this task was eligible for the pick that chose it, in the same
    # `<code> — <prose>` shape as `park-reason:` (CBN-EPIC-17.2). Optional for
    # DTO compatibility; `pick_next` always sets it.
    eligibility_reason: str | None = None
    tasknote: OpenTasknote | None = None


class Denial(_Dto):
    """A task the safety dimension excluded from an unattended pick.

    Carries what an operator needs to *act* on the row, not only to be told
    about it (CBN-119.3): where the line is, what it says now, and — when the
    denial is one seeding can fix — what it should say instead. `line` / `text`
    mirror `UnparsedRow`'s pairing, so both halves of the report point at a PLAN
    line the same way.
    """

    task_id: str
    reason: str
    # 1-based PLAN line and the raw row, from `PlanTask`. Defaulted for DTO
    # compatibility with records built before CBN-119.3; 0 reads as "unlocated".
    line: int = 0
    text: str = ""
    # The row with `[unattended]` correctly placed, ready to paste over `text`
    # — `None` when seeding is not the fix (a `blocked` or `exploratory` denial)
    # or when no safe insertion exists (`seeded_line`). Caobunga never applies
    # it: seeding stays a manual operator act (§"Marker coverage").
    seeded_line: str | None = None
    # A blocking mention `Blocked by [[ID]]` captured nothing from (CBN-128) —
    # the fragment from the mention to the end of the description, so a renderer
    # that elides it shows the clause rather than the decoration run. `None` when
    # the row carries a parsed clause (its `reason` already says `blocked`) or
    # says nothing about being blocked. Report-only: nothing denies on it.
    unparsed_blocker: str | None = None
    # Split of an `unmarked` denial (CBN-157). `obvious` is a row every mirrored
    # clause confirmed; `needs-judgment` is not. A clause 3 keyword miss is the
    # latter (CBN-361), so a real read does not assign `obvious`. `None` for
    # every other denial code — the predicate only addressed the marker
    # question. Derived per read from fields already on this record's
    # `PlanTask`, never persisted (docs/CONTRACT.md §"Marker coverage").
    triage: TriageClass | None = None
    # `<code> — <prose>` reason a `needs_judgment` triage names, same shape as
    # `park_reason` / `eligibility_reason`. `None` when `triage` is `obvious`
    # or absent.
    triage_reason: str | None = None


class UnparsedRow(_Dto):
    """An open-looking PLAN row that failed `TASK_LINE` (CBN-115).

    A checkbox line in a launch bucket that the grammar dropped — e.g. an
    `[!unattended]` marker, which matches no slot. It never became a
    `PlanTask`, so `Coverage.open` cannot count it; the id is unrecoverable for
    the same reason, which leaves line number + raw text as the only handle.
    """

    line: int
    text: str


class Coverage(_Dto):
    """`[unattended]` marker coverage over a project's open launch queue (CBN-EPIC-18.3).

    Computed **as-if-unattended on every read, whatever the project's tier** —
    the question it answers is "where does this repo still need markers seeded
    before anything can dispatch here", which every registered project has and
    a drained one has already answered. Scoping it to the launching tiers would
    blank every row a rollout wave just registered — and since CBN-118 made the
    marker a requirement of every launching tier, this report is no longer a
    forecast of a possible promotion but a direct read of what the project's
    own tier will dispatch.

    `open` is the **base-eligible** queue — the three dimensions `eligibility`
    checks before the safety one — so `marked + denied == open` always holds and
    a parent-epic tracking row never shows up as something to mark.
    """

    open: int = 0
    marked: int = 0
    denied: int = 0
    # The ids `marked` tallies, in the same dispatch order `denials` uses — the
    # detail half of the count (CBN-125). It exists because a *later* read needs
    # to know which rows were armed at an earlier one: `marker_drift` diffs
    # against a set recorded from here, and a bare count cannot say which row
    # lost its marker. Detail-only on the reports, exactly like `denials`.
    marked_tasks: list[str] = []
    # Open-looking launch-bucket rows the grammar dropped (CBN-115). Counted
    # beside `open`, never inside it — a row that failed to parse cannot honestly
    # join a queue of parsed tasks, and `marked + denied == open` stays intact.
    # Report-only in v1: nothing refuses a dispatch over it.
    unparsed: int = 0
    # Detail-only in the reports (§"Status API"): the aggregate surfaces render
    # the counts, the per-project ones name the ids an operator would seed —
    # and, for unparsed rows, the line an operator would fix. `denials` is in
    # dispatch order (CBN-119.3), so it reads as the order to seed in.
    denials: list[Denial] = []
    unparsed_rows: list[UnparsedRow] = []


class ProjectRead(_Dto):
    suggested_next: str | None = None
    tasks: list[PlanTask]
    open_tasknotes: list[OpenTasknote]
    candidate: NextTask | None = None
    # What the operator has open right now, independent of what the picker would
    # dispatch (CBN-EPIC-17.3). Always computed, on every read and every posture:
    # it is the *display* half of the in-progress override, and the dashboard's
    # only durable "current" signal (CBN-040, from CBN-011).
    in_flight: NextTask | None = None
    # Tasks in the open launch queue that this unattended pick refused — the
    # safety dimension (CBN-EPIC-17.2) or the `Blocked by` gate (CBN-116).
    # Always empty on an attended read; routine exclusions (completed / bucket
    # / parent-epic) are never listed here — they would flood the list with the
    # whole `## Completed` section.
    denied: list[Denial] = []
    # Marker coverage over the open queue (CBN-EPIC-18.3). Like `in_flight`, it
    # is computed on every read and every posture: it reports on the repo, not
    # on this caller's dispatch. `denied` above is posture-dependent and covers
    # only what *this* pick excluded; these two never merge.
    coverage: Coverage = Coverage()
