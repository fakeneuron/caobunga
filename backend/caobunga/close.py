"""Classify a Flaitron close from the worker repo (PLAN + archive + git)."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from caobunga.errors import GitUnreadable, HeadLost, UserError
from caobunga.flaitron import (
    PLAN_REL,
    TASK_LINE,
    NextTask,
    OpenTasknote,
    PlanTask,
    ProjectRead,
    dispatchable,
    external_review_box,
    extract_follow_ups,
    extract_summary,
    is_descoped,
    parse_plan,
    read_project,
    split_park_reason,
)
from caobunga.git import (
    find_archived_tasknote_at_head,
    git_head,
    git_show,
    list_commits_touching,
    list_foreign_commits,
    reachable_from_head,
    sole_path_patch,
    tree_after,
)
from caobunga.models import (
    CLOSURE_OUTCOMES,
    Escalation,
    ExternalReview,
    ForeignCommit,
    NewRow,
    Outcome,
    Suite,
)
from caobunga.text import read_text_or_none


class CloseSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    outcome: Outcome
    commit_sha: str | None = None
    tasknote_path: str | None = None
    summary: str
    next_task: str | None = None
    escalation: Escalation | None = None
    park_reason: str | None = None
    # Follow-up lines the closing worker proposed (CBN-121). `None` when the note
    # carried no follow-up section, or carried one with nothing in it — a section
    # reading "**None.**" is saying there is nothing to file.
    follow_ups: list[str] | None = None
    # PLAN rows that appeared while the run was in flight (CBN-138.3). `None`
    # when nothing appeared, or when the caller passed no launch baseline to
    # diff against — an absent baseline is "not measured", not "nothing new".
    new_rows: list[NewRow] | None = None
    # Where this run's worker stdout/stderr was teed (CBN-162), when the adapter
    # wrote one. A path, never contents — the deny-list forbids the transcript
    # itself, not an operator's pointer to it. `None` when no log was written
    # (a `local` run that never reached `send`, or an adapter that keeps no tee).
    log_path: str | None = None
    # Commits in `start_sha..HEAD` this run does not itself account for (CBN-172).
    # `None` when nothing else landed in the window, or when `start_sha` was
    # unavailable to compare against.
    foreign_commits: list[ForeignCommit] | None = None
    # Window commits that only filed rows `new_rows` already reports (CBN-299).
    # Snapshot-only, like `log_path`'s absence from the record: `new_rows` is the
    # account the record keeps, so these ride to the loop's `filed:` echo and no
    # further. `None` when no window commit was a pure filing.
    filed_commits: list[ForeignCommit] | None = None
    # The loop-side deterministic suite's result on this close (CBN-EPIC-202.3).
    # Attached by the loop after the worker settles, the way `log_path` is —
    # `inspect_close` never sets it, since the suite runs *after* close
    # detection. `None` when nothing ran: a park or a failure has no closed
    # tree to measure, and the record model forbids the field there.
    suite: Suite | None = None
    # How the archived note's External review box closed (CBN-257). Set only on
    # the archived branch — a park or a `no-tasknote` close has no archived note
    # to read, and `None` there is "not read", never `missing`.
    external_review: ExternalReview | None = None


def parked_snapshot(
    note: OpenTasknote,
    *,
    lede: str,
    escalation: Escalation,
    commit_sha: str | None = None,
    rows: list[NewRow] | None = None,
    follow_ups: list[str] | None = None,
    foreign_commits: list[ForeignCommit] | None = None,
    filed_commits: list[ForeignCommit] | None = None,
) -> CloseSnapshot:
    """Blocked-outcome snapshot for a parked tasknote.

    Used by this module's mid-run classification of a note that came back
    `blocked`. It was shared with `loop`'s refusal to launch an already-`blocked`
    note until CBN-252 moved that case into the pick as the `parked` denial, so
    flaitron's `<code> — <prose>` grammar is still read in exactly one place.
    The code is the machine-readable half and the prose folds into `summary`,
    the same division `docs/CONTRACT.md` §"Escalation reasons" already draws.

    `follow_ups` is passed in rather than read here (CBN-138.4). Only one of the
    two call sites should carry them, and which one is a *posture* question this
    function has no access to — see `read_parked_follow_ups`.
    """
    code, prose = split_park_reason(note.park_reason)
    return CloseSnapshot(
        outcome=Outcome.BLOCKED,
        commit_sha=commit_sha,
        tasknote_path=note.path,
        summary=f"{lede}: {prose}" if prose else lede,
        escalation=escalation,
        park_reason=code,
        new_rows=rows,
        follow_ups=follow_ups,
        foreign_commits=foreign_commits,
        filed_commits=filed_commits,
    )


def read_parked_follow_ups(root: Path, note: OpenTasknote) -> list[str] | None:
    """Follow-ups from a note that parked rather than closing (CBN-138.4).

    CBN-121 read only the *archived* note, and recorded the exclusion as
    deliberate: a parked note's follow-ups are still in the operator's working
    tree, which is not the surface that swallows them. That rationale is sound at
    `gated`, where the operator reads the recap and walks into the repo — and it
    is exactly wrong under a drain, where nobody is watching and the park is just
    a run that ended without an archive. Same swallow, different door.

    So the caller decides on posture and this reads the *open* note in place. The
    file is by definition uncommitted, and a park is already the unhappy path, so
    an unreadable note degrades to `None` rather than raising: the snapshot's job
    is to record what stopped the run, and losing the whole escalation over a
    narrative field would be the worse trade.
    """
    text = read_text_or_none(root / note.path)
    if text is None:
        return None
    return extract_follow_ups(text)


def filed_row_ids(root: Path, sha: str) -> frozenset[str] | None:
    """The task ids `sha` adds to PLAN, when adding rows is *all* it does (CBN-299).

    A worker's own `/ft-file-task` filing is one commit that touches
    `PLAN.md` and nothing else, and only adds lines. `None` for any other shape —
    a second path, a removed or edited line, an added line that is not a task
    row, or a read git could not answer — so the caller falls back to calling the
    commit foreign, the posture CBN-172 set for everything this cannot positively
    place. The one tolerated removal is a section's `(none)` placeholder (or a
    blank line), which flaitron's own filing drops when it fills an empty section
    (`SPEC/plan-filing.md` §"Empty-section placeholder").
    """
    patch = sole_path_patch(root, sha, PLAN_REL.as_posix())
    if patch is None:
        return None
    ids: set[str] = set()
    in_hunk = False
    for line in patch:
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("-"):
            if line[1:].strip() not in ("", "(none)"):
                return None
            continue
        if not line.startswith("+") or not line[1:].strip():
            continue
        match = TASK_LINE.match(line[1:])
        if match is None:
            return None
        ids.add(match.group(2))
    return frozenset(ids) or None


def split_window_commits(
    root: Path,
    start_sha: str,
    head: str,
    rows: list[NewRow] | None,
    *,
    exclude: str | None,
) -> tuple[list[ForeignCommit] | None, list[ForeignCommit] | None]:
    """`(foreign, filed)` for `start_sha..head` (CBN-172, CBN-299).

    A window commit that only adds PLAN rows, every one of them in `rows`, is a
    filing this run's record already accounts for through `new_rows` — usually
    the worker's own `/ft-file-task`. It leaves `foreign` and rides `filed`
    instead. Anything else, including a filing `rows` does not name or any
    window with no baseline to compare against, stays foreign.
    """
    window = list_foreign_commits(root, start_sha, head, exclude=exclude)
    if window is None or not rows:
        return window, None
    reported = frozenset(row.task_id for row in rows)
    foreign: list[ForeignCommit] = []
    filed: list[ForeignCommit] = []
    for commit in window:
        ids = filed_row_ids(root, commit.sha)
        (filed if ids is not None and ids <= reported else foreign).append(commit)
    return foreign or None, filed or None


def diff_new_rows(tasks: list[PlanTask], baseline: Collection[str] | None) -> list[NewRow] | None:
    """PLAN rows present now that `baseline` did not carry (CBN-138.3).

    The other half of the loop CBN-138.2 opened: a worker cued to file a follow-up
    files it into the target's own PLAN, and until now no caobunga surface said a
    row had appeared. `baseline` is the id set the run's pre-flight read saw, so
    the diff is against the queue the candidate was picked from rather than against
    a second read that could disagree with it.

    **Every parsed row counts, not just the open launch queue.** `marker_drift`
    scopes to that queue because scoping *removes* false positives there; scoping
    here would *hide* a true one — a row filed into `## Future Opportunities` is
    still a discovery, and dropping it silently is the blind-spot class CBN-115,
    CBN-125 and CBN-EPIC-17.2 each closed. State it, refuse nothing.

    Ordered by `parse_plan`'s document order rather than dispatch order: these are
    rows to *read*, not a worklist to seed down, and the order a worker filed them
    in is the order its own reasoning ran.

    `None` for an absent or empty baseline — not measured, as distinct from
    measured-and-empty, the same distinction `marked_tasks` draws.
    """
    if not baseline:
        return None
    known = frozenset(baseline)
    found = [
        NewRow(task_id=task.task_id, unattended=task.unattended)
        for task in tasks
        if task.task_id not in known
    ]
    return found or None


def inspect_close(
    root: Path,
    task_id: str,
    start_sha: str,
    *,
    unattended: bool = False,
    drain: bool = False,
    baseline: Collection[str] | None = None,
    strict_attribution: bool = False,
) -> CloseSnapshot | None:
    """Return a snapshot when this task has closed; None if still running.

    `unattended` / `drain` are the *dispatch* posture of the run being classified
    — the run loop's tier, not the target's `--unattended` capability (CBN-117).
    Close detection itself stays posture-blind; what the posture reaches is the
    two claims that are only true relative to a dispatch. `next_task` is one: the
    record's forward-looking claim must be read the way the next dispatch will
    read it, and since CBN-118 that means the full posture rather than the marker
    dimension alone. A parked note's `follow_ups` is the other, `drain` only —
    CBN-121's reason for skipping a park (its follow-ups are still in front of the
    operator) is a claim about somebody being there, and under a drain nobody is
    (CBN-138.4).

    `baseline` is the task-id set the run's pre-flight read saw (CBN-138.3). It is
    passed in rather than re-read for the same reason `marker_drift`'s is: the diff
    must be against the queue the candidate was picked from. Every snapshot this
    function builds carries the result, a park included — a worker that filed a row
    and then parked still filed it.

    `strict_attribution` (CBN-357) is the re-read's: a close git cannot attribute
    returns `None` rather than naming `head`. Off, a closure is recorded against
    `head` so the record always carries a commit; on, the caller keeps the
    snapshot it already has.
    """
    project = read_project(root, unattended=unattended, drain=drain)
    rows = diff_new_rows(project.tasks, baseline)
    open_notes = {note.task_id: note for note in project.open_tasknotes}
    open_note = open_notes.get(task_id)
    if open_note is not None and open_note.status == "blocked":
        return _parked_close(root, open_note, start_sha, rows, drain=drain)

    head = git_head(root)
    if head is None:
        raise HeadLost(f"HEAD unreadable after worker launch: {root}")
    if head == start_sha:
        return None

    # Read from HEAD's committed tree, not the checkout (CBN-158): a mid-task
    # commit (e.g. a `/ft-file-task` filing) already moves `head` off
    # `start_sha` while the actual close is still mid-flight on disk — flip
    # written, archive move not yet written, or vice versa. `git show` /
    # `git ls-tree` only ever see the last commit's tree, so the two writes
    # this reads (PLAN flip, archive move) are only ever observed together,
    # exactly as the paper-complete guard's atomic single commit requires.
    task = _plan_row(git_show(root, PLAN_REL.as_posix()), task_id)
    if task is None or not task.completed:
        return None

    archived = find_archived_tasknote_at_head(root, task_id)
    if open_note is not None and archived is None:
        return None

    close_sha = _close_commit(root, task_id, start_sha, head, archived)
    if close_sha is None:
        if strict_attribution:
            return None
        close_sha = head
    foreign, filed = split_window_commits(root, start_sha, head, rows, exclude=close_sha)
    next_task = _next_after(project, task_id, unattended=unattended)
    if archived is not None:
        shown = git_show(root, archived)
        text = shown or ""
        summary = extract_summary(text) or task.description or f"Closed {task_id}."
        summary = f"{summary} — {tree_after(root, task_id)}"
        outcome = Outcome.DE_SCOPED if is_descoped(text) else Outcome.CLOSED
        # An unreadable note is "not read", never `missing` (CBN-257). A
        # de-scope has no diff to grade, so an unticked box there is `na`.
        review = None if shown is None else ExternalReview(external_review_box(text))
        if outcome is Outcome.DE_SCOPED and review is ExternalReview.MISSING:
            review = ExternalReview.NA
        return CloseSnapshot(
            outcome=outcome,
            commit_sha=close_sha,
            tasknote_path=archived,
            summary=summary,
            next_task=next_task,
            follow_ups=extract_follow_ups(text),
            new_rows=rows,
            foreign_commits=foreign,
            filed_commits=filed,
            external_review=review,
        )
    summary = task.description or f"PLAN closed {task_id} with no tasknote."
    return CloseSnapshot(
        outcome=Outcome.NO_TASKNOTE,
        commit_sha=close_sha,
        summary=summary,
        next_task=next_task,
        new_rows=rows,
        foreign_commits=foreign,
        filed_commits=filed,
    )


def _parked_close(
    root: Path,
    note: OpenTasknote,
    start_sha: str,
    rows: list[NewRow] | None,
    *,
    drain: bool,
) -> CloseSnapshot:
    """`inspect_close`'s verdict on a note that came back `blocked`.

    A parked note is the verdict on its own: a HEAD git cannot read
    (`GitUnreadable`) or cannot name (`None`) only leaves its commit unattributed
    (CBN-269), where the closure ladder raises `GitUnreadable` / `HeadLost`.
    """
    try:
        head = git_head(root)
    except GitUnreadable:
        head = None
    sha = head if head and head != start_sha else None
    foreign, filed = (
        split_window_commits(root, start_sha, head, rows, exclude=sha) if head else (None, None)
    )
    return parked_snapshot(
        note,
        lede="tasknote parked (status: blocked)",
        escalation=Escalation.WORKER_PARKED,
        commit_sha=sha,
        rows=rows,
        follow_ups=read_parked_follow_ups(root, note) if drain else None,
        foreign_commits=foreign,
        filed_commits=filed,
    )


def _plan_row(plan: str | None, task_id: str) -> PlanTask | None:
    """`task_id`'s row in `plan` text; `None` when the text or the row is absent."""
    if plan is None:
        return None
    return {t.task_id: t for t in parse_plan(plan)}.get(task_id)


def _close_commit(
    root: Path, task_id: str, start_sha: str, head: str, archived: str | None
) -> str | None:
    """The commit in `start_sha..head` that closed `task_id` (CBN-279).

    `None` when git cannot say — the range will not list, or no commit in it
    reads closed. The caller chooses the fall-back (CBN-357): `head` is a
    guess, and `inspect_close` is the one that decides whether to make it.

    A commit is closed when its PLAN reads the row completed and, if HEAD has an
    archived note, that note is in its tree too — the pair the paper-complete
    guard lands together. Only commits touching either path are walked, oldest
    first, and the close is the first commit of the final closed run: a flip
    later reopened does not count, and a later filing that also touches PLAN
    still reads closed, so it stays stacked work. HEAD is not the close when
    work stacked on it: an amend-then-stack the settle re-read sees, or a commit
    landed before the first poll. Classification still reads HEAD's tree; only
    attribution walks the range.
    """
    paths = [PLAN_REL.as_posix(), *([archived] if archived else [])]
    close: str | None = None
    for sha in list_commits_touching(root, start_sha, head, *paths) or []:
        task = _plan_row(git_show(root, PLAN_REL.as_posix(), rev=sha), task_id)
        closed = task is not None and task.completed
        if closed and archived:
            closed = git_show(root, archived, rev=sha) is not None
        if not closed:
            close = None
        elif close is None:
            close = sha
    return close


def reinspect_settled(
    root: Path,
    task_id: str,
    start_sha: str,
    snap: CloseSnapshot,
    *,
    unattended: bool = False,
    drain: bool = False,
    baseline: Collection[str] | None = None,
    on_branch: bool = False,
) -> CloseSnapshot:
    """Re-read a closure once its worker has settled (CBN-275).

    `wait_for_close` keeps the first poll that sees the close, and the worker
    keeps running after it. One that amends its close commit in that window
    leaves the detected sha off-branch: a fleet repo's FE-038 recorded `f85eb38` while
    the branch carried `f38218b`. So when the detected commit is no longer
    reachable from HEAD, classify again and keep the result if it is still a
    closure — commit, tasknote, and summary all from the final tree.

    Only an unreachable commit re-reads. A commit stacked *on* the close leaves
    it on-branch, and re-attributing to that later commit would list the real
    close as foreign. An amend *and* a stacked commit still re-read, and
    `inspect_close` attributes the amended close, not HEAD, so the stacked
    commit lands in `foreign_commits` (CBN-279). Every "could not say" — git
    unreadable, or a re-read that no longer classifies as a closure — keeps the
    first snapshot: this refines a detected close, never loses it.

    `on_branch` is the post-suite pass (CBN-356): the worker is stopped, so the
    re-read is final, and it also runs when the close is still on-branch — a
    commit stacked during the suite (a fleet repo's rotation) then lands in
    `foreign_commits`. A reachable close is the close, so that re-read is
    strict (CBN-357): one git cannot attribute keeps the first snapshot rather
    than falling back to HEAD, while a re-read that names another commit is a
    real re-attribution and is adopted. The `suite` measured before this pass
    rides onto the fresh snapshot.
    """
    if snap.outcome not in CLOSURE_OUTCOMES or snap.commit_sha is None:
        return snap
    try:
        reachable = reachable_from_head(root, snap.commit_sha)
        if reachable is None or (reachable and not on_branch):
            return snap
        fresh = inspect_close(
            root,
            task_id,
            start_sha,
            unattended=unattended,
            drain=drain,
            baseline=baseline,
            strict_attribution=reachable,
        )
    except (OSError, GitUnreadable, UserError):
        return snap
    if fresh is None or fresh.outcome not in CLOSURE_OUTCOMES:
        return snap
    return fresh.model_copy(update={"log_path": snap.log_path, "suite": snap.suite})


def _next_after(
    project: ProjectRead,
    closed_id: str,
    *,
    unattended: bool = False,
) -> str | None:
    """The id this project runs next, read at the posture that produced the run.

    `suggested_next` is prose the closing worker wrote, and `docs/CONTRACT.md`
    §"Flaitron reader" calls it "not the selector (the line can go stale)" — so
    it is validated rather than preferred: kept when `dispatchable` says this
    posture would run it, since it then carries context the picker's tie-breaks
    do not, otherwise fallen through to `candidate`, which is already
    posture-correct because `read_project` was given the posture.

    The check runs at **every** posture. CBN-117 gated it on `unattended` —
    flaitron's own authorship of that line consults no marker (`[unattended]`
    is captured grammar since v5.22.0 but carries no meaning flaitron assigns
    itself), so under a drain the line can only be an *attended*
    recommendation, and preferring it unchecked is how a drain came to
    publish a `next:` it had denied in the same invocation (CBN-EPIC-18.4
    F4). The attended path was left byte-identical as
    behavior preservation, and the header then went stale on a completed row
    (CBN-151): an attended `gated` close would have rendered that id into the
    committed `caobunga-status.md`, the same claim one door over. Attended
    validation consults nothing marker-shaped — `eligibility` at
    `unattended=False` is the posture-blind membership and `Blocked by`
    dimensions only — so it drops exactly what an attended pick would refuse,
    and nothing an operator would have run.
    """
    suggested = project.suggested_next
    if suggested and suggested != closed_id:
        if dispatchable(
            project.tasks, suggested, unattended=unattended, notes=project.open_tasknotes
        ):
            return suggested
    candidate: NextTask | None = project.candidate
    if candidate is not None and candidate.task_id != closed_id:
        return candidate.task_id
    return None
