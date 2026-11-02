"""Daily fleet digest (CBN-032). Folds run records, not tasknote phase logs."""

from __future__ import annotations

from datetime import date, datetime

from caobunga.invocation import running_verdicts, stale_receipts
from caobunga.models import (
    ExternalReview,
    InvocationResult,
    NewRow,
    RegistryRecord,
    RetainedWorktree,
    RunRecord,
    StaleReceipt,
    SuiteVerdict,
    iso_z,
    utc_now,
)
from caobunga.retained import report_retained, retained_worktrees, unlanded
from caobunga.status_render import landed_line, retained_state
from caobunga.store import Store


def local_day(value: datetime) -> date:
    """Calendar day in the operator's local zone (CBN-EPIC-95.4).

    Run records store UTC; only the bucketing is local. A UTC-day window left
    the digest structurally blank through a UTC−4 operator's whole evening —
    the hours they would actually read it (CBN-EPIC-95.3 F1).
    """  # noqa: RUF002
    return value.astimezone().date()


def runs_on_day(runs: list[RunRecord], day: date) -> list[RunRecord]:
    return [run for run in runs if local_day(run.closed_at) == day]


def run_lines(run: RunRecord) -> list[str]:
    """One run's digest bullet, plus what it proposed and what it filed.

    The follow-up block is the whole point of surfacing them here (CBN-121): a
    drain closes tasks with nobody reading the transcript, so the digest is the
    first place an operator meets what those runs discovered. Runs that proposed
    nothing render exactly as they did before — one bullet, no empty heading.

    The new-row block is the same argument one step later (CBN-138.3). A proposal
    is what the worker *said*; a new row is what it actually wrote into the target
    PLAN, and that row cannot dispatch until someone seeds `[unattended]` on it.
    Both blocks are omitted whole when empty, so a quiet run's bullet is unchanged.
    Proposals come first: they read as the reasoning, the rows as its result.

    The Hermes line closes the bullet (CBN-148): what the worker said and wrote
    come first, the reviewer's judgment of it last. Same omit-when-absent rule.
    Only records from before CBN-258 carry it; the loop no longer writes it.

    The task-id line itself, and the why of each tag on it, is `run_bullet`.
    """
    lines = [run_bullet(run)]
    if run.follow_ups:
        lines.append(f"  - **follow-ups proposed ({len(run.follow_ups)}):**")
        lines.extend(f"    - {line}" for line in run.follow_ups)
    if run.new_rows:
        unmarked = sum(1 for row in run.new_rows if not row.unattended)
        lines.append(f"  - **new rows ({len(run.new_rows)}, {unmarked} unmarked):**")
        lines.extend(f"    - {_row_line(row)}" for row in run.new_rows)
    if run.hermes_verdict:
        lines.append(f"  - **hermes:** {run.hermes_verdict}")
    return lines


def run_bullet(run: RunRecord) -> str:
    """The head line of `run_lines`: id, outcome, anomaly tags, summary.

    Public so the epic rollup renders its runs with the same tags (CBN-402)
    instead of a tagless copy that drifts each time the digest grows one.

    The verdict code (CBN-160) rides the task-id tag rather than the Hermes
    line: it is a fleet-scannable count/sort key, so it belongs where a reader
    scans task-by-task, not buried after the summary alongside the prose.

    The park reason (CBN-187) rides the same line for the same reason: a
    `blocked` outcome alone does not say why the run stopped, so a
    `visual-confirm` park — the one code that needs a human's eyes, not just
    a wait — reads identically to any other park among a day's bullets unless
    the code is visible right on the line.

    The suite verdict (CBN-EPIC-202.5) rides the same line, but only when it
    is not `green`: unlike `hermes_verdict_code`, `suite` is a required field
    on every closure outcome (CBN-EPIC-202.2), so an unconditional tag would
    fire on nearly every bullet, every day — the noise the `park_reason` idiom
    exists to avoid. A red/timeout/refused suite is the anomaly worth a scan,
    the same signal that stops a `proceed-on-green` drain (CBN-EPIC-202.4).

    The External review tag (CBN-257) follows the suite's anomaly-only rule:
    `ticked` and `na` are dispositions a runner recorded, so only `missing` —
    a full-shape note closed without the in-run review — earns a tag.

    `[not landed]` (CBN-EPIC-450.6) marks an isolated closure main did not get:
    the task closed on its branch, which is not main completion.
    """
    tag = f" [{run.hermes_verdict_code.value}]" if run.hermes_verdict_code else ""
    suite_tag = (
        f" [suite: {run.suite.verdict.value}]"
        if run.suite and run.suite.verdict is not SuiteVerdict.GREEN
        else ""
    )
    review_tag = " [review: missing]" if run.external_review is ExternalReview.MISSING else ""
    landing_tag = " [not landed]" if unlanded(run) else ""
    park = f" (park: {run.park_reason})" if run.park_reason else ""
    return (
        f"- **{run.task_id}** (`{run.outcome.value}`){tag}{suite_tag}{review_tag}{landing_tag}"
        f"{park} — {run.summary.strip()}"
    )


def _row_line(row: NewRow) -> str:
    """`{id} — unmarked` / `{id} — marked`, stated either way.

    Naming both states rather than annotating only the unmarked ones: a bare id
    would leave the reader to infer marker state from the count in the heading,
    which stops working the moment a run files a mix.
    """
    return f"{row.task_id} — {'marked' if row.unattended else 'unmarked'}"


def stale_receipt_line(stale: StaleReceipt) -> str:
    """One proven-dead `running` receipt as a digest bullet (CBN-444).

    Not day-bucketed like the run bullets: the receipt is a live condition, and
    a dead run's commits otherwise reach no report surface until the slug's next
    `run`. Report-only, as everywhere: the rewrite to `abandoned` is that run's.
    """
    task = stale.task_id or "no task"
    return (
        f"- **stale receipt** `{stale.receipt_id}` ({task}) — pid {stale.pid} gone, "
        f"since {iso_z(stale.started_at)}, last stage {stale.last_stage}; "
        f"{landed_line(stale.landed)}"
    )


def retained_lines(project: RegistryRecord, runs: list[RunRecord]) -> list[str]:
    """One bullet per `wt-<ID>` leftover of an isolated project (CBN-EPIC-450.6).

    Not day-bucketed, like a stale receipt: the leftover is a live condition,
    and the drain skips its id until the operator acts. The recovery commands
    are `status <id>`'s, to keep the bullet one line.
    """
    if not project.isolate:
        return []
    entries = retained_worktrees(project, report_retained(project), lambda: runs)
    if entries is None:
        return ["- **retained** unreadable — git could not list wt- branches and worktrees"]
    return [_retained_line(project, entry) for entry in entries]


def _retained_line(project: RegistryRecord, entry: RetainedWorktree) -> str:
    return (
        f"- **retained** `{entry.branch}` ({retained_state(entry)}) at {entry.path} — "
        f"recovery: caobunga status {project.id}"
    )


def format_digest(store: Store, *, now: datetime | None = None) -> str:
    """Markdown fleet rollup for the local calendar day of `now`, plus stale receipts
    and retained worktrees."""
    moment = now if now is not None else utc_now()
    day = local_day(moment)
    lines = [f"# Fleet digest — {day.isoformat()}", ""]
    projects = store.list_projects()
    if not projects:
        lines.append("(no registered projects)")
        return "\n".join(lines).rstrip() + "\n"
    # One scan for the fleet, not one per project (CBN-421's reason).
    by_project: dict[str, list[tuple[InvocationResult, bool | None]]] = {}
    for receipt, alive in running_verdicts(store, None):
        by_project.setdefault(receipt.project, []).append((receipt, alive))
    for project in projects:
        lines.append(f"## {project.id}")
        lines.append("")
        runs = store.list_runs(project.id)
        day_runs = runs_on_day(runs, day)
        if not day_runs:
            lines.append("(no runs)")
        else:
            for run in day_runs:
                lines.extend(run_lines(run))
        stale = stale_receipts(project, by_project.get(project.id, []))
        lines.extend(stale_receipt_line(item) for item in stale)
        lines.extend(retained_lines(project, runs))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
