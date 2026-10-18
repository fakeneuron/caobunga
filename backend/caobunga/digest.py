"""Daily fleet digest (CBN-032). Folds run records, not tasknote phase logs."""

from __future__ import annotations

from datetime import date, datetime

from caobunga.models import ExternalReview, NewRow, RunRecord, SuiteVerdict, utc_now
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
    """
    tag = f" [{run.hermes_verdict_code.value}]" if run.hermes_verdict_code else ""
    suite_tag = (
        f" [suite: {run.suite.verdict.value}]"
        if run.suite and run.suite.verdict is not SuiteVerdict.GREEN
        else ""
    )
    review_tag = " [review: missing]" if run.external_review is ExternalReview.MISSING else ""
    park = f" (park: {run.park_reason})" if run.park_reason else ""
    lines = [
        f"- **{run.task_id}** (`{run.outcome.value}`){tag}{suite_tag}{review_tag}{park}"
        f" — {run.summary.strip()}"
    ]
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


def _row_line(row: NewRow) -> str:
    """`{id} — unmarked` / `{id} — marked`, stated either way.

    Naming both states rather than annotating only the unmarked ones: a bare id
    would leave the reader to infer marker state from the count in the heading,
    which stops working the moment a run files a mix.
    """
    return f"{row.task_id} — {'marked' if row.unattended else 'unmarked'}"


def format_digest(store: Store, *, now: datetime | None = None) -> str:
    """Markdown fleet rollup for the local calendar day of `now`."""
    moment = now if now is not None else utc_now()
    day = local_day(moment)
    lines = [f"# Fleet digest — {day.isoformat()}", ""]
    projects = store.list_projects()
    if not projects:
        lines.append("(no registered projects)")
        return "\n".join(lines).rstrip() + "\n"
    for project in projects:
        lines.append(f"## {project.id}")
        lines.append("")
        day_runs = runs_on_day(store.list_runs(project.id), day)
        if not day_runs:
            lines.append("(no runs)")
        else:
            for run in day_runs:
                lines.extend(run_lines(run))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
