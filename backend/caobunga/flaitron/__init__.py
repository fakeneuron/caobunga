"""Read a Flaitron project: PLAN.md + open tasknotes → next-task candidate."""

from __future__ import annotations

from pathlib import Path

from caobunga.errors import UserError

# Façade re-exports (CBN-143.4). The `X as X` form is PEP 484's explicit
# re-export and ruff's own remedy for F401 on a package `__init__`; it is applied
# uniformly, including to the names this module still uses itself, so the list
# does not churn as `CBN-143.5`–`.8` migrate that usage out. `__all__` is  # noqa: RUF003
# declined permanently (`CBN-143.N` F5, closing the `CBN-143.3` Q1 deferral) —
# a second, unenforced copy of the names pinned by `test_flaitron.py`'s `FACADE`
# is worse than none.
from caobunga.flaitron.dto import (
    Coverage as Coverage,
)
from caobunga.flaitron.dto import (
    Denial as Denial,
)
from caobunga.flaitron.dto import (
    NextTask as NextTask,
)
from caobunga.flaitron.dto import (
    OpenTasknote as OpenTasknote,
)
from caobunga.flaitron.dto import (
    PlanPriority as PlanPriority,
)
from caobunga.flaitron.dto import (
    PlanTask as PlanTask,
)
from caobunga.flaitron.dto import (
    ProjectRead as ProjectRead,
)
from caobunga.flaitron.dto import (
    TriageClass as TriageClass,
)
from caobunga.flaitron.dto import (
    UnparsedRow as UnparsedRow,
)
from caobunga.flaitron.grammar import (
    BLOCKED_BY_BLOCK as BLOCKED_BY_BLOCK,
)
from caobunga.flaitron.grammar import (
    BLOCKED_MENTION as BLOCKED_MENTION,
)
from caobunga.flaitron.grammar import (
    CHECKBOX_ROW as CHECKBOX_ROW,
)
from caobunga.flaitron.grammar import (
    CODE_SPAN as CODE_SPAN,
)
from caobunga.flaitron.grammar import (
    EXTRA_TOKEN as EXTRA_TOKEN,
)
from caobunga.flaitron.grammar import (
    FENCE_DELIMITER as FENCE_DELIMITER,
)
from caobunga.flaitron.grammar import (
    FRONTMATTER as FRONTMATTER,
)
from caobunga.flaitron.grammar import (
    HANDOFF_MARKER as HANDOFF_MARKER,
)
from caobunga.flaitron.grammar import (
    HEADING_LINE as HEADING_LINE,
)
from caobunga.flaitron.grammar import (
    HTML_COMMENT as HTML_COMMENT,
)
from caobunga.flaitron.grammar import (
    PARENT_EPIC as PARENT_EPIC,
)
from caobunga.flaitron.grammar import (
    PARK_SEPARATOR as PARK_SEPARATOR,
)
from caobunga.flaitron.grammar import (
    SUGGESTED_NEXT as SUGGESTED_NEXT,
)
from caobunga.flaitron.grammar import (
    TASK_ID_BODY as TASK_ID_BODY,
)
from caobunga.flaitron.grammar import (
    TASK_LINE as TASK_LINE,
)
from caobunga.flaitron.grammar import (
    UNATTENDED_MARKER as UNATTENDED_MARKER,
)
from caobunga.flaitron.grammar import (
    WIKILINK as WIKILINK,
)
from caobunga.flaitron.grammar import (
    XHEAVY_TOKEN as XHEAVY_TOKEN,
)
from caobunga.flaitron.grammar import (
    epic_parent as epic_parent,
)
from caobunga.flaitron.grammar import (
    epic_stem as epic_stem,
)
from caobunga.flaitron.grammar import (
    fence_mask as fence_mask,
)
from caobunga.flaitron.grammar import (
    parse_blocked_by as parse_blocked_by,
)
from caobunga.flaitron.grammar import (
    split_park_reason as split_park_reason,
)
from caobunga.flaitron.layout import (
    CORE_REL as CORE_REL,
)
from caobunga.flaitron.layout import (
    FT_TASK_SKILL_DIR_REL as FT_TASK_SKILL_DIR_REL,
)
from caobunga.flaitron.layout import (
    PLAN_REL as PLAN_REL,
)
from caobunga.flaitron.layout import (
    SELF_FT_TASK_SKILL_DIR_REL as SELF_FT_TASK_SKILL_DIR_REL,
)
from caobunga.flaitron.layout import (
    SIDEQUEST_DIR_REL as SIDEQUEST_DIR_REL,
)
from caobunga.flaitron.layout import (
    TASKNOTE_DIR_REL as TASKNOTE_DIR_REL,
)
from caobunga.flaitron.layout import (
    UNATTENDED_MODULE as UNATTENDED_MODULE,
)
from caobunga.flaitron.layout import (
    UNATTENDED_TOKEN as UNATTENDED_TOKEN,
)
from caobunga.flaitron.layout import (
    pinned_skill_rel as pinned_skill_rel,
)
from caobunga.flaitron.layout import (
    supports_unattended as supports_unattended,
)
from caobunga.flaitron.layout import (
    unattended_module_rel as unattended_module_rel,
)
from caobunga.flaitron.reader import (
    LAUNCH_BUCKETS as LAUNCH_BUCKETS,
)
from caobunga.flaitron.reader import (
    _suggested_next,
)
from caobunga.flaitron.reader import (
    parse_plan as parse_plan,
)
from caobunga.flaitron.reader import (
    seeded_line as seeded_line,
)
from caobunga.flaitron.reader import (
    unparsed_plan_rows as unparsed_plan_rows,
)
from caobunga.flaitron.selection import (
    _denied_rows,
)
from caobunga.flaitron.selection import (
    completed_task_ids as completed_task_ids,
)
from caobunga.flaitron.selection import (
    coverage as coverage,
)
from caobunga.flaitron.selection import (
    dispatchable as dispatchable,
)
from caobunga.flaitron.selection import (
    eligibility as eligibility,
)
from caobunga.flaitron.selection import (
    in_flight_task as in_flight_task,
)
from caobunga.flaitron.selection import (
    marker_drift as marker_drift,
)
from caobunga.flaitron.selection import (
    open_task_ids as open_task_ids,
)
from caobunga.flaitron.selection import (
    pick_next as pick_next,
)
from caobunga.flaitron.selection import (
    queue_denial as queue_denial,
)
from caobunga.flaitron.selection import (
    unparsed_blocked_clause as unparsed_blocked_clause,
)
from caobunga.flaitron.tasknote import (
    OPEN_STATUSES as OPEN_STATUSES,
)
from caobunga.flaitron.tasknote import (
    external_review_box as external_review_box,
)
from caobunga.flaitron.tasknote import (
    extract_follow_ups as extract_follow_ups,
)
from caobunga.flaitron.tasknote import (
    extract_summary as extract_summary,
)
from caobunga.flaitron.tasknote import (
    is_descoped as is_descoped,
)
from caobunga.flaitron.tasknote import (
    list_open_tasknotes as list_open_tasknotes,
)
from caobunga.flaitron.tasknote import (
    phase_progress as phase_progress,
)


def read_project(root: Path, *, unattended: bool = False, drain: bool = False) -> ProjectRead:
    """Parse `<root>/.flaitron/PLAN.md` and open tasknotes; pick the next task.

    `unattended=True` applies the safety dimension (CBN-EPIC-17.2): unmarked
    tasks are denied from the pick and listed on `denied` with their reasons.
    Every *launching* tier passes it, `gated` included (CBN-118) — see
    `pick_next` for the two dimensions and why `drain` is the separate one. The
    attended default is byte-identical to the pre-marker behavior and is what
    the non-launching readers use: `observe` and the status API.

    The `Blocked by` gate (CBN-116) applies on both postures. Its denials join
    `denied` on an unattended read — the drain echoes that list, and a skip it
    cannot explain is the silence this package refuses.

    A PLAN that is not UTF-8 is a `UserError`, like a missing one (CBN-323): the
    pick is never made off a lossy read, and `try_read_project` degrades it.
    """
    repo = root.expanduser().resolve()
    if not repo.is_dir():
        raise UserError(f"not a directory: {root}")
    plan_path = repo / PLAN_REL
    if not plan_path.is_file():
        raise UserError(f"not a Flaitron project (missing {PLAN_REL}): {repo}")
    try:
        markdown = plan_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise UserError(f"{PLAN_REL} is not valid UTF-8: {repo}") from exc
    tasks = parse_plan(markdown)
    notes = list_open_tasknotes(repo)
    return ProjectRead(
        suggested_next=_suggested_next(markdown),
        tasks=tasks,
        open_tasknotes=notes,
        candidate=pick_next(tasks, notes, unattended=unattended, drain=drain),
        in_flight=in_flight_task(tasks, notes),
        denied=_denied_rows(
            tasks, unattended=unattended, completed_ids=completed_task_ids(tasks), notes=notes
        ),
        coverage=coverage(tasks, unparsed_plan_rows(markdown), notes),
    )


def try_read_project(root: Path) -> ProjectRead | None:
    """`read_project` under the skip-not-fail policy: an unreadable repo is None.

    The reports read every registered path on one request (§"Status API"), and a
    path that has moved, been deleted, or never held a Flaitron project must cost
    that one row rather than the whole response. Callers render the null state.

    It exists so the CLI and the API share one wrapper *and* one read per project
    — `current` and `coverage` come off the same `ProjectRead` rather than
    parsing the same PLAN twice.

    An `OSError` on the read (CBN-325) degrades the same way. It is caught here
    and not mapped in `read_project`: `wait.poll` treats it as transient
    (CBN-168), and a `UserError` there would turn that into an abort.
    """
    try:
        return read_project(root)
    except (UserError, OSError):
        return None
