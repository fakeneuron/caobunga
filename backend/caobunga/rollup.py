"""Epic rollup writer (CBN-031). Folds run records, not tasknote phase logs."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from caobunga.digest import run_bullet
from caobunga.flaitron import (
    PARENT_EPIC,
    TASK_ID_BODY,
    epic_parent,
    parse_plan,
    unfenced_comments,
)
from caobunga.models import RunRecord, compact_utc, utc_now
from caobunga.store import Store

# The start is a whole grammar id, so `CBN-EPIC-16–18` reads as one non-plain id  # noqa: RUF003
# and is skipped, never as area `EPIC` (CBN-402). Only an en dash may be spaced:
# a spaced hyphen is the title separator, so `CBN-050 - 2026 rollout` is no
# 1,977-id range (CBN-408).
ID_RANGE = re.compile(rf"(?<![\w-])({TASK_ID_BODY})(?:\s*–\s*|-)(\d+)\b")  # noqa: RUF001
# Title separators in priority order, checked outside range matches (CBN-408).
TITLE_SEPARATORS = ("—", " – ", " - ")  # noqa: RUF001
PLAIN_ID = re.compile(r"^([A-Z]+)-(\d+)$")


@dataclass(frozen=True)
class EpicCohort:
    key: str
    title: str
    task_ids: frozenset[str]


def parse_epic_cohorts(markdown: str) -> list[EpicCohort]:
    """Discover epic cohorts from PLAN HTML comments and Flaitron EPIC parent rows."""
    cohorts: list[EpicCohort] = []
    seen_keys: set[str] = set()
    tasks = parse_plan(markdown)
    # Authoritative spellings for plain AREA-N rows (padding as filed in PLAN).
    by_area_num: dict[tuple[str, int], str] = {}
    for task in tasks:
        plain = PLAIN_ID.fullmatch(task.task_id)
        if plain is not None:
            by_area_num[(plain.group(1), int(plain.group(2)))] = task.task_id

    for cohort in _range_cohorts(markdown, by_area_num):
        if cohort.key not in seen_keys:
            cohorts.append(cohort)
            seen_keys.add(cohort.key)

    for task in tasks:
        if PARENT_EPIC.fullmatch(task.task_id) is None:
            continue
        parent = task.task_id
        # Match children by canonical parent, not by literal string prefix: a
        # `DOC-EPIC-20` row's children are legally filed as `DOC-20.1`, which no
        # prefix of the parent id ever matches (CBN-EPIC-9.5 F6).
        children = frozenset(
            other.task_id for other in tasks if epic_parent(other.task_id) == parent
        )
        if not children:
            continue
        key = parent.replace("-", "_")
        if key in seen_keys:
            continue
        cohorts.append(EpicCohort(key=key, title=parent, task_ids=children))
        seen_keys.add(key)

    return cohorts


def _range_cohorts(
    markdown: str,
    by_area_num: dict[tuple[str, int], str],
) -> Iterator[EpicCohort]:
    # Ranges live only in unfenced PLAN comments: one in a task row's prose or
    # a fenced example is not a cohort declaration (CBN-402).
    for comment in unfenced_comments(markdown):
        title = _comment_title(comment)
        for match in ID_RANGE.finditer(comment):
            start = PLAIN_ID.fullmatch(match.group(1))
            if start is None:
                continue
            area, start_raw, end_raw = start.group(1), start.group(2), match.group(2)
            start_s, end_s = int(start_raw), int(end_raw)
            if end_s < start_s:
                continue
            width = max(len(start_raw), len(end_raw))
            ids = frozenset(
                _range_member(area, num, width, by_area_num) for num in range(start_s, end_s + 1)
            )
            start_token = _range_member(area, start_s, width, by_area_num).partition("-")[2]
            end_token = _range_member(area, end_s, width, by_area_num).partition("-")[2]
            yield EpicCohort(key=f"{area}-{start_token}-{end_token}", title=title, task_ids=ids)


def _range_member(
    area: str,
    num: int,
    width: int,
    by_area_num: dict[tuple[str, int], str],
) -> str:
    """Prefer the PLAN spelling; else synthesize with the comment's digit width."""
    existing = by_area_num.get((area, num))
    if existing is not None:
        return existing
    return f"{area}-{num:0{width}d}"


def _comment_title(comment: str) -> str:
    inner = (comment.strip().splitlines() or [""])[0].strip()
    # A range's own dash is never the separator: `DOC-1 – 5 docs` is not `DOC-1`.  # noqa: RUF003
    # Non-space filler, so blanking cannot mint a spaced separator beside it.
    unranged = ID_RANGE.sub(lambda match: "x" * len(match.group(0)), inner)
    for separator in TITLE_SEPARATORS:
        cut = unranged.find(separator)
        if cut != -1:
            return inner[:cut].strip()
    return inner or "Epic rollup"


def cohort_for_task(cohorts: list[EpicCohort], task_id: str) -> EpicCohort | None:
    for cohort in cohorts:
        if task_id in cohort.task_ids:
            return cohort
    return None


def cohort_complete(cohort: EpicCohort, markdown: str) -> bool:
    by_id = {task.task_id: task for task in parse_plan(markdown)}
    for task_id in cohort.task_ids:
        task = by_id.get(task_id)
        if task is None or not task.completed:
            return False
    return True


def format_rollup(project: str, cohort: EpicCohort, runs: list[RunRecord]) -> str:
    lines = [
        f"# Epic rollup — {project} — {cohort.key}",
        "",
        cohort.title,
        "",
        "## Runs",
        "",
    ]
    if not runs:
        lines.append("(no run records for this cohort)")
    else:
        for run in runs:
            lines.append(run_bullet(run))
    return "\n".join(lines).rstrip() + "\n"


def maybe_write_rollup(
    store: Store,
    project: str,
    closed_task_id: str,
    plan_markdown: str,
) -> Path | None:
    """When `closed_task_id` completes an epic cohort, write a rollup artifact."""
    cohorts = parse_epic_cohorts(plan_markdown)
    cohort = cohort_for_task(cohorts, closed_task_id)
    if cohort is None or not cohort_complete(cohort, plan_markdown):
        return None
    if store.has_rollup(project, cohort.key):
        return None
    runs = store.runs_for_tasks(project, cohort.task_ids)
    body = format_rollup(project, cohort, runs)
    stamp = compact_utc(runs[-1].closed_at) if runs else compact_utc(utc_now())
    return store.save_rollup(project, stamp, cohort.key, body)
