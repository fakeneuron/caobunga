"""The text one invocation prints: observe, eligibility, and exit-detail lines.

`caobunga.run_echo` owns the formatters `loop.py` composes into terminal output
and stop details: the observe-tier dump (`echo_observe`, the one helper that
reads — `store.last_run` and the `probe_fitness` subprocess), the per-pick
`eligibility:` and `unparsed:` echoes (`echo_eligibility`, `echo_unparsed`), the
exit-3 message (`no_task_message`), the denial list both share (`denial_codes`),
and the stop details (`unpinned_exit_detail`, `outcome_exit_detail`,
`suite_exit_detail`, `run_tally`). None of them decides anything: the stop
classifiers and every `drain:` reason literal stay in `loop.py`, where
`test_contract`'s drain guard source-reads them. A leaf module: the flaitron
façade, `models`, `store`, and `suite_verbs` cross in, and nothing calls back
into `loop.py`. Split out of `loop.py` at CBN-EPIC-363.5.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from caobunga.flaitron import (
    LAUNCH_BUCKETS,
    Denial,
    NextTask,
    ProjectRead,
    UnparsedRow,
    completed_task_ids,
    eligibility,
    open_task_ids,
    split_park_reason,
)
from caobunga.models import RegistryRecord, RunRecord, SuiteVerb
from caobunga.store import Store
from caobunga.suite_verbs import probe_fitness


def echo_observe(
    record: RegistryRecord,
    project: ProjectRead,
    store: Store,
    echo: Callable[[str], None] | None,
) -> None:
    if echo is None:
        return
    last = store.last_run(record.id)
    next_id = project.candidate.task_id if project.candidate else "(none)"
    last_task = last.task_id if last else "(none)"
    echo(f"id: {record.id}")
    echo(f"tier: {record.tier.value}")
    echo(f"enabled: {str(record.enabled).lower()}")
    echo(f"last: {last_task}")
    echo(f"next: {next_id}")
    echo(probe_fitness(Path(record.path)).render())
    # Per-task as-if-unattended eligibility over the open queue (CBN-EPIC-17.2):
    # what a launching tier would decide, reported without launching.
    # Routine exclusions (completed rows, non-launch buckets) are omitted — the
    # queue, not the whole PLAN.
    done = completed_task_ids(project.tasks)
    still_open = open_task_ids(project.tasks)
    notes = {note.task_id: note for note in project.open_tasknotes}
    for task in project.tasks:
        if task.completed or task.priority.value not in LAUNCH_BUCKETS:
            continue
        allow, reason = eligibility(
            task,
            unattended=True,
            completed_ids=done,
            note=notes.get(task.task_id),
            open_ids=still_open,
        )
        verdict = "allow" if allow else "deny"
        echo(f"eligibility: {task.task_id} {verdict}: {reason}")


def echo_eligibility(read: ProjectRead, echo: Callable[[str], None] | None) -> None:
    """One line per headless pick decision — allow and deny alike (CBN-EPIC-17.2).

    Fires on every launching tier since CBN-118, `gated` included, so a refusal
    the operator did not expect always names the row and the reason.
    """
    if echo is None:
        return
    for denial in read.denied:
        echo(f"eligibility: {denial.task_id} deny: {denial.reason}")
    candidate = read.candidate
    if candidate is not None and candidate.eligibility_reason is not None:
        echo(f"eligibility: {candidate.task_id} allow: {candidate.eligibility_reason}")


def echo_unparsed(read: ProjectRead, echo: Callable[[str], None] | None) -> None:
    """One line per PLAN row the grammar dropped — report-only (CBN-119.2).

    Its own helper rather than a branch of `echo_eligibility`: that one prints
    per *pick decision*, and an unparsed row never became a `PlanTask`, so it is
    not a decision at all. It is the queue member the reader could not see.

    **Nothing refuses over a nonzero count**, and `CBN-119.2` re-decided that
    deliberately rather than inheriting it (`docs/CONTRACT.md` §"Marker
    coverage"). A dropped row causes *under*-dispatch, never an unsafe one, so
    the bias-to-refuse `eligibility` applies to the marker does not transfer;
    the row carries no id by definition, so a refusal could not be row-scoped
    the way every other denial here is; and `CHECKBOX_ROW` is loose enough that
    a stray prose checkbox would wedge a whole repo's drain, clearable only by
    committing in someone else's tree.

    What it *does* fix is that `loop.py` never read `coverage` at all: an
    operator who seeded a marker into a shape the grammar still rejects (e.g.
    `[!unattended]`) and ran straight into a drain got no signal that the seed
    had deleted the row. Fires on every launching tier, beside the eligibility
    echoes, so the two halves of "what this dispatch could see" are read
    together.
    """
    if echo is None:
        return
    for row in read.coverage.unparsed_rows:
        echo(f"unparsed: line {row.line} — {row.text}")


def denial_codes(denials: Sequence[Denial]) -> str:
    """`id (code)` list. Shared by the exit-3 message and the drain-exit echo.

    Both answer the same operator question — which rows the headless pick refused
    — so they render it the same way rather than drifting into two formats.
    """
    return ", ".join(
        f"{denial.task_id} ({split_park_reason(denial.reason)[0]})" for denial in denials
    )


def unpinned_exit_detail(candidate: NextTask) -> str:
    return (
        f"{candidate.task_id} not dispatchable on the drain's first read; "
        "not dispatched — check who changed it before re-running"
    )


def outcome_exit_detail(last: RunRecord) -> str:
    """`<id> outcome <value>`, plus the park code when the stop was a park."""
    detail = f"{last.task_id} outcome {last.outcome.value}"
    return f"{detail} ({last.park_reason})" if last.park_reason else detail


def run_tally(runs: Sequence[RunRecord]) -> str:
    """`<id> <outcome>[ <sha8>][ (<park reason>)]` per task the invocation ran, in order."""
    return ", ".join(
        " ".join(
            part
            for part in (
                run.task_id,
                run.outcome.value,
                run.commit_sha[:8] if run.commit_sha else None,
                f"({run.park_reason})" if run.park_reason else None,
            )
            if part
        )
        for run in runs
    )


def suite_exit_detail(last: RunRecord) -> str:
    """`<id> suite <verdict> — <verb> exit <n>[, …]`: every verb that was not green."""
    assert last.suite is not None  # noqa: S101 — `suite_stops` gated this call
    failed = ", ".join(_verb_word(verb) for verb in last.suite.verbs if not _verb_green(verb))
    return f"{last.task_id} suite {last.suite.verdict.value} — {failed}"


def _verb_green(verb: SuiteVerb) -> bool:
    return verb.exit_code == 0


def _verb_word(verb: SuiteVerb) -> str:
    if verb.exit_code is not None:
        return f"{verb.verb} exit {verb.exit_code}"
    if verb.timed_out:
        return f"{verb.verb} timeout"
    return f"{verb.verb} refused: {verb.refused}"


def no_task_message(project_id: str, denied: list[Denial], unparsed: list[UnparsedRow]) -> str:
    """Exit-3 message; names every safety denial so an empty drain is legible.

    `unparsed` rides on the same line (CBN-119.2). "Nothing to run" is exactly
    where a dropped row misleads most: without it the message names the rows
    that *survived* an operator's botched seed and stays silent about the one
    they were trying to seed. Line numbers, not ids — an unparsed row has no
    parsed id, which is what makes it unparsed.
    """
    base = f"no next task for {project_id}"
    if denied:
        base = (
            f"{base}; {len(denied)} task(s) denied under headless dispatch: {denial_codes(denied)}"
        )
    if unparsed:
        lines = ", ".join(str(row.line) for row in unparsed)
        base = f"{base}; {len(unparsed)} unparsed row(s) — see line {lines}"
    return base
