"""Operator-presence read: would a launch into this target be refused, and why.

One derivation with three consumers across three layers — the dispatcher refuses
on it (`loop._refuse_if_dirty`), and the two report surfaces render it
(`caobunga status`, the Status API). It lives in its own module because it
belongs to none of them: `close.py` inspects a *close*, and `flaitron.py` reads
producers and may hold no registry concept (§"Fleet boundary") — `tier` is one.

**Why the gate and the report must share it (CBN-130).** The tasknote exemption
is scoped to the candidate, so *which row is the candidate* decides whether an
existing open note counts as foreign dirt. Two implementations of that rule —
one in the gate, one in the report — is the report lying about the gate. So
`blocking` is derived once and the gate refuses on the same answer the report
prints.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from caobunga.errors import GitUnreadable
from caobunga.flaitron import (
    PLAN_REL,
    SIDEQUEST_DIR_REL,
    TASKNOTE_DIR_REL,
    NextTask,
    OpenTasknote,
    ProjectRead,
    pick_next,
)
from caobunga.git import git_porcelain
from caobunga.models import DRAIN_TIERS, LAUNCHING_TIERS, ExecutionTier

# The paths a dispatched worker writes when it *files* a discovery rather than
# doing the task: the PLAN line itself, a `--park` sidequest stub, or a tasknote.
# Everything a filing can touch lives under `.flaitron/`, which is what lets the
# refusal print one directory-scoped commit instead of a path list it might
# truncate (CBN-138.4).
_FILING_FILE = PLAN_REL.as_posix()
_FILING_DIRS = (f"{SIDEQUEST_DIR_REL.as_posix()}/", f"{TASKNOTE_DIR_REL.as_posix()}/")
# The directory that commit spans. Named here rather than spelled in the message
# so the command can never name a wider tree than the predicate checked.
FILING_ROOT = PLAN_REL.parent.as_posix()


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirtLine(_Dto):
    """One `git status --porcelain -uall` line, classified.

    `path` is the repo-relative path the line names, or `None` when this reader
    could not extract one — git C-quotes non-ASCII paths under the default
    `core.quotePath`, and other porcelain shapes exist. `task_id` is set only
    when `path` is exactly an open tasknote's path.

    An unclassified line is **blocking** (see `tree_state`). The parse failing
    open would silently exempt a file nobody explained, which inverts the
    bias-to-refuse CBN-EPIC-17.3 chose for the read itself.
    """

    text: str
    path: str | None = None
    task_id: str | None = None


class TreeState(_Dto):
    """Whether the target tree would refuse a launch, and what the operator can do.

    Three launch-refusal states, all stated — unreadable, blocking dirt, or
    clean — on CBN-125's rule: a guard that renders nothing when it cannot
    answer is indistinguishable from one that checked and found nothing.
    """

    readable: bool
    # The task this project's own tier would dispatch, and the open tasknote that
    # candidate exempts. `exempt_task` is `None` when the candidate carries no
    # open note, which is when an *unrelated* open note becomes foreign dirt.
    candidate: str | None = None
    exempt_task: str | None = None
    # Every uncommitted line, then the subset that would refuse the launch. Both
    # are carried: the difference between them *is* the exemption, and a reader
    # that only saw `blocking` could not show what the candidate bought.
    dirt: list[DirtLine] = []
    blocking: list[DirtLine] = []
    # The counterfactual candidate: when every blocking line belongs to one open
    # tasknote, dispatching *that* task would exempt them all and the gate would
    # read clean. `None` when any blocking line is not an open tasknote (no
    # candidate choice clears it) or when more than one task's notes are dirty.
    clean_for: str | None = None
    # Whether *every* blocking line is a path a worker writes when it files a
    # discovery (CBN-138.4). All-or-nothing on purpose: one line of operator dirt
    # makes "this is a worker's filing" the wrong thing to say, and the caller
    # falls back to the generic refusal. `False` on a clean or unreadable tree —
    # there is no blocking set to characterize.
    filing_only: bool = False

    @property
    def clean(self) -> bool:
        return self.readable and not self.blocking


def launch_pick(read: ProjectRead, tier: ExecutionTier) -> NextTask | None:
    """The candidate this project's *own tier* would dispatch.

    Not `read.candidate`: the report surfaces read attended (`try_read_project`),
    and both posture dimensions move the answer. `unattended` drops unmarked rows
    (CBN-118), and `drain` drops the in-progress override (CBN-EPIC-17.3) — which
    is the dimension that makes the coupling visible, because under a drain the
    operator's own open note stops being the candidate and starts being dirt.

    `observe` picks as `gated` would. It never dispatches, so the answer is
    informational — reported for the same reason coverage is (§"Marker
    coverage"): the report is about the repo, and blanking the tier that has not
    been promoted yet blanks exactly the rows a promotion wave needs to see.
    """
    return pick_next(
        read.tasks,
        read.open_tasknotes,
        unattended=tier in LAUNCHING_TIERS,
        drain=tier in DRAIN_TIERS,
    )


def read_tree(read: ProjectRead, root: Path | str, tier: ExecutionTier) -> TreeState:
    """`launch_pick` + `tree_state` for a project already parsed once.

    The composition both report surfaces want, in one place so `caobunga status`
    and the Status API cannot render two different verdicts. It takes the
    `ProjectRead` rather than re-parsing, which is the same one-read-per-request
    discipline `current` and `coverage` already share (§"Status API").

    A hung or missing `git` is `readable=False` here, never a raise: the launch
    gate refuses on it (CBN-317), a report only states it.
    """
    candidate = launch_pick(read, tier)
    try:
        return tree_state(Path(root), candidate, read.open_tasknotes)
    except GitUnreadable:
        return TreeState(readable=False, candidate=_task_id(candidate))


def _porcelain_path(line: str) -> str | None:
    """The repo-relative path a porcelain line names, or `None` if unrecoverable.

    Porcelain v1 is `XY<space>PATH`, with a rename carrying `ORIG -> PATH`. A
    path containing a character git considers unusual is C-quoted, and this
    reader does not unquote: an unrecoverable path classifies as foreign dirt,
    which is the safe direction. Tasknote paths are `<ID>.md` under
    `.flaitron/tasknote/` and are never the quoted case.
    """
    if len(line) < 4:
        return None
    path = line[3:]
    _, arrow, renamed = path.partition(" -> ")
    if arrow:
        path = renamed
    if path.startswith('"'):
        return None
    return path or None


def tree_state(
    root: Path,
    candidate: NextTask | None,
    notes: list[OpenTasknote],
) -> TreeState:
    """Read the target tree once and classify what a launch would find.

    One path is exempt, by **matching the parsed porcelain path**, not by
    excluding it from the git call, so this one read answers both "would it
    refuse" and "which lines survive" — the pair the report needs.

    The candidate's own tasknote: flaitron commits an active note only at Phase
    4, inside the atomic closure commit, so a stranded `in-progress` note is
    *always* uncommitted and treating it as foreign would refuse every CBN-109
    resume (CBN-EPIC-17.3). Nothing else is exempt — since CBN-188.2 Caobunga
    writes no file of its own into a target repo, so a leftover
    `caobunga-status.md` from before that move is ordinary dirt (CBN-188.3).

    `git_porcelain`'s `-uall` stays load-bearing: a collapsed `?? .flaitron/tasknote/`
    line names a directory, matches no note's path, and would classify the
    exempt note as foreign dirt — the same refusal CBN-EPIC-17.3 hit through the
    pathspec door.

    Raises `GitUnreadable` when `git` hung or is missing (CBN-317), so the gate
    refuses exit 5; `readable=False` is git answering "no" (nonzero exit).
    """
    lines = git_porcelain(root)
    if lines is None:
        return TreeState(readable=False, candidate=_task_id(candidate))
    by_path = {note.path: note.task_id for note in notes}
    dirt = [_classify(line, by_path) for line in lines]
    exempt = candidate.tasknote.task_id if candidate and candidate.tasknote else None
    blocking = [entry for entry in dirt if entry.task_id is None or entry.task_id != exempt]
    return TreeState(
        readable=True,
        candidate=_task_id(candidate),
        exempt_task=exempt,
        dirt=dirt,
        blocking=blocking,
        clean_for=_clean_for(blocking),
        filing_only=bool(blocking) and all(_is_filing(entry) for entry in blocking),
    )


def _task_id(candidate: NextTask | None) -> str | None:
    return candidate.task_id if candidate else None


def _classify(line: str, by_path: dict[str, str]) -> DirtLine:
    path = _porcelain_path(line)
    return DirtLine(text=line, path=path, task_id=by_path.get(path) if path else None)


def _is_filing(entry: DirtLine) -> bool:
    """Is this blocking line a path a worker writes when it files a discovery?

    Path shape only, and deliberately so: reading the file to confirm a worker
    wrote it would be Caobunga judging a producer's content (§"Fleet boundary"),
    and the claim the message makes is milder than authorship — this is dirt whose
    *shape* is a filing, which is what makes naming one worth saying. An
    unrecoverable path (`path is None`, git's C-quoting) is never a filing, so the
    bias-to-refuse CBN-EPIC-17.3 chose for the read survives into the message.

    The prefix test lands correctly on both sides of the collapsed-directory case
    CBN-130 recorded: `?? .flaitron/sidequest/` names a directory, matches the
    prefix, and is a filing; a collapsed `?? .flaitron/` matches neither the prefix
    nor the exact PLAN path, and drags the whole verdict back to the generic
    message. No special case needed in either direction.
    """
    if entry.path is None:
        return False
    return entry.path == _FILING_FILE or entry.path.startswith(_FILING_DIRS)


def _clean_for(blocking: list[DirtLine]) -> str | None:
    """The one candidate whose exemption would clear every blocking line.

    A statement about the gate, not about eligibility: it says which dispatch the
    tree reads clean for, not that the row is dispatchable. Answering it needs no
    second git read — the classification already holds every fact.
    """
    if not blocking:
        return None
    owners = {entry.task_id for entry in blocking}
    if len(owners) != 1 or None in owners:
        return None
    return owners.pop()
