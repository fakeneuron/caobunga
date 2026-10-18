"""Fleet-wide fold over run records (CBN-EPIC-12.7).

Folds run records only — never tasknote phase logs or worker transcripts, per
docs/CONTRACT.md §"Status API". Sits beside `digest.py` and `rollup.py` because
`api.py` imports folds, never the reverse.

Two conventions inherited from CBN-EPIC-12.3 and kept deliberately:
every tally seeds its full enum key set, so a zero is a real answer and no
consumer guards for a missing key; and every ordering is an explicit sort on
this surface rather than a property inherited from the filesystem.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from statistics import median
from typing import Any

from caobunga.digest import local_day
from caobunga.models import Escalation, Outcome, RunRecord
from caobunga.store import Store


def outcome_counts(runs: Iterable[RunRecord]) -> dict[str, int]:
    """Tally by outcome, seeded so all five keys are always present.

    The UI (CBN-EPIC-12.4) renders a fixed chip set; a zero is a real answer,
    so an absent key would only make the consumer guard for it.
    """
    counts = dict.fromkeys((outcome.value for outcome in Outcome), 0)
    for run in runs:
        counts[run.outcome.value] += 1
    return counts


def escalation_counts(runs: Iterable[RunRecord]) -> dict[str, int]:
    """Tally by escalation reason, seeded with all seven values.

    `sum()` may fall short of `failed + blocked`: records written before
    CBN-EPIC-12.2 stopped without a reason, and the model deliberately still
    loads them (see `RunRecord._escalation_only_when_stopped`).
    """
    counts = dict.fromkeys((reason.value for reason in Escalation), 0)
    for run in runs:
        if run.escalation_reason is not None:
            counts[run.escalation_reason.value] += 1
    return counts


def run_seconds(run: RunRecord) -> int | None:
    """Wall-clock duration, or None when the record cannot supply one.

    Duration is derived, never stored (docs/CONTRACT.md §"Run record"). Records
    predating CBN-EPIC-12.2 carry no `started_at` and are excluded rather than
    counted as zero, which would drag every median toward it.
    """
    if run.started_at is None:
        return None
    seconds = (run.closed_at - run.started_at).total_seconds()
    if seconds < 0:
        return None
    return round(seconds)


def duration_stats(runs: Iterable[RunRecord]) -> dict[str, int | None]:
    """Median and worst case in seconds. `counted` names the contributing sample."""
    spans = sorted(span for span in map(run_seconds, runs) if span is not None)
    if not spans:
        return {"counted": 0, "median_seconds": None, "max_seconds": None}
    return {
        "counted": len(spans),
        "median_seconds": round(median(spans)),
        "max_seconds": spans[-1],
    }


def throughput(runs: Iterable[RunRecord]) -> list[dict[str, Any]]:
    """One row per local day that saw runs, newest day first.

    Bucketing reuses `digest.local_day` — the fleet's one day boundary, moved
    off UTC by CBN-EPIC-95.4 because a UTC day rolls over mid-evening for a
    UTC-4 operator. A second boundary here would be a second thing to drift.
    """
    buckets: dict[date, list[RunRecord]] = {}
    for run in runs:
        buckets.setdefault(local_day(run.closed_at), []).append(run)
    return [
        {"day": day.isoformat(), "runs": len(members), "outcomes": outcome_counts(members)}
        for day, members in sorted(buckets.items(), reverse=True)
    ]


def grouped(
    runs: Iterable[RunRecord], key: Callable[[RunRecord], str | None]
) -> list[dict[str, Any]]:
    """Rows for one routing dimension: busiest first, then key ascending, null last.

    Runs whose key is unset bucket under `null` rather than being dropped, which
    is what keeps `sum(row["runs"]) == len(runs)` true for every dimension — the
    same checkable-invariant habit as CBN-EPIC-12.3's `run_counts`.
    """
    buckets: dict[str | None, list[RunRecord]] = {}
    for run in runs:
        buckets.setdefault(key(run), []).append(run)
    rows = [
        {
            "key": bucket,
            "runs": len(members),
            "outcomes": outcome_counts(members),
            "duration": duration_stats(members),
        }
        for bucket, members in buckets.items()
    ]
    rows.sort(key=lambda row: (-row["runs"], row["key"] is None, row["key"] or ""))
    return rows


def fleet_stats(store: Store) -> dict[str, Any]:
    """Aggregate every registered project's run records into one read.

    Unbounded in v1, stated the way CBN-EPIC-12.3 stated it for `runs`: every
    record for every project is folded on each request. No pagination, no cap,
    no window parameter.
    """
    projects = store.list_projects()
    runs = [run for project in projects for run in store.list_runs(project.id)]
    outcomes = outcome_counts(runs)
    return {
        "projects": len(projects),
        "runs": len(runs),
        # Legacy records carry no key and are simply not counted, so this is
        # "invocations we can name", not a claim about fleet history. The key
        # is (project, invocation_id): parallel legs can share a millisecond
        # stamp, and a bare id would merge them. One project's drain stays one.
        "invocations": len({(run.project, run.invocation_id) for run in runs if run.invocation_id}),
        "outcomes": outcomes,
        # Strictly `closed / runs`. `de-scoped` is a legitimate Flaitron closure
        # but not work landed, so it counts against the rate; the raw tallies
        # above are there for any other reading.
        "success_rate": round(outcomes[Outcome.CLOSED.value] / len(runs), 3) if runs else None,
        "duration": duration_stats(runs),
        "escalations": escalation_counts(runs),
        "throughput": throughput(runs),
        "routing": {
            "provider": grouped(runs, lambda run: run.provider),
            "model": grouped(runs, lambda run: run.model),
            "plan_model": grouped(runs, lambda run: run.plan_model),
        },
    }
