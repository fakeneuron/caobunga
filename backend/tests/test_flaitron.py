"""Flaitron PLAN.md + open-tasknote reader (CBN-011)."""

import ast
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import caobunga
from caobunga import flaitron
from caobunga.errors import UserError
from caobunga.flaitron import (
    CORE_REL,
    FT_TASK_SKILL_DIR_REL,
    HEADING_LINE,
    LAUNCH_BUCKETS,
    SELF_FT_TASK_SKILL_DIR_REL,
    UNATTENDED_MODULE,
    Denial,
    PlanPriority,
    PlanTask,
    TriageClass,
    completed_task_ids,
    coverage,
    dispatchable,
    eligibility,
    epic_parent,
    epic_stem,
    fence_mask,
    in_flight_task,
    list_open_tasknotes,
    marker_drift,
    open_task_ids,
    parse_blocked_by,
    parse_plan,
    pick_next,
    pinned_skill_rel,
    read_project,
    seeded_line,
    split_park_reason,
    supports_unattended,
    try_read_project,
    unattended_module_rel,
    unparsed_blocked_clause,
    unparsed_plan_rows,
)
from caobunga.flaitron.selection import _HANDOFF_KEYWORDS, _task_id_sort_key

# Flaitron's shared task-line grammar conformance fixtures (CORE-618), reached
# through the pinned submodule (CBN-EPIC-13.2 fleet boundary) — see
# "SPEC/fixtures/plan conformance" near the end of this file.
PLAN_FIXTURES_DIR = (
    Path(__file__).resolve().parents[2] / ".flaitron" / "core" / "SPEC" / "fixtures" / "plan"
)
# `rotated-history` covers `PLAN-ARCHIVE.md` grammar; caobunga never reads that
# file (README.md §"Files" marks it optional for a consumer in that position).
# `README` is the fixture directory's own doc, not a case (no `.json` sibling).
PLAN_FIXTURE_CASES = sorted(
    p.stem for p in PLAN_FIXTURES_DIR.glob("*.md") if p.stem not in {"README", "rotated-history"}
)


def _repo(tmp_path: Path, plan: str, notes: dict[str, str] | None = None) -> Path:
    root = tmp_path / "repo"
    tasknotes = root / ".flaitron" / "tasknote"
    tasknotes.mkdir(parents=True)
    (root / ".flaitron" / "PLAN.md").write_text(plan, encoding="utf-8")
    for task_id, body in (notes or {}).items():
        (tasknotes / f"{task_id}.md").write_text(body, encoding="utf-8")
    return root


def _note(status: str, title: str = "t") -> str:
    return f"---\ntitle: {title}\nstatus: {status}\ncreated: 2026-08-19\n---\n\n# body\n"


def _parked(reason: str) -> str:
    """A parked note carrying a raw `park-reason:` frontmatter value."""
    return (
        f"---\ntitle: t\nstatus: blocked\npark-reason: {reason}\n"
        "created: 2026-08-19\n---\n\n# body\n"
    )


MINIMAL = """# sample

**Suggested next task:** **CBN-011** — Flaitron reader.

## High

<!--
- [ ] **CBN-999** [medium] | shortname — example inside a comment
-->

- [ ] **CBN-011** [medium] | flaitron-reader — PLAN.md + open tasknotes → next-task candidate.
- [ ] **CBN-012** [medium] | cao-client — Launch a session.

## Medium

- [ ] **CBN-020** [medium] | usage-adapters — Leftover usage.

## Low

(none)

## Future Opportunities

- [ ] **CBN-050** [medium] | later-tiers — Not first ship.

## Completed

- [x] **CBN-010** [medium] | local-registry — Completed 2026-08-19.
"""


def test_parse_skips_comments_completed_and_future() -> None:
    tasks = parse_plan(MINIMAL)
    ids = [t.task_id for t in tasks]
    assert ids == ["CBN-011", "CBN-012", "CBN-020", "CBN-050", "CBN-010"]
    assert [t.task_id for t in tasks if not t.completed][:3] == ["CBN-011", "CBN-012", "CBN-020"]
    by_id = {t.task_id: t for t in tasks}
    assert by_id["CBN-011"].model == "medium"
    assert by_id["CBN-011"].shortname == "flaitron-reader"
    assert by_id["CBN-010"].completed is True
    assert by_id["CBN-010"].priority is PlanPriority.COMPLETED
    assert by_id["CBN-050"].priority is PlanPriority.FUTURE


def test_parse_skips_fenced_examples_and_pre_heading_rows() -> None:
    md = """# intro

- [ ] **CBN-099** [light] | stray — above any priority heading.

```markdown
## High
- [ ] **CBN-001** [light] | fenced — not a real row.
```

## High

- [ ] **CBN-002** [light] | real — kept.
"""
    tasks = parse_plan(md)
    assert [t.task_id for t in tasks] == ["CBN-002"]


def test_parse_grammar_tolerances() -> None:
    md = """## High

- [ ] 🟢 **CBN-001** [!critical] [heavy] | urgent — first.
- [ ] **CBN-002** [medium]🧠 | glyph — decorative.
- [ ] **CBN-007** [xheavy]🔭 | xglyph — exploratory rung.
- [ ] **CBN-008** [frontier]💎 | fglyph — frontier rung (flaitron v6.1.0).
- [ ] **CBN-003** [fable] [light] | stacked — first token wins.
- [ ] **CBN-004** [medium] [!critical] | swapped — still critical.
  - [ ] **CBN-005.1** [medium] | child — indented epic child.
- [ ] **CBN-006** — legacy dash only.
"""
    tasks = parse_plan(md)
    by_id = {t.task_id: t for t in tasks}
    assert list(by_id) == [
        "CBN-001",
        "CBN-002",
        "CBN-007",
        "CBN-008",
        "CBN-003",
        "CBN-004",
        "CBN-005.1",
        "CBN-006",
    ]
    assert by_id["CBN-001"].critical is True
    assert by_id["CBN-001"].model == "heavy"
    assert by_id["CBN-002"].model == "medium"
    assert by_id["CBN-007"].model == "xheavy"
    assert by_id["CBN-007"].shortname == "xglyph"
    assert by_id["CBN-008"].model == "frontier"
    assert by_id["CBN-008"].shortname == "fglyph"
    assert by_id["CBN-003"].model == "fable"
    assert by_id["CBN-004"].critical is True
    assert by_id["CBN-005.1"].shortname == "child"
    assert by_id["CBN-006"].shortname is None
    assert by_id["CBN-006"].description == "legacy dash only."


def test_legacy_critical_heading_flags_rows() -> None:
    md = """## Critical

- [ ] **BE-001** [light] | hot — legacy heading.
"""
    tasks = parse_plan(md)
    assert tasks[0].priority is PlanPriority.HIGH
    assert tasks[0].critical is True


def test_spec_selection_skips_future_completed_and_parent_epics() -> None:
    md = """## High

- [ ] **CBN-EPIC-1** [heavy] | epic — tracking row.
  - [ ] **CBN-011.1** [medium] | discovery — child.
- [ ] **CBN-012** [medium] | later — higher id.

## Future Opportunities

- [ ] **CBN-001** [light] | future-low-id — must not win.

## Completed

- [x] **CBN-010** [medium] | done — ignored.
"""
    chosen = pick_next(parse_plan(md))
    assert chosen is not None
    assert chosen.task_id == "CBN-011.1"
    assert chosen.reason == "spec-selection"
    assert chosen.plan_model == "medium"


def test_critical_high_beats_lower_id() -> None:
    md = """## High

- [ ] **CBN-001** [medium] | ordinary — first by id.
- [ ] **CBN-099** [!critical] [heavy] | fire — urgency.
"""
    chosen = pick_next(parse_plan(md))
    assert chosen is not None
    assert chosen.task_id == "CBN-099"
    assert chosen.critical is True


def test_natural_id_sort_within_bucket() -> None:
    md = """## Medium

- [ ] **CBN-10** [light] | ten — two digits.
- [ ] **CBN-9** [light] | nine — one digit.
"""
    chosen = pick_next(parse_plan(md))
    assert chosen is not None
    assert chosen.task_id == "CBN-9"


def test_mixed_suffix_ids_sort_without_type_error() -> None:
    """`X-1.2a` beside `X-1.2.1` / `X-1.2.N` used to raise `TypeError` from the sort (CBN-342)."""
    md = """## High

- [ ] **CBN-324.N** [light] | audit — last.
- [ ] **CBN-324.2a** [light] | lettered — after its bare number's children.
- [ ] **CBN-324.2.1** [light] | nested — child of .2.
- [ ] **CBN-324.2** [light] | bare — first.
"""
    tasks = parse_plan(md)
    assert pick_next(tasks) is not None
    order = [d.task_id for d in coverage(tasks).denials]
    assert order == ["CBN-324.2", "CBN-324.2.1", "CBN-324.2a", "CBN-324.N"]


def test_task_id_sort_key_elements_are_int_str_pairs() -> None:
    """Every element is an `(int, str)` pair; an `-EPIC-` stem keys like the bare one (CBN-342)."""
    for task_id in ("CBN-9", "CBN-324.2a", "CBN-324.2.1", "CBN-324.N", "CBN-EPIC-324.N"):
        key = _task_id_sort_key(task_id)
        assert all(isinstance(n, int) and isinstance(s, str) for n, s in key), f"{task_id}: {key}"
    assert _task_id_sort_key("CBN-EPIC-324.N") == _task_id_sort_key("CBN-324.N")


def test_in_progress_override_and_open_tasknote_attachment(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        MINIMAL,
        {
            "CBN-020": _note("in-progress"),
            "README": "# Tasknote Directory\n",
        },
    )
    result = read_project(root)
    assert result.suggested_next == "CBN-011"
    assert result.candidate is not None
    assert result.candidate.task_id == "CBN-020"
    assert result.candidate.reason == "in-progress-override"
    assert result.candidate.tasknote is not None
    assert result.candidate.tasknote.status == "in-progress"
    assert result.candidate.tasknote.path == ".flaitron/tasknote/CBN-020.md"
    assert [n.task_id for n in result.open_tasknotes] == ["CBN-020"]


def test_starter_does_not_override_spec_order(tmp_path: Path) -> None:
    root = _repo(tmp_path, MINIMAL, {"CBN-020": _note("starter")})
    result = read_project(root)
    assert result.candidate is not None
    assert result.candidate.task_id == "CBN-011"
    assert result.candidate.reason == "spec-selection"
    assert result.candidate.tasknote is None


def test_blocked_status_attaches_without_override(tmp_path: Path) -> None:
    root = _repo(tmp_path, MINIMAL, {"CBN-011": _note("blocked")})
    result = read_project(root)
    assert result.candidate is not None
    assert result.candidate.task_id == "CBN-011"
    assert result.candidate.reason == "spec-selection"
    assert result.candidate.tasknote is not None
    assert result.candidate.tasknote.status == "blocked"


def test_parked_head_is_denied_and_skipped_on_unattended_pick(tmp_path: Path) -> None:
    """CBN-252: a parked row is never re-picked headless; the pick falls through."""
    root = _repo(tmp_path, MARKED_PLAN, {"CBN-011": _parked("drift — premise contradicted")})
    result = read_project(root, unattended=True)
    assert result.candidate is not None
    assert result.candidate.task_id == "CBN-012"
    assert [(d.task_id, d.reason) for d in result.denied] == [
        (
            "CBN-011",
            "parked — tasknote is blocked (drift); resume attended with /ft-task CBN-011",
        )
    ]
    assert result.denied[0].seeded_line is None
    assert result.coverage.marked_tasks == ["CBN-012"]
    assert result.coverage.marked + result.coverage.denied == result.coverage.open


def test_parked_only_row_leaves_no_candidate(tmp_path: Path) -> None:
    plan = "# sample\n\n## High\n\n- [ ] **CBN-011** [medium] [unattended] | a — work.\n"
    root = _repo(tmp_path, plan, {"CBN-011": _note("blocked")})
    result = read_project(root, unattended=True)
    assert result.candidate is None
    assert result.denied[0].reason == (
        "parked — tasknote is blocked (no park-reason); resume attended with /ft-task CBN-011"
    )


AUDIT_PLAN = """# sample

## High

- [ ] **CBN-EPIC-41** [light] | parent — tracking row.
  - [x] **CBN-EPIC-41.1** [light] | discovery — closed.
  - [ ] **CBN-EPIC-41.2** [light] | plan-current-state — open, unmarked.
  - [ ] **CBN-EPIC-41.3** [light] | readme-status — open, unmarked.
  - [ ] **CBN-EPIC-41.N** [light] [unattended] | audit — marked early.
- [ ] **CBN-050** [light] | standalone — unmarked.
"""


def test_marked_audit_with_open_siblings_is_denied_on_unattended_pick() -> None:
    """CBN-309: the JD-041.N shape — a marked `.N` whose siblings are still open."""
    tasks = parse_plan(AUDIT_PLAN)
    assert pick_next(tasks, unattended=True) is None
    audit = next(t for t in tasks if t.task_id == "CBN-EPIC-41.N")
    allow, reason = eligibility(
        audit,
        unattended=True,
        completed_ids=completed_task_ids(tasks),
        open_ids=open_task_ids(tasks),
    )
    assert allow is False
    assert reason == (
        "audit — epic siblings still open (CBN-EPIC-41.2, CBN-EPIC-41.3); "
        "the .N audit runs after they close"
    )
    denial = next(d for d in coverage(tasks).denials if d.task_id == "CBN-EPIC-41.N")
    assert denial.reason == reason
    assert denial.seeded_line is None
    assert denial.triage is None
    assert dispatchable(tasks, "CBN-EPIC-41.N", unattended=True) is False


def test_audit_siblings_fold_both_child_id_forms() -> None:
    """`CBN-41.2` and `CBN-EPIC-41.N` are one cohort — keyed by `epic_parent`."""
    plan = (
        "## High\n\n"
        "- [ ] **CBN-41.2** [light] | a — open, SPEC/epic.md filing shape.\n"
        "- [ ] **CBN-EPIC-41.N** [light] [unattended] | audit — marked early.\n"
    )
    tasks = parse_plan(plan)
    audit = next(t for t in tasks if t.task_id == "CBN-EPIC-41.N")
    allow, reason = eligibility(audit, unattended=True, open_ids=open_task_ids(tasks))
    assert allow is False
    assert "(CBN-41.2)" in reason


NUMBERED_AUDIT_PLAN = """# sample

## High

- [ ] **DEPLOY-EPIC-1** [light] | parent — tracking row.
  - [x] **DEPLOY-EPIC-1.1** [light] | discovery — closed.
  - [ ] **DEPLOY-EPIC-1.2** [light] | cf-access — open, unmarked.
  - [ ] **DEPLOY-EPIC-1.3** [light] [unattended] | audit-log — marked, not an audit.
  - [ ] **DEPLOY-EPIC-1.4** [light] [unattended] | access-gate audit
"""


def test_marked_numbered_audit_with_open_siblings_is_denied() -> None:
    """CBN-350: a fleet repo's DEPLOY-EPIC-1.4 shape — audit named only by shortname."""
    tasks = parse_plan(NUMBERED_AUDIT_PLAN)
    audit = next(t for t in tasks if t.task_id == "DEPLOY-EPIC-1.4")
    allow, reason = eligibility(audit, unattended=True, open_ids=open_task_ids(tasks))
    assert allow is False
    assert reason == (
        "audit — epic siblings still open (DEPLOY-EPIC-1.2, DEPLOY-EPIC-1.3); "
        "the .4 audit runs after they close"
    )
    assert dispatchable(tasks, "DEPLOY-EPIC-1.4", unattended=True) is False


def test_numbered_child_without_audit_shortname_is_not_an_audit() -> None:
    """`audit-log` ends in `log`, so `.3` keeps its `marked` reason despite open `.2`."""
    tasks = parse_plan(NUMBERED_AUDIT_PLAN)
    child = next(t for t in tasks if t.task_id == "DEPLOY-EPIC-1.3")
    assert eligibility(child, unattended=True, open_ids=open_task_ids(tasks)) == (
        True,
        "marked — [unattended] on the PLAN line",
    )


def test_marked_numbered_audit_dispatches_once_siblings_close() -> None:
    plan = NUMBERED_AUDIT_PLAN.replace(
        "- [ ] **DEPLOY-EPIC-1.2**", "- [x] **DEPLOY-EPIC-1.2**"
    ).replace("- [ ] **DEPLOY-EPIC-1.3**", "- [x] **DEPLOY-EPIC-1.3**")
    chosen = pick_next(parse_plan(plan), unattended=True)
    assert chosen is not None
    assert chosen.task_id == "DEPLOY-EPIC-1.4"


def test_numbered_audit_waits_on_every_open_sibling() -> None:
    """`/ft-close-epic` bails on any open sibling, so later ones and `.N` hold `.4` too."""
    plan = (
        "## High\n\n"
        "- [x] **X-EPIC-1.2** [light] | work — closed.\n"
        "- [ ] **X-EPIC-1.3a** [light] | lettered — open.\n"
        "- [ ] **X-EPIC-1.4** [light] [unattended] | gate_audits.\n"
        "- [ ] **X-EPIC-1.5.1** [light] | later nested — open.\n"
        "- [ ] **X-EPIC-1.N** [light] [unattended] | audit — final.\n"
    )
    tasks = parse_plan(plan)
    open_ids = open_task_ids(tasks)
    numbered = next(t for t in tasks if t.task_id == "X-EPIC-1.4")
    final = next(t for t in tasks if t.task_id == "X-EPIC-1.N")
    assert eligibility(numbered, unattended=True, open_ids=open_ids)[1] == (
        "audit — epic siblings still open (X-EPIC-1.3a, X-EPIC-1.5.1, X-EPIC-1.N); "
        "the .4 audit runs after they close"
    )
    assert eligibility(final, unattended=True, open_ids=open_ids)[1] == (
        "audit — epic siblings still open (X-EPIC-1.3a, X-EPIC-1.4, X-EPIC-1.5.1); "
        "the .N audit runs after they close"
    )


def test_audit_without_open_ids_cannot_confirm_and_denies() -> None:
    audit = next(t for t in parse_plan(AUDIT_PLAN) if t.task_id == "CBN-EPIC-41.N")
    assert eligibility(audit, unattended=True) == (
        False,
        "audit — epic siblings cannot be confirmed closed",
    )


def test_marker_drift_does_not_report_an_early_audit() -> None:
    """Sibling sequencing is not an evaporated marker (CBN-125 scope)."""
    assert marker_drift(parse_plan(AUDIT_PLAN), {"CBN-EPIC-41.N"}) == []


def test_marked_audit_dispatches_once_every_sibling_is_checked() -> None:
    plan = AUDIT_PLAN.replace("- [ ] **CBN-EPIC-41.2**", "- [x] **CBN-EPIC-41.2**").replace(
        "- [ ] **CBN-EPIC-41.3**", "- [x] **CBN-EPIC-41.3**"
    )
    chosen = pick_next(parse_plan(plan), unattended=True)
    assert chosen is not None
    assert chosen.task_id == "CBN-EPIC-41.N"
    assert chosen.eligibility_reason == "marked — [unattended] on the PLAN line"


def test_unmarked_audit_keeps_its_seed_offer() -> None:
    """Seeding the cohort in one pass is still the fix, so `unmarked` stays the reason."""
    plan = AUDIT_PLAN.replace("**CBN-EPIC-41.N** [light] [unattended]", "**CBN-EPIC-41.N** [light]")
    denial = next(d for d in coverage(parse_plan(plan)).denials if d.task_id == "CBN-EPIC-41.N")
    assert denial.reason.startswith("unmarked — ")
    assert denial.seeded_line is not None


def test_audit_dimension_is_launching_tier_only() -> None:
    chosen = pick_next(parse_plan(AUDIT_PLAN))
    assert chosen is not None
    assert chosen.task_id == "CBN-EPIC-41.2"
    audit = next(t for t in parse_plan(AUDIT_PLAN) if t.task_id == "CBN-EPIC-41.N")
    assert eligibility(audit, open_ids=open_task_ids(parse_plan(AUDIT_PLAN)))[0] is True


def test_parked_dimension_is_unattended_only(tmp_path: Path) -> None:
    """Attended reads keep the parked row: the operator resuming it is the fix."""
    task = parse_plan(MARKED_PLAN)[0]
    note = list_open_tasknotes(_repo(tmp_path, MARKED_PLAN, {"CBN-011": _note("blocked")}))[0]
    assert eligibility(task, note=note)[0] is True
    assert eligibility(task, unattended=True, note=note)[0] is False
    assert eligibility(task, unattended=True)[0] is True


def test_dispatchable_refuses_a_parked_row(tmp_path: Path) -> None:
    root = _repo(tmp_path, MARKED_PLAN, {"CBN-011": _note("blocked")})
    tasks = parse_plan(MARKED_PLAN)
    notes = list_open_tasknotes(root)
    assert dispatchable(tasks, "CBN-011", unattended=True, notes=notes) is False
    assert dispatchable(tasks, "CBN-012", unattended=True, notes=notes) is True


def test_archive_tasknotes_are_ignored(tmp_path: Path) -> None:
    root = _repo(tmp_path, MINIMAL)
    archive = root / ".flaitron" / "tasknote" / "archive" / "cbn"
    archive.mkdir(parents=True)
    (archive / "CBN-010.md").write_text(_note("completed"), encoding="utf-8")
    (root / ".flaitron" / "tasknote" / "CBN-011.md").write_text(
        _note("in-progress"), encoding="utf-8"
    )
    notes = list_open_tasknotes(root)
    assert [n.task_id for n in notes] == ["CBN-011"]


def test_tasknote_vanished_between_glob_and_read_is_skipped(tmp_path: Path) -> None:
    """CBN-168: a note present at glob time and gone at read time drops out of the poll.

    A dangling symlink is the faithful stand-in — `glob` lists it, `read_text`
    raises `FileNotFoundError` — for the worker's closure `git mv` landing
    between the two. Before this fix the exception escaped `read_project` and
    crashed the orchestrator mid-run, taking a finished worker down with it.
    """
    root = _repo(tmp_path, MINIMAL, {"CBN-011": _note("in-progress")})
    (root / ".flaitron" / "tasknote" / "CBN-012.md").symlink_to(tmp_path / "moved-away.md")
    notes = list_open_tasknotes(root)
    assert [n.task_id for n in notes] == ["CBN-011"]
    assert read_project(root).candidate is not None


def test_malformed_frontmatter_yaml_falls_back_to_the_status_line(tmp_path: Path) -> None:
    """CBN-186: unparseable YAML costs the broken key, not the note."""
    root = _repo(
        tmp_path,
        MINIMAL,
        {
            "CBN-011": _note("in-progress"),
            "CBN-012": "---\ntitle: [unclosed\nstatus: in-progress\n---\n\n# body\n",
        },
    )
    notes = list_open_tasknotes(root)
    assert [(n.task_id, n.status, n.park_reason) for n in notes] == [
        ("CBN-011", "in-progress", None),
        ("CBN-012", "in-progress", None),
    ]
    result = read_project(root)
    assert result.candidate is not None


@pytest.mark.parametrize(
    "frontmatter",
    [
        "title: [unclosed\ncreated: 2026-08-19",  # unparseable, no status line
        "- status: in-progress",  # parses, but not a mapping
        "title: t\nstatus: [unclosed",  # unparseable, status line itself is the breakage
    ],
)
def test_frontmatter_with_no_recoverable_status_is_not_open(
    tmp_path: Path, frontmatter: str
) -> None:
    """The CBN-EPIC-7.4 skip rule still holds where nothing says the note is open."""
    root = _repo(
        tmp_path,
        MINIMAL,
        {"CBN-011": _note("in-progress"), "CBN-012": f"---\n{frontmatter}\n---\n\n# body\n"},
    )
    assert [n.task_id for n in list_open_tasknotes(root)] == ["CBN-011"]


def test_missing_plan_is_user_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(UserError, match="missing") as exc:
        read_project(empty)
    assert exc.value.exit_code == 2


def test_non_utf8_plan_is_user_error(tmp_path: Path) -> None:
    """CBN-323: a decode failure is a refusal, not a raw `UnicodeDecodeError`."""
    root = _repo(tmp_path, MINIMAL)
    (root / ".flaitron" / "PLAN.md").write_bytes(b"## High\n\n- [ ] **CBN-001** \xff\xfe\n")
    with pytest.raises(UserError, match="not valid UTF-8") as exc:
        read_project(root)
    assert exc.value.exit_code == 2
    assert try_read_project(root) is None


def test_exhausted_when_only_future_and_completed(tmp_path: Path) -> None:
    plan = """## High

(none)

## Future Opportunities

- [ ] **CBN-050** [medium] | later — skipped.

## Completed

- [x] **CBN-010** [medium] | done — skipped.
"""
    result = read_project(_repo(tmp_path, plan))
    assert result.candidate is None
    assert result.suggested_next is None
    assert result.tasks  # rows exist; none eligible


def test_epic_stem() -> None:
    assert epic_stem("CBN-009.1") == "CBN-009"
    assert epic_stem("CBN-009.N") == "CBN-009"
    assert epic_stem("FE-132.3") == "FE-132"
    assert epic_stem("CBN-011") is None
    assert epic_stem("CBN-EPIC-009") is None


def test_epic_stem_handles_both_child_forms() -> None:
    """CBN-EPIC-9.5 F6. `AREA-EPIC-N.sub` used to return None, dropping it from its cohort."""
    assert epic_stem("CBN-009.1") == "CBN-009"
    assert epic_stem("DOC-20.3") == "DOC-20"
    assert epic_stem("CBN-EPIC-9.1") == "CBN-EPIC-9"
    # The reserved terminal audit child is a normal subtask (SPEC/epic.md) — it
    # belongs to the cohort, so it carries a stem like any other child.
    assert epic_stem("CBN-EPIC-9.N") == "CBN-EPIC-9"
    assert epic_stem("DOC-20.N") == "DOC-20"
    assert epic_stem("CBN-011") is None
    assert epic_stem("CBN-EPIC-9") is None
    assert epic_stem("DOC-EPIC-20") is None


def test_epic_parent_folds_both_child_forms_onto_the_plan_row() -> None:
    """CBN-EPIC-9.5 F6. Rollup cohort keying reads the same cohort `epic_stem` derives."""
    assert epic_parent("DOC-20.1") == "DOC-EPIC-20"
    assert epic_parent("DOC-EPIC-20.1") == "DOC-EPIC-20"
    assert epic_parent("CBN-EPIC-9.2") == "CBN-EPIC-9"
    assert epic_parent("DOC-19") is None
    assert epic_parent("DOC-EPIC-20") is None


def test_park_reason_is_read_verbatim_off_a_parked_note(tmp_path: Path) -> None:
    """`OpenTasknote` mirrors the frontmatter; splitting is the caller's job."""
    raw = "destructive — needs a `git push` before the tag can be verified"
    root = _repo(tmp_path, MINIMAL, {"CBN-011": _parked(raw)})
    notes = list_open_tasknotes(root)
    assert [n.park_reason for n in notes] == [raw]
    result = read_project(root)
    assert result.candidate is not None
    assert result.candidate.tasknote is not None
    assert result.candidate.tasknote.park_reason == raw


def test_park_reason_prose_with_a_colon_keeps_the_park(tmp_path: Path) -> None:
    """CBN-186: a fleet repo's APP-74 shape — `: ` in unquoted prose breaks YAML.

    Before the line-wise fallback the note vanished from the open set and
    `inspect_close` recorded `worker-exited` instead of `worker-parked`.
    """
    raw = "visual-confirm — Phase 3 👁️ CONFIRM: header renders"
    root = _repo(tmp_path, MINIMAL, {"CBN-011": _parked(raw)})
    notes = list_open_tasknotes(root)
    assert [(n.status, n.park_reason) for n in notes] == [("blocked", raw)]


def test_note_without_park_reason_reads_none(tmp_path: Path) -> None:
    root = _repo(tmp_path, MINIMAL, {"CBN-011": _note("blocked")})
    assert list_open_tasknotes(root)[0].park_reason is None


@pytest.mark.parametrize("value", ["[]", '""', "'   '"])
def test_non_string_or_blank_park_reason_costs_only_the_reason(tmp_path: Path, value: str) -> None:
    """Skip-not-fail one step further than `status`: the note still counts."""
    root = _repo(tmp_path, MINIMAL, {"CBN-011": _parked(value)})
    notes = list_open_tasknotes(root)
    assert [n.task_id for n in notes] == ["CBN-011"]
    assert notes[0].status == "blocked"
    assert notes[0].park_reason is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("destructive — needs a push", ("destructive", "needs a push")),
        ("drift - hyphen typed by hand", ("drift", "hyphen typed by hand")),
        ("model-mismatch", ("model-mismatch", None)),
        ("  dependency  ", ("dependency", None)),
        ("input-needed — a — b", ("input-needed", "a — b")),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_split_park_reason(raw: str | None, expected: tuple[str | None, str | None]) -> None:
    """Split on the *first* separator only; a bare value is a bare code."""
    assert split_park_reason(raw) == expected


def _pin(tmp_path: Path, *, module: bool = True, skill: str | None = None) -> Path:
    """Target repo with a pinned `/ft-task` shaped by the two probe halves."""
    root = tmp_path / "target"
    skill_dir = root / FT_TASK_SKILL_DIR_REL
    skill_dir.mkdir(parents=True)
    if module:
        (skill_dir / UNATTENDED_MODULE).write_text("posture\n", encoding="utf-8")
    if skill is not None:
        (skill_dir / "SKILL.md").write_text(skill, encoding="utf-8")
    return root


_CAPABLE_SKILL = "Trailing `--fast` / `-f`, `--debug`, and `--unattended` flags.\n"
_SUB_FLOOR_SKILL = "Trailing `--fast` / `-f` and `--debug` / `-d` flags.\n"


def test_supports_unattended_capable(tmp_path: Path) -> None:
    assert supports_unattended(_pin(tmp_path, skill=_CAPABLE_SKILL)) is True


def test_supports_unattended_needs_the_token(tmp_path: Path) -> None:
    """v5.19.0's shape: the module could exist, but the arg parser is what decides."""
    assert supports_unattended(_pin(tmp_path, skill=_SUB_FLOOR_SKILL)) is False


def test_supports_unattended_needs_the_module(tmp_path: Path) -> None:
    """The token alone would parse into a module the skill then cannot read."""
    assert supports_unattended(_pin(tmp_path, module=False, skill=_CAPABLE_SKILL)) is False


def test_supports_unattended_missing_skill_file(tmp_path: Path) -> None:
    assert supports_unattended(_pin(tmp_path, skill=None)) is False


def test_supports_unattended_no_flaitron_core(tmp_path: Path) -> None:
    """Skip-not-fail: a target with no submodule and no self-layout answers False."""
    bare = tmp_path / "bare"
    (bare / ".flaitron").mkdir(parents=True)
    assert supports_unattended(bare) is False


def _self_pin(tmp_path: Path, *, module: bool = True, skill: str | None = None) -> Path:
    """flaitron-self layout: skills at `claude/skills/ft-task/`, no `.flaitron/core`."""
    root = tmp_path / "flaitron-self"
    skill_dir = root / SELF_FT_TASK_SKILL_DIR_REL
    skill_dir.mkdir(parents=True)
    if module:
        (skill_dir / UNATTENDED_MODULE).write_text("posture\n", encoding="utf-8")
    if skill is not None:
        (skill_dir / "SKILL.md").write_text(skill, encoding="utf-8")
    return root


def test_supports_unattended_self_layout_capable(tmp_path: Path) -> None:
    """CBN-156: no submodule → read `claude/skills/ft-task/`."""
    assert supports_unattended(_self_pin(tmp_path, skill=_CAPABLE_SKILL)) is True


def test_supports_unattended_self_layout_needs_the_module(tmp_path: Path) -> None:
    assert supports_unattended(_self_pin(tmp_path, module=False, skill=_CAPABLE_SKILL)) is False


def test_unattended_module_rel_follows_the_probe(tmp_path: Path) -> None:
    """CBN-189: the path the continue prompt names is the one the probe read.

    Pinned adopter → the submodule path; core-less flaitron-self → the repo's
    own `claude/skills/ft-task/`. Relative to the root, since the worker runs
    there, and resolved by the same gate as `supports_unattended` so the two
    cannot disagree.
    """
    pinned = _pin(tmp_path, skill=_CAPABLE_SKILL)
    assert unattended_module_rel(pinned) == FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE
    assert not unattended_module_rel(pinned).is_absolute()
    self_hosted = _self_pin(tmp_path, skill=_CAPABLE_SKILL)
    assert unattended_module_rel(self_hosted) == SELF_FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE


def test_pinned_skill_rel_names_only_a_skill_on_disk(tmp_path: Path) -> None:
    """CBN-291: the fresh-launch cue names the probed SKILL.md, or nothing.

    Same gate as `unattended_module_rel`, plus the existence check it skips:
    the cue rides the `--fast` degrade too, where the probe may find nothing.
    """
    pinned = _pin(tmp_path, skill=_CAPABLE_SKILL)
    assert pinned_skill_rel(pinned) == FT_TASK_SKILL_DIR_REL / "SKILL.md"
    self_hosted = _self_pin(tmp_path, skill=_CAPABLE_SKILL)
    assert pinned_skill_rel(self_hosted) == SELF_FT_TASK_SKILL_DIR_REL / "SKILL.md"
    bare = tmp_path / "bare"
    bare.mkdir()
    assert pinned_skill_rel(bare) is None
    # An empty core is still "present" (CBN-156), so it never falls back.
    (bare / CORE_REL).mkdir(parents=True)
    assert pinned_skill_rel(bare) is None


def test_supports_unattended_core_present_does_not_fall_back(tmp_path: Path) -> None:
    """An adopter with a core keeps the pin even if self-layout skills are capable."""
    root = _self_pin(tmp_path, skill=_CAPABLE_SKILL)
    (root / CORE_REL).mkdir(parents=True)
    assert supports_unattended(root) is False


def test_supports_unattended_undecodable_skill(tmp_path: Path) -> None:
    """A binary SKILL.md degrades to False rather than propagating a decode error."""
    root = _pin(tmp_path)
    (root / FT_TASK_SKILL_DIR_REL / "SKILL.md").write_bytes(b"\xfe\xff\x00--unattended")
    assert supports_unattended(root) is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_pin_degrades_instead_of_raising(tmp_path: Path) -> None:
    """CBN-344: a `PermissionError` on the core probe is skip-not-fail, not a crash."""
    root = _pin(tmp_path, skill=_CAPABLE_SKILL)
    flaitron_dir = root / ".flaitron"
    flaitron_dir.chmod(0o000)
    try:
        assert supports_unattended(root) is False
        assert pinned_skill_rel(root) is None
        # Unreadable core is still a present pin: no fall-back to the self-layout.
        assert unattended_module_rel(root) == FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE
    finally:
        flaitron_dir.chmod(0o755)


def test_core_under_a_file_counts_as_absent(tmp_path: Path) -> None:
    """CBN-347: `NotADirectoryError` from the core probe is absence, not an unreadable pin."""
    root = _self_pin(tmp_path, skill=_CAPABLE_SKILL)
    (root / ".flaitron").write_text("not a directory\n", encoding="utf-8")
    assert unattended_module_rel(root) == SELF_FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE
    assert supports_unattended(root) is True


# --- Unattended-safety marker + eligibility dimension (CBN-EPIC-17.2) ---

# The exploratory dimension's verbatim reason (CBN-114), asserted from several
# tests — pinned once so a reword cannot pass by being changed in only one.
XHEAVY_REASON = (
    "exploratory — [xheavy] is manual-only (SPEC/model.md); "
    "caobunga caps dispatch at [heavy], stricter than flaitron's [frontier]"
)

SAFETY = """# sample

## High

- [ ] **CBN-EPIC-30** [heavy] | parent — tracking row.
- [ ] **CBN-101** [medium] | unmarked — needs an operator.
- [ ] **CBN-102** [medium] [unattended] | marked — safe unwatched.
- [ ] **CBN-104** [xheavy] [unattended] | exploratory — marked, but manual-only.

## Medium

(none)

## Low

(none)

## Future Opportunities

- [ ] **CBN-200** [medium] [unattended] | future — marker does not launch a bucket.

## Completed

- [x] **CBN-100** [medium] | done — Completed 2026-08-19.
"""


def test_marker_parses_after_model() -> None:
    by_id = {t.task_id: t for t in parse_plan(SAFETY)}
    assert by_id["CBN-102"].unattended is True
    assert by_id["CBN-102"].model == "medium"
    assert by_id["CBN-101"].unattended is False


def test_marker_parses_among_stacked_extra_tokens() -> None:
    md = "## High\n\n- [ ] **CBN-103** [heavy] [foo] [unattended] | t — stacked run.\n"
    (task,) = parse_plan(md)
    assert task.model == "heavy"
    assert task.unattended is True


def test_footgun_leading_bang_token_drops_the_whole_line() -> None:
    """`[!unattended]` matches no TASK_LINE slot — the task silently vanishes.

    Pinned so the marker's documented grammar ("lowercase, after [model]") stays
    honest: if a future grammar change makes this parse, the docs and CBN-EPIC-17.4's
    upstream filing must move with it.
    """
    md = (
        "## High\n\n"
        "- [ ] **CBN-104** [medium] [!unattended] | bad — leading bang.\n"
        "- [ ] **CBN-105** [medium] | control — parses fine.\n"
    )
    assert [t.task_id for t in parse_plan(md)] == ["CBN-105"]


def test_footgun_marker_before_or_without_model_is_captured_as_model() -> None:
    """Placed before (or without) `[model]`, the marker IS the model token.

    It then routes effort (taskmap buckets unknown tokens to MEDIUM) instead of
    gating safety — silently. Pinned for the same doc-honesty reason as above.
    """
    md = (
        "## High\n\n"
        "- [ ] **CBN-106** [unattended] [heavy] | swapped — marker first.\n"
        "- [ ] **CBN-107** [unattended] | bare — no model at all.\n"
    )
    by_id = {t.task_id: t for t in parse_plan(md)}
    assert by_id["CBN-106"].model == "unattended"
    assert by_id["CBN-106"].unattended is False
    assert by_id["CBN-107"].model == "unattended"
    assert by_id["CBN-107"].unattended is False


def test_eligibility_reasons_cover_every_branch() -> None:
    by_id = {t.task_id: t for t in parse_plan(SAFETY)}
    assert eligibility(by_id["CBN-101"]) == (
        True,
        "attended — this read does not consult the marker",
    )
    assert eligibility(by_id["CBN-102"], unattended=True) == (
        True,
        "marked — [unattended] on the PLAN line",
    )
    assert eligibility(by_id["CBN-101"], unattended=True) == (
        False,
        "unmarked — no [unattended] marker on the PLAN line",
    )
    # The exploratory dimension outranks the marker: CBN-104 *is* marked, and is
    # still denied, because seeding is not the fix for an `[xheavy]` row (CBN-114).
    assert eligibility(by_id["CBN-104"], unattended=True) == (False, XHEAVY_REASON)
    assert eligibility(by_id["CBN-104"]) == (
        True,
        "attended — this read does not consult the marker",
    )
    assert eligibility(by_id["CBN-100"], unattended=True) == (False, "completed — row is checked")
    assert eligibility(by_id["CBN-200"], unattended=True) == (
        False,
        "bucket — Future Opportunities is outside the launch buckets",
    )
    assert eligibility(by_id["CBN-EPIC-30"], unattended=True) == (
        False,
        "parent-epic — tracking row; children launch instead",
    )


def test_pick_next_unattended_filters_to_marked_tasks() -> None:
    tasks = parse_plan(SAFETY)
    attended = pick_next(tasks)
    assert attended is not None
    assert attended.task_id == "CBN-101"
    assert attended.eligibility_reason == "attended — this read does not consult the marker"
    unattended = pick_next(tasks, unattended=True)
    assert unattended is not None
    assert unattended.task_id == "CBN-102"
    assert unattended.eligibility_reason == "marked — [unattended] on the PLAN line"


def test_read_project_unattended_lists_safety_denials_only(tmp_path: Path) -> None:
    """`denied` carries the safety denials; routine exclusions never appear."""
    repo = _repo(tmp_path, SAFETY)
    read = read_project(repo, unattended=True)
    assert read.candidate is not None
    assert read.candidate.task_id == "CBN-102"
    assert [(d.task_id, d.reason) for d in read.denied] == [
        ("CBN-101", "unmarked — no [unattended] marker on the PLAN line"),
        ("CBN-104", XHEAVY_REASON),
    ]


def test_read_project_attended_default_never_denies(tmp_path: Path) -> None:
    repo = _repo(tmp_path, SAFETY)
    read = read_project(repo)
    assert read.candidate is not None
    assert read.candidate.task_id == "CBN-101"
    assert read.denied == []


MARKED_PLAN = """# sample

## High

- [ ] **CBN-011** [medium] [unattended] | flaitron-reader — reader work.
- [ ] **CBN-012** [medium] [unattended] | cao-client — client work.
"""

# The override's target is the *unmarked* row here, so a posture that requires
# the marker must fall through to spec-selection rather than prefer it (CBN-118).
MIXED_OVERRIDE_PLAN = """# sample

## High

- [ ] **CBN-011** [medium] [unattended] | flaitron-reader — reader work.
- [ ] **CBN-012** [medium] | cao-client — needs an operator.
"""


def test_drain_pick_drops_the_in_progress_override(tmp_path: Path) -> None:
    """The operator's open note is their work; an unwatched drain must not take it.

    Non-launching reads are unchanged — `observe` / the API / the brief all read
    attended (CBN-EPIC-17.3).
    """
    root = _repo(tmp_path, MARKED_PLAN, {"CBN-012": _note("in-progress")})

    attended = read_project(root)
    assert attended.candidate is not None
    assert attended.candidate.task_id == "CBN-012"
    assert attended.candidate.reason == "in-progress-override"

    drain = read_project(root, unattended=True, drain=True)
    assert drain.candidate is not None
    assert drain.candidate.task_id == "CBN-011"
    assert drain.candidate.reason == "spec-selection"


def test_gated_posture_keeps_the_override_while_requiring_the_marker(tmp_path: Path) -> None:
    """The CBN-118 split, proven on one fixture: two dimensions, two tier sets.

    `gated` is headless (marker required) but watched (override kept), so it must
    land on neither of the other two postures' answers. `CBN-012` carries the
    marker here, so the override survives the safety dimension it now passes
    through — which is the half a merged flag could not express.
    """
    root = _repo(tmp_path, MARKED_PLAN, {"CBN-012": _note("in-progress")})

    gated = read_project(root, unattended=True)
    assert gated.candidate is not None
    assert gated.candidate.task_id == "CBN-012"
    assert gated.candidate.reason == "in-progress-override"
    assert gated.candidate.eligibility_reason == "marked — [unattended] on the PLAN line"


def test_gated_posture_declines_an_unmarked_open_note(tmp_path: Path) -> None:
    """The override is a dispatch preference, so it is subject to the marker.

    Preferring an unmarked note would let the override smuggle past the very
    dimension CBN-118 added — the override picks among *eligible* rows, it does
    not exempt one.
    """
    root = _repo(tmp_path, MIXED_OVERRIDE_PLAN, {"CBN-012": _note("in-progress")})

    attended = read_project(root)
    assert attended.candidate is not None
    assert attended.candidate.task_id == "CBN-012"
    assert attended.candidate.reason == "in-progress-override"

    gated = read_project(root, unattended=True)
    assert gated.candidate is not None
    assert gated.candidate.task_id == "CBN-011"
    assert gated.candidate.reason == "spec-selection"
    assert [d.task_id for d in gated.denied] == ["CBN-012"]


def test_drain_without_unattended_is_unrepresentable(tmp_path: Path) -> None:
    """A drain is headless by definition; the incoherent pairing raises.

    Accepting it would silently reinstate the CBN-EPIC-17.3 race — the same
    footgun `worker_message` guards with a required keyword.
    """
    root = _repo(tmp_path, MARKED_PLAN, {})
    with pytest.raises(ValueError, match="drain implies unattended"):
        read_project(root, drain=True)
    with pytest.raises(ValueError, match="drain implies unattended"):
        pick_next(parse_plan(MARKED_PLAN), drain=True)


def test_in_flight_is_reported_on_every_posture(tmp_path: Path) -> None:
    """The display half survives the dispatch half being dropped.

    `api.current_task` reads this field, and it is the only durable current-task
    signal the dashboard has — run records are written at close (CBN-040, from
    CBN-011). Reading `candidate` instead would report no current task while the
    operator was working on one.
    """
    root = _repo(tmp_path, MARKED_PLAN, {"CBN-012": _note("in-progress")})
    postures = (
        read_project(root),
        read_project(root, unattended=True),
        read_project(root, unattended=True, drain=True),
    )
    for read in postures:
        assert read.in_flight is not None
        assert read.in_flight.task_id == "CBN-012"
        assert read.in_flight.reason == "in-progress-override"
        assert read.in_flight.tasknote is not None
        assert read.in_flight.tasknote.status == "in-progress"


def test_in_flight_is_none_without_an_in_progress_note(tmp_path: Path) -> None:
    root = _repo(tmp_path, MARKED_PLAN, {"CBN-012": _note("starter")})
    assert read_project(root).in_flight is None
    assert in_flight_task(parse_plan(MARKED_PLAN)) is None


def test_unattended_still_carries_a_stranded_note_at_the_spec_winner(tmp_path: Path) -> None:
    """CBN-109 survives the override drop.

    A worker that died mid-task left PLAN untouched, so spec-selection returns
    the same id it returned when the task was dispatched — and the candidate
    still carries the open note, which is what makes `worker_message` send the
    continue prompt rather than a start-only `/ft-task`.
    """
    root = _repo(tmp_path, MARKED_PLAN, {"CBN-011": _note("in-progress")})
    read = read_project(root, unattended=True, drain=True)
    assert read.candidate is not None
    assert read.candidate.task_id == "CBN-011"
    assert read.candidate.reason == "spec-selection"
    assert read.candidate.tasknote is not None
    assert read.candidate.tasknote.status == "in-progress"


COVERAGE_PLAN = """# sample

## High

- [ ] **CBN-001** [medium] [unattended] | marked — carries the marker.
- [ ] **CBN-002** [medium] | unmarked — no marker.
- [x] **CBN-004** [medium] | done — Completed 2026-08-19.
- [ ] **CBN-EPIC-5** [heavy] | tracking — parent row, never launched.

## Medium

- [ ] **CBN-003** [light] | also-unmarked — no marker either.

## Future Opportunities

- [ ] **CBN-009** [medium] | someday — outside the launch buckets.
"""


def test_coverage_counts_only_the_open_launch_queue() -> None:
    """`marked + denied == open`, over the base-eligible rows and nothing else.

    The three base dimensions exclude a checked row, a `## Future Opportunities`
    row, and a parent-epic tracking row. Keeping them out is what makes the
    figure a seeding worklist rather than a PLAN census — a tracking row is not
    something an operator can mark.
    """
    result = coverage(parse_plan(COVERAGE_PLAN))
    assert (result.open, result.marked, result.denied) == (3, 1, 2)
    assert result.marked + result.denied == result.open
    assert [denial.task_id for denial in result.denials] == ["CBN-002", "CBN-003"]
    assert all(denial.reason.startswith("unmarked — ") for denial in result.denials)


def test_coverage_is_computed_on_every_read_regardless_of_posture(tmp_path: Path) -> None:
    """Coverage reports on the repo, not on this caller's dispatch posture.

    `denied` is posture-dependent — empty on an attended read by contract — so
    the dashboard and `caobunga status`, which never pass `unattended=True`,
    would show nothing at all if coverage rode on that field.
    """
    root = _repo(tmp_path, COVERAGE_PLAN)
    attended = read_project(root)
    unattended = read_project(root, unattended=True)
    assert attended.denied == []
    assert attended.coverage == unattended.coverage
    assert attended.coverage.marked == 1


def test_coverage_is_empty_for_a_plan_with_no_open_rows() -> None:
    empty = coverage(parse_plan("# sample\n\n## High\n\n(none)\n"))
    assert (empty.open, empty.marked, empty.denied) == (0, 0, 0)
    assert empty.denials == []


# The glyph-then-marker shape (upstream CORE-502) parses since CBN-167 —
# `SUGGESTION_GLYPH` now has a slot on either side of `TRAILING_TOKENS`. The
# shape that still drops is `[!unattended]`, which matches no fragment of the
# grammar at all (SPEC.md §"Task-line format" documents it as a footgun no
# tolerance covers). Detection must flag a broken row whether checked or
# unchecked (CBN-210); beside them, it must NOT flag: a prose bullet, a
# prose *checklist* item (a checkbox with no `*` — CBN-209, the reference
# parser's FE-087 gate), a fenced example, and a broken row outside the
# launch buckets.
UNPARSED_PLAN = """# sample

## High

- [ ] **CBN-001** [medium] [unattended] | marked — carries the marker.
- [ ] **CBN-002** [xheavy] [!unattended] | bang-shape — dropped by the grammar.
- [x] **CBN-004** [xheavy] [!unattended] | checked-broken — still flagged.
- [x] **CBN-005** Completed 2026-09-20.
- a plain prose bullet, not a task row at all.
- [ ] a prose checklist item: a checkbox, no emphasis, not a task row either.
- [x] done prose checklist item, also not a task row.

## Medium

```text
- [ ] **CBN-777** [xheavy] [!unattended] | fenced — an example, not a row.
```

## Future Opportunities

- [ ] **CBN-009** [xheavy] [!unattended] | future-broken — outside the launch buckets.
"""


def test_unparsed_rows_name_the_dropped_line_by_number_and_text() -> None:
    """The id never parsed, so line number + raw text is the only handle (CBN-115).

    A checked row failing `TASK_LINE` is flagged too (CBN-210): it's a landed
    close, not an open seed target, but a caller still needs the handle on it.
    """
    rows = unparsed_plan_rows(UNPARSED_PLAN)
    assert len(rows) == 3
    assert rows[0].line == 6
    assert rows[0].text.startswith("- [ ] **CBN-002** [xheavy] [!unattended]")
    assert rows[1].line == 7
    assert rows[1].text.startswith("- [x] **CBN-004** [xheavy] [!unattended]")
    # The stub flaitron CORE-632.2's unattended close actually wrote (2026-09-20):
    # `| shortname` dropped, so SPEC's stub form is violated and `parse_plan`
    # never sees the tick — the row `inspect_close` then read as `worker-exited`.
    assert rows[2].line == 8
    assert rows[2].text == "- [x] **CBN-005** Completed 2026-09-20."


def test_unparsed_rows_skip_everything_that_is_not_a_seeding_target() -> None:
    """Prose bullets and checklist items, fenced examples, and non-launch buckets don't count."""
    only_noise = (
        UNPARSED_PLAN.replace(
            "- [ ] **CBN-002** [xheavy] [!unattended] | bang-shape — dropped by the grammar.\n",
            "",
        )
        .replace(
            "- [x] **CBN-004** [xheavy] [!unattended] | checked-broken — still flagged.\n",
            "",
        )
        .replace("- [x] **CBN-005** Completed 2026-09-20.\n", "")
    )
    assert unparsed_plan_rows(only_noise) == []


def test_glyph_then_marker_now_parses_since_cbn_167() -> None:
    """CBN-167 reversed CBN-127: the once-dropped shape now parses cleanly."""
    plan = "## High\n\n- [ ] **CBN-005** [xheavy]🔭 [unattended] | reversed — now parses.\n"
    tasks = parse_plan(plan)
    assert len(tasks) == 1
    assert tasks[0].unattended is True
    assert unparsed_plan_rows(plan) == []


def test_both_glyph_placements_parse_to_identical_fields() -> None:
    """The two placements SPEC §"Task-line format" calls equivalent read as one row.

    Only `text` (the raw line) and `line` may differ between them; every parsed
    field — id, model, marker, shortname, description — must be byte-equal, or the
    second glyph slot has changed what a row *means* rather than where a glyph sits.
    """
    before = "- [ ] **CBN-006** [light]🔧 [unattended] | glyphed — same row, two spellings."
    after = "- [ ] **CBN-006** [light] [unattended]🔧 | glyphed — same row, two spellings."
    parsed = [parse_plan(f"## High\n\n{row}\n")[0] for row in (before, after)]
    assert parsed[0].text != parsed[1].text
    fields = [t.model_dump(exclude={"text", "line"}) for t in parsed]
    assert fields[0] == fields[1]
    assert fields[0]["unattended"] is True
    assert fields[0]["model"] == "light"


def test_coverage_surfaces_the_seeding_blindspot(tmp_path: Path) -> None:
    """A row seeded into a shape the grammar still rejects doesn't yield a clean bill of health.

    The CBN-115 filing measured this against the glyph-then-marker shape, which
    CBN-167 has since fixed (both glyph placements now parse — see
    `test_glyph_then_marker_now_parses_since_cbn_167`). The remaining blind spot
    is `[!unattended]`: it still fails `TASK_LINE` entirely, so the report would
    read `open=2 marked=2 denied=0` — fully covered, one row silently gone,
    `marked + denied == open` intact — without `unparsed` making the drop
    visible on the same read.
    """
    seeded = """# sample

## High

- [ ] **CBN-001** [medium] [unattended] | ok — marker on a glyph-free row.
- [ ] **CBN-002** [heavy]🧠 [unattended] | ok — glyph-then-marker parses since CBN-167.
- [ ] **CBN-003** [heavy] [!unattended] | dropped — the bang-shape still fails.
"""
    read = read_project(_repo(tmp_path, seeded))
    assert (read.coverage.open, read.coverage.marked, read.coverage.denied) == (2, 2, 0)
    assert read.coverage.unparsed == 1
    assert read.coverage.unparsed_rows[0].line == 7
    assert read.coverage.marked + read.coverage.denied == read.coverage.open


def test_coverage_without_unparsed_rows_defaults_to_zero() -> None:
    """`coverage(tasks)` alone stays valid — the tally is byte-identical to before."""
    result = coverage(parse_plan(COVERAGE_PLAN))
    assert result.unparsed == 0
    assert result.unparsed_rows == []


def test_coverage_names_the_marked_ids_it_counted() -> None:
    """`marked_tasks` is the detail half of `marked`, in the same dispatch order."""
    result = coverage(parse_plan(COVERAGE_PLAN))
    assert result.marked_tasks == ["CBN-001"]
    assert len(result.marked_tasks) == result.marked


# The evaporation shape, verbatim from the CBN-120.2 observation: a line rewrite
# that keeps the task text and drops the trailing bracket token, plus a stale
# `Blocked by` clause arriving in the same edit. `CBN-001` is the drifted row,
# `CBN-002` was never marked, `CBN-006` is still armed.
DRIFTED_PLAN = """# sample

## High

- [ ] **CBN-001** [medium] | disarmed — the rewrite dropped the marker.
- [ ] **CBN-002** [medium] | unmarked — never marked, never in the baseline.
- [ ] **CBN-006** [medium] [unattended] | intact — still armed.
"""


def test_marker_drift_reports_a_row_that_lost_its_marker() -> None:
    """The guard's whole job: armed at the last run, still here, not armed now."""
    drift = marker_drift(parse_plan(DRIFTED_PLAN), ["CBN-001", "CBN-006"])
    assert [d.task_id for d in drift] == ["CBN-001"]
    assert drift[0].reason.startswith("unmarked — ")
    # Actionable like every other `Denial` (CBN-119.3), from the one constructor.
    assert drift[0].line == 5
    assert drift[0].text.startswith("- [ ] **CBN-001**")
    assert drift[0].seeded_line is not None
    assert "[medium] [unattended]" in drift[0].seeded_line


def test_marker_drift_catches_the_stale_clause_that_arrives_in_the_same_edit() -> None:
    """The verdict is `eligibility`, not the marker token alone.

    CBN-120.2's rewrite dropped markers *and* added already-satisfied `Blocked
    by` clauses. A marker-only test would report half the damage.
    """
    plan = """# sample

## High

- [ ] **CBN-001** [medium] [unattended] | clause — Blocked by [[CBN-002]] arrived in the rewrite.
- [x] **CBN-002** [medium] | done — Completed 2026-09-05.
"""
    drift = marker_drift(parse_plan(plan), ["CBN-001"])
    assert [d.task_id for d in drift] == ["CBN-001"]
    assert drift[0].reason.startswith("blocked — ")
    # Seeding does not fix a clause, so no marker is offered (CBN-114 ordering).
    assert drift[0].seeded_line is None


def test_marker_drift_ignores_a_row_that_simply_closed() -> None:
    """A drained cohort is not a disarmed one — the queue-membership scope."""
    plan = """# sample

## High

- [x] **CBN-001** [medium] [unattended] | done — Completed 2026-09-05.
"""
    assert marker_drift(parse_plan(plan), ["CBN-001"]) == []


def test_marker_drift_ignores_a_row_that_left_the_open_queue() -> None:
    """Rotation to PLAN-ARCHIVE.md and re-prioritization are operator acts, not drift.

    A row absent from PLAN — rotated out of `## Completed` into a file this
    reader never opens — would otherwise report as evaporated forever. Scoping
    the diff to queue membership drops both by construction.
    """
    rotated = parse_plan("# sample\n\n## High\n\n(none)\n")
    assert marker_drift(rotated, ["CBN-001"]) == []

    moved = """# sample

## Future Opportunities

- [ ] **CBN-001** [medium] | someday — outside the launch buckets now.
"""
    assert marker_drift(parse_plan(moved), ["CBN-001"]) == []


def test_marker_drift_is_empty_without_a_baseline() -> None:
    """No run record carries a marked set: nothing to diff, nothing claimed."""
    tasks = parse_plan(DRIFTED_PLAN)
    assert marker_drift(tasks, None) == []
    assert marker_drift(tasks, []) == []


def test_marker_drift_is_ordered_like_every_other_denial_list() -> None:
    """Dispatch order, from `_selection_key` — two lists of one DTO must agree."""
    plan = """# sample

## Medium

- [ ] **CBN-020** [medium] | second — Medium sorts after High.

## High

- [ ] **CBN-030** [medium] | first — High wins despite the higher id.
"""
    drift = marker_drift(parse_plan(plan), ["CBN-020", "CBN-030"])
    assert [d.task_id for d in drift] == ["CBN-030", "CBN-020"]


def test_try_read_project_returns_none_for_a_non_flaitron_path(tmp_path: Path) -> None:
    """Skip-not-fail: one unreadable registered path costs one row, not the report."""
    missing = tmp_path / "gone"
    missing.mkdir()
    assert try_read_project(missing) is None
    assert try_read_project(tmp_path / "never-existed") is None


@pytest.mark.parametrize("error", [PermissionError("denied"), FileNotFoundError("raced")])
def test_try_read_project_degrades_oserror_but_read_project_raises_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    """CBN-325: an `OSError` on the PLAN read is None for the reports, raw for `wait.poll`."""
    root = _repo(tmp_path, MINIMAL)
    real = Path.read_text

    def deny_plan(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "PLAN.md":
            raise error
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_plan)
    with pytest.raises(type(error)):
        read_project(root)
    assert try_read_project(root) is None


# --- `Blocked by [[ID]]` don't-start gate (CBN-116) -------------------------
#
# The parser half is a port of `viz/src/parser.ts` `extractBlockedBy`, so these
# mirror the upstream cases in `viz/src/parser.test.ts` one for one — including
# the two negatives that define the contract's edges (bare ID, code span).


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Execute migration. Blocked by [[CBN-008]] — wait for backlog.", ["CBN-008"]),
        ("Run it. Blocked by [[CBN-008]], [[CBN-007]] — needs both.", ["CBN-008", "CBN-007"]),
        ("Builds on [[CBN-001]]. Blocked by [[CBN-008]] — wait.", ["CBN-008"]),
        ("Extends [[CBN-008]] in spirit. Blocked by [[CBN-008]] until clear.", ["CBN-008"]),
        ("Blocked by [[CBN-008]] and again Blocked by [[CBN-008]].", ["CBN-008"]),
        ("Run migration. Blocked by: CBN-008 — bare ID must not parse.", []),
        ("Example: `Blocked by [[CBN-999]]` is literal code.", []),
        ("Plain prose with no cross-references at all.", []),
        ("", []),
    ],
)
def test_parse_blocked_by_matches_the_viz_parser(description: str, expected: list[str]) -> None:
    """Wikilink-only, comma lists, code spans stripped, deduped in source order."""
    assert parse_blocked_by(description) == expected


BLOCKED_PLAN = """# sample

## High

- [ ] **CBN-001** [medium] [unattended] | open-blocker — Blocked by [[CBN-050]] — not yet done.
- [ ] **CBN-002** [medium] [unattended] | stale-clause — Blocked by [[CBN-051]] — already done.
- [ ] **CBN-003** [medium] [unattended] | unknown-blocker — Blocked by [[CBN-900]] — absent.
- [ ] **CBN-004** [medium] [unattended] | mixed — Blocked by [[CBN-050]], [[CBN-051]] — one open.
- [ ] **CBN-005** [medium] [unattended] | clear — no clause at all.
- [ ] **CBN-050** [medium] | still-open — the live blocker.

## Completed

- [x] **CBN-051** [medium] | done — Completed 2026-08-29.
"""


def _blocked_tasks() -> dict[str, PlanTask]:
    return {task.task_id: task for task in parse_plan(BLOCKED_PLAN)}


def test_plan_task_carries_the_parsed_blockers() -> None:
    tasks = _blocked_tasks()
    assert tasks["CBN-004"].blocked_by == ["CBN-050", "CBN-051"]
    assert tasks["CBN-005"].blocked_by == []


@pytest.mark.parametrize("unattended", [False, True])
def test_blocked_gate_denies_on_every_posture(unattended: bool) -> None:
    """Flaitron scopes the clause to no tier, so `gated` honors it as a drain does."""
    tasks = _blocked_tasks()
    done = completed_task_ids(tasks.values())
    allow, reason = eligibility(tasks["CBN-001"], unattended=unattended, completed_ids=done)
    assert allow is False
    assert reason == "blocked — Blocked by [[CBN-050]]"


def test_stale_clause_still_denies_but_says_so() -> None:
    """Clause removal is the operator's act (`SPEC/blocked.md`), so this refuses.

    Auto-releasing would dispatch a row whose PLAN line literally reads "Blocked
    by" — the orchestrator overruling the operator's own text. Naming the
    staleness is what keeps the refusal cheap to clear.
    """
    tasks = _blocked_tasks()
    done = completed_task_ids(tasks.values())
    allow, reason = eligibility(tasks["CBN-002"], completed_ids=done)
    assert allow is False
    assert reason == "blocked — Blocked by [[CBN-051]] (completed; clause is stale, remove it)"


def test_unknown_blocker_reads_as_still_blocking() -> None:
    """An id absent from PLAN cannot be *confirmed* complete — bias-to-refuse."""
    tasks = _blocked_tasks()
    done = completed_task_ids(tasks.values())
    _, reason = eligibility(tasks["CBN-003"], completed_ids=done)
    assert reason == "blocked — Blocked by [[CBN-900]]"


def test_one_open_blocker_keeps_the_plain_reason() -> None:
    tasks = _blocked_tasks()
    done = completed_task_ids(tasks.values())
    _, reason = eligibility(tasks["CBN-004"], completed_ids=done)
    assert reason == "blocked — Blocked by [[CBN-050]], [[CBN-051]]"


def test_without_completed_ids_staleness_is_undetermined() -> None:
    """Omitting the set costs the annotation, never the gate."""
    allow, reason = eligibility(_blocked_tasks()["CBN-002"])
    assert allow is False
    assert reason == "blocked — Blocked by [[CBN-051]]"


@pytest.mark.parametrize("unattended", [False, True])
def test_pick_next_skips_blocked_rows(unattended: bool) -> None:
    """CBN-001..004 are all blocked; the first dispatchable row is CBN-005."""
    chosen = pick_next(parse_plan(BLOCKED_PLAN), unattended=unattended)
    assert chosen is not None
    assert chosen.task_id == "CBN-005"


def test_blocked_rows_stay_inside_the_coverage_queue() -> None:
    """`open` keeps its three membership dimensions; the gate denies within it.

    Shrinking `open` instead would let a repo read "fully covered" while still
    carrying rows to seed — the blind-spot class CBN-115 closed one dimension
    earlier.
    """
    result = coverage(parse_plan(BLOCKED_PLAN))
    assert result.open == 6
    assert result.marked + result.denied == result.open
    blocked = {d.task_id: d.reason for d in result.denials if d.reason.startswith("blocked — ")}
    assert set(blocked) == {"CBN-001", "CBN-002", "CBN-003", "CBN-004"}
    # CBN-050 is unblocked but unmarked — the safety dimension, not this one.
    assert result.marked == 1


def test_unattended_read_echoes_blocked_denials(tmp_path: Path) -> None:
    """A drain that skips a row must be able to say why (`read.denied` is the echo)."""
    read = read_project(_repo(tmp_path, BLOCKED_PLAN), unattended=True)
    reasons = {denial.task_id: denial.reason for denial in read.denied}
    assert reasons["CBN-001"] == "blocked — Blocked by [[CBN-050]]"
    assert reasons["CBN-050"].startswith("unmarked — ")
    assert read.candidate is not None
    assert read.candidate.task_id == "CBN-005"


def test_in_flight_still_reports_a_blocked_row_the_operator_has_open(tmp_path: Path) -> None:
    """Display answers "what is open", not "what may dispatch" (CBN-EPIC-17.3).

    The picker must refuse CBN-001, but blanking the dashboard's only durable
    current-task signal over the same clause is the blind spot that split
    `in_flight` off in the first place.
    """
    root = _repo(tmp_path, BLOCKED_PLAN, {"CBN-001": _note("in-progress")})
    read = read_project(root)
    assert read.in_flight is not None
    assert read.in_flight.task_id == "CBN-001"
    assert read.candidate is not None
    assert read.candidate.task_id == "CBN-005"


XHEAVY_PLAN = """# sample

## High

- [ ] **CBN-001** [xheavy] [unattended] | exploratory — marked, never dispatched.
- [ ] **CBN-002** [xheavy] [unattended]🔭 | glyphed — same rung, decorated.
- [ ] **CBN-003** [heavy] [unattended] | routine — the reachable head.
"""
# CBN-002's glyph trails the marker deliberately, to keep this fixture testing
# the xheavy denial rather than the grammar: the reverse order
# (`[xheavy]🔭 [unattended]`) used to be the shape `TASK_LINE` dropped entirely
# (caobunga's own CBN-115 finding, upstream flaitron CORE-502 / v5.23.0,
# declined here at CBN-127) but now parses too, since `CBN-167` mirrored it
# (see docs/decisions/flaitron-reader.md §"Parse contract — pin-move
# history"). Either order now reaches the xheavy denial this test exercises;
# the after-marker order is kept so a regression in either glyph slot doesn't
# silently shrink this fixture.


def test_xheavy_rows_never_dispatch() -> None:
    """`SPEC/model.md` reserves the exploratory rung to the operator (CBN-114).

    Both `[xheavy]` rows sort ahead of the `[heavy]` one, so a picker that did
    not deny them would dispatch CBN-001 — the "automated chooser picks up an
    `[xheavy]`-tagged task" the clause forbids.
    """
    chosen = pick_next(parse_plan(XHEAVY_PLAN), unattended=True)
    assert chosen is not None
    assert chosen.task_id == "CBN-003"


def test_xheavy_denial_outranks_the_marker_reason() -> None:
    """The reason must name the fix that exists, not the one that would not work.

    Every row here carries `[unattended]`, so a marker-first ordering would report
    them as eligible; an ordering that merely reordered the checks would still
    have to avoid `unmarked`, which would send an operator reading
    `Coverage.denials` to seed a marker that changes nothing.
    """
    result = coverage(parse_plan(XHEAVY_PLAN))
    assert (result.open, result.marked, result.denied) == (3, 1, 2)
    assert result.marked + result.denied == result.open
    assert {d.task_id for d in result.denials} == {"CBN-001", "CBN-002"}
    assert all(d.reason.startswith("exploratory — ") for d in result.denials)


def test_xheavy_stays_dispatchable_when_attended() -> None:
    """Manual-only means the operator may still work it — the denial is launch-only."""
    tasks = parse_plan(XHEAVY_PLAN)
    chosen = pick_next(tasks)
    assert chosen is not None
    assert chosen.task_id == "CBN-001"


@pytest.mark.parametrize("token", ["xheavy", "XHEAVY", "XHeavy", " xheavy "])
def test_xheavy_denial_normalizes_its_token(token: str) -> None:
    """Matched the way `taskmap.classify` matches, so the two cannot disagree.

    Built as a `PlanTask` rather than parsed: `TASK_LINE`'s model group is
    lowercase-only (`[a-z][\\w.-]*`), so an uppercase `[XHEAVY]` never survives
    `parse_plan` to reach this predicate at all. The casefold is defensive
    symmetry with `taskmap`, and pinning it needs a task built directly — routing
    it through the grammar would assert the grammar's rejection instead.
    """
    task = PlanTask(
        task_id="CBN-001",
        completed=False,
        priority=PlanPriority.HIGH,
        model=token,
        unattended=True,
    )
    allow, reason = eligibility(task, unattended=True)
    assert allow is False
    assert reason.startswith("exploratory — ")


# --- Frontier dimension (CBN-331) ---------------------------------------------

FRONTIER_PLAN = """# sample

## High

- [ ] **CBN-001** [frontier] [unattended] | band — chooser-assignable upstream, not here.
- [ ] **CBN-002** [fable] [unattended] | concrete — frontier-band model.
- [ ] **CBN-003** [mythos] [unattended] | concrete — its limited-access sibling.
- [ ] **CBN-004** [heavy] [unattended] | routine — the reachable head.
"""


def test_frontier_band_never_dispatches() -> None:
    """Caobunga caps itself at `[heavy]`, below flaitron's `[frontier]` cap (CBN-331).

    Every frontier-band row sorts ahead of the `[heavy]` one, so a picker that did
    not deny them would dispatch CBN-001.
    """
    chosen = pick_next(parse_plan(FRONTIER_PLAN), unattended=True)
    assert chosen is not None
    assert chosen.task_id == "CBN-004"


def test_frontier_denial_names_the_token_and_outranks_the_marker() -> None:
    """Every row is marked, so the reason must not be `marked` or `unmarked`."""
    result = coverage(parse_plan(FRONTIER_PLAN))
    assert (result.open, result.marked, result.denied) == (4, 1, 3)
    assert [d.reason for d in result.denials] == [
        f"frontier — [{token}] is operator-authorized; "
        "caobunga caps dispatch at [heavy], stricter than flaitron's [frontier]"
        for token in ("frontier", "fable", "mythos")
    ]
    assert all(d.seeded_line is None for d in result.denials)


def test_frontier_stays_dispatchable_when_attended() -> None:
    """Operator-authorized means the operator may still work it — launch-only denial."""
    chosen = pick_next(parse_plan(FRONTIER_PLAN))
    assert chosen is not None
    assert chosen.task_id == "CBN-001"


@pytest.mark.parametrize(
    ("row", "code"),
    [
        ("**CBN-050** [xheavy] [fable] | both — exploratory first.", "exploratory"),
        ("**CBN-050** [fable] [handoff] | both — frontier before handoff.", "frontier"),
        ("**CBN-050** [heavy] [frontier] [unattended] | x — extras frontier.", "frontier"),
        ("**CBN-050** [unattended] [mythos] | x — marker misplaced.", "frontier"),
    ],
)
def test_frontier_check_order_and_position(row: str, code: str) -> None:
    """Order is `exploratory` → `frontier` → `handoff`, read from either slot (CBN-260)."""
    allow, reason = eligibility(parse_plan(f"## High\n\n- [ ] {row}\n")[0], unattended=True)
    assert allow is False
    assert reason.startswith(f"{code} — ")


# --- Handoff dimension (CBN-136) ----------------------------------------------

HANDOFF_PLAN = """# sample

## High

- [ ] **CBN-030** [heavy] [handoff] | upstream-filing — operator runs the scoped prompt.
- [ ] **CBN-031** [heavy] [handoff] [unattended] | mis-seeded — marked anyway; still denied.
- [ ] **CBN-032** [heavy] [unattended] | routine — the reachable head.
"""
# CBN-031 is the case the gate exists for. CBN-123 measured an operator seeding
# `[unattended]` on a row whose own description said not to start it, so a
# report-only signal would be silent exactly where it is needed most.


def test_parse_plan_reads_the_handoff_marker() -> None:
    """`[handoff]` rides the trailing-token run `[unattended]` already uses.

    No `TASK_LINE` change: group 5 captures the run and `EXTRA_TOKEN` returns
    every token in it, so reading a second name out of that list is reading the
    producer's format rather than widening it (§"Fleet boundary").
    """
    by_id = {task.task_id: task for task in parse_plan(HANDOFF_PLAN)}
    assert by_id["CBN-030"].handoff is True
    assert by_id["CBN-030"].unattended is False
    assert by_id["CBN-031"].handoff is True
    assert by_id["CBN-031"].unattended is True
    assert by_id["CBN-032"].handoff is False
    # The model slot is untouched: a marker in the trailing run is not the model.
    assert by_id["CBN-030"].model == "heavy"


def test_handoff_rows_never_dispatch() -> None:
    """A headless worker can only reach the park, so it is never sent (CBN-136)."""
    chosen = pick_next(parse_plan(HANDOFF_PLAN), unattended=True)
    assert chosen is not None
    assert chosen.task_id == "CBN-032"


def test_handoff_denies_even_when_the_row_is_marked() -> None:
    """The mis-seed case — the one behaviour a report-only signal cannot supply.

    CBN-031 carries `[unattended]`, so a marker-only picker would dispatch it.
    """
    tasks = {task.task_id: task for task in parse_plan(HANDOFF_PLAN)}
    allow, reason = eligibility(tasks["CBN-031"], unattended=True)
    assert allow is False
    assert reason.startswith("handoff — ")


def test_handoff_denial_names_a_fix_that_exists() -> None:
    """Not `unmarked`, which would send an operator to seed a marker that changes
    nothing — the misdirection CBN-114 closed for the exploratory rung.
    """
    result = coverage(parse_plan(HANDOFF_PLAN))
    assert (result.open, result.marked, result.denied) == (3, 1, 2)
    assert result.marked + result.denied == result.open
    by_id = {d.task_id: d for d in result.denials}
    assert set(by_id) == {"CBN-030", "CBN-031"}
    assert all(d.reason.startswith("handoff — ") for d in by_id.values())


def test_handoff_denial_offers_no_seeded_line() -> None:
    """Seeding is not the fix: the hand-off is a declared property of the work.

    Suppression costs no code — `_denial` already gates the suggestion on the
    reason code — but it is the report's whole point, so it is pinned here.
    """
    result = coverage(parse_plan(HANDOFF_PLAN))
    assert all(d.seeded_line is None for d in result.denials)
    # The control: an ordinary unmarked row in the same shape still gets one.
    plan = "## High\n\n- [ ] **CBN-040** [heavy] | plain — nothing declared.\n"
    assert coverage(parse_plan(plan)).denials[0].seeded_line is not None


def test_exploratory_outranks_handoff() -> None:
    """Check order is `exploratory` → `handoff` → `marker` (CBN-114, CBN-136).

    An `[xheavy]` row is manual-only whether or not it also hands off, and
    CBN-114's position is left byte-stable rather than renegotiated.
    """
    plan = "## High\n\n- [ ] **CBN-050** [xheavy] [handoff] | both — two denials apply.\n"
    allow, reason = eligibility(parse_plan(plan)[0], unattended=True)
    assert allow is False
    assert reason.startswith("exploratory — ")


def test_handoff_stays_dispatchable_when_attended() -> None:
    """The declaration is about a *headless* run.

    With an operator present the hand-off is simply the work, so the row is the
    candidate — and the reason says what to expect rather than staying silent,
    which is the next-task-suggester half of CBN-136.
    """
    tasks = parse_plan(HANDOFF_PLAN)
    chosen = pick_next(tasks)
    assert chosen is not None
    assert chosen.task_id == "CBN-030"
    assert chosen.eligibility_reason is not None
    assert chosen.eligibility_reason.startswith("attended — ")
    assert "[handoff] declared" in chosen.eligibility_reason


def test_attended_reason_is_unchanged_without_the_marker() -> None:
    """The annotation is additive: a plain row reads exactly as it did (CBN-EPIC-17.2)."""
    plan = "## High\n\n- [ ] **CBN-060** [heavy] | plain — nothing declared.\n"
    allow, reason = eligibility(parse_plan(plan)[0])
    assert allow is True
    assert reason == "attended — this read does not consult the marker"


@pytest.mark.parametrize(
    ("row", "code"),
    [
        # `[handoff]` captured *as* the model: marked, so it used to dispatch.
        ("**CBN-080** [handoff] [unattended] | h — model-slot hand-off.", "handoff"),
        ("**CBN-080** [handoff] | h — model-slot hand-off, unmarked.", "handoff"),
        # `[xheavy]` in the trailing run: marked, so it used to dispatch.
        ("**CBN-080** [heavy] [xheavy] [unattended] | x — extras xheavy.", "exploratory"),
        # Model slot taken by the marker: denied, but as `unmarked` — the wrong fix.
        ("**CBN-080** [unattended] [xheavy] | x — extras xheavy, marker misplaced.", "exploratory"),
        # The trailing grammar pins only the first letter lowercase.
        ("**CBN-080** [heavy] [xHeavy] [unattended] | x — mixed-case extras.", "exploratory"),
        ("**CBN-080** [heavy] [hAndoff] [unattended] | h — mixed-case extras.", "handoff"),
    ],
)
def test_denials_are_position_independent(row: str, code: str) -> None:
    """CBN-260. `TASK_LINE` captures whichever token comes first as the model.

    A denial keyed to one slot missed the same token one position over, and the
    first two shapes marked-and-dispatched a row the operator had declared
    manual-only or hand-off.
    """
    task = parse_plan(f"## High\n\n- [ ] {row}\n")[0]
    allow, reason = eligibility(task, unattended=True)
    assert allow is False
    assert reason.startswith(f"{code} — ")


def test_model_slot_handoff_is_named_when_attended() -> None:
    """The attended clause reads the same position-independent predicate (CBN-260)."""
    task = parse_plan("## High\n\n- [ ] **CBN-081** [handoff] | h — model-slot hand-off.\n")[0]
    assert task.model == "handoff"
    allow, reason = eligibility(task)
    assert allow is True
    assert "[handoff] declared" in reason


def test_parse_plan_keeps_the_trailing_run_verbatim() -> None:
    task = parse_plan("## High\n\n- [ ] **CBN-082** [heavy] [xheavy] [unattended] | x — y.\n")[0]
    assert task.model == "heavy"
    assert task.extras == ["xheavy", "unattended"]


def test_handoff_is_orthogonal_to_the_blocked_gate() -> None:
    """Three signals, three questions — a blocked row denies as `blocked` first.

    `Blocked by [[ID]]` answers "may this start" and applies at every posture, so
    it is checked ahead of both safety dimensions and its reason wins.
    """
    plan = (
        "## High\n\n"
        "- [ ] **CBN-070** [heavy] [handoff] | both — Blocked by [[CBN-099]] until that lands.\n"
    )
    task = parse_plan(plan)[0]
    assert task.handoff is True
    assert task.blocked_by == ["CBN-099"]
    allow, reason = eligibility(task, unattended=True)
    assert allow is False
    assert reason.startswith("blocked — ")


# --- Seeding report (CBN-119.3) ------------------------------------------------

SEEDING_PLAN = """# sample

## High

- [ ] **CBN-020** [medium]🧩 | glyphed — the 52% shape: a marker typed last lands after the glyph.
- [ ] **CBN-021** [!critical] [light]🔧 | floated — critical, so it dispatches before CBN-020.
- [ ] **CBN-022** | modelless — no [model], so an insertion here is captured as the model.
- [ ] **CBN-023** [heavy] | blocked-row — Blocked by [[CBN-099]] until that lands.

## Medium

- [ ] **CBN-024** [light] [unattended] | marked — already seeded, nothing to suggest.
"""


def test_parse_plan_records_where_each_row_lives() -> None:
    """A report that names only an id sends the operator searching for it (CBN-119.3)."""
    by_id = {task.task_id: task for task in parse_plan(SEEDING_PLAN)}
    assert by_id["CBN-020"].line == 5
    assert by_id["CBN-024"].line == 12
    assert by_id["CBN-020"].text.startswith("- [ ] **CBN-020** [medium]🧩")


def test_parse_plan_reports_the_original_line_not_the_blanked_one() -> None:
    """`text` is the row as the operator would edit it, comment and all.

    `parse_plan` matches against the HTML-comment-blanked line; reporting that
    back would hand the operator text their file does not contain.
    """
    plan = "## High\n\n- [ ] **CBN-030** [light] | s — d <!-- note -->\n"
    assert parse_plan(plan)[0].text.endswith("<!-- note -->")


def test_seeded_line_places_the_marker_after_the_suggestion_glyph() -> None:
    """The measured motivation: 11 of the fleet's 21 denied rows carry a glyph.

    The after-glyph placement used to drop the whole row (upstream `CORE-502`);
    `CBN-167` made both placements parse, and `seeded_line` emits flaitron's own
    shape — `SPEC/unattended-candidacy.md` writes `[light]🔧 [unattended]`
    (CBN-343) — so every suggestion matches what upstream filers write.
    """
    task = next(t for t in parse_plan(SEEDING_PLAN) if t.task_id == "CBN-020")
    seeded = seeded_line(task)
    assert seeded is not None
    assert "[medium]🧩 [unattended]" in seeded
    reparsed = parse_plan(f"## High\n{seeded}\n")
    assert len(reparsed) == 1
    assert reparsed[0].unattended


def test_seeded_line_keeps_a_critical_flag_and_child_indent() -> None:
    """The suggestion replaces a row, so every decoration it did not add survives."""
    plan = "## High\n\n  - [ ] **CBN-031.2** [!critical] [heavy]🧠 | s — d\n"
    seeded = seeded_line(parse_plan(plan)[0])
    assert seeded is not None
    assert seeded.startswith("  - [ ] **CBN-031.2** [!critical] [heavy]🧠 [unattended]")
    assert parse_plan(f"## High\n{seeded}\n")[0].unattended


def test_seeded_line_places_the_marker_after_the_frontier_glyph() -> None:
    """💎 (flaitron v6.1.0) is a suggestion glyph like the other four (CBN-330)."""
    plan = "## High\n\n- [ ] **CBN-032** [frontier]💎 | s — d\n"
    seeded = seeded_line(parse_plan(plan)[0])
    assert seeded is not None
    assert seeded.startswith("- [ ] **CBN-032** [frontier]💎 [unattended]")
    assert parse_plan(f"## High\n{seeded}\n")[0].unattended


def test_seeded_line_keeps_before_glyph_where_after_cannot_parse() -> None:
    """A glyph after a stacked `[model]` run sits in `TASK_LINE`'s second slot (CBN-343).

    No token may follow that slot — here or in upstream `parser.ts` — so
    `[sonnet] [light]🔧 [unattended]` would drop the row; the marker lands
    before the glyph, the one placement that parses.
    """
    plan = "## High\n\n- [ ] **CBN-033** [sonnet] [light]🔧 | s — d\n"
    seeded = seeded_line(parse_plan(plan)[0])
    assert seeded is not None
    assert seeded.startswith("- [ ] **CBN-033** [sonnet] [light] [unattended]🔧 |")
    assert parse_plan(f"## High\n{seeded}\n")[0].unattended
    after = "- [ ] **CBN-033** [sonnet] [light]🔧 [unattended] | s — d"
    assert parse_plan(f"## High\n{after}\n") == []


def test_seeded_line_refuses_a_row_with_no_model() -> None:
    """The silent failure, and why the suggestion is re-parsed rather than trusted.

    `**ID** [unattended]` still *parses* — the marker is captured as the model,
    so the row reads `unmarked` forever with no trace of the attempt. Emitting a
    naive insertion would manufacture that; the round-trip catches it.
    """
    task = next(t for t in parse_plan(SEEDING_PLAN) if t.task_id == "CBN-022")
    assert seeded_line(task) is None
    naive = "- [ ] **CBN-022** [unattended] | modelless — d"
    assert parse_plan(f"## High\n{naive}\n")[0].unattended is False


def test_seeded_line_has_nothing_to_offer_an_already_marked_row() -> None:
    task = next(t for t in parse_plan(SEEDING_PLAN) if t.task_id == "CBN-024")
    assert seeded_line(task) is None


def test_denials_carry_the_line_the_operator_would_edit() -> None:
    result = coverage(parse_plan(SEEDING_PLAN))
    by_id = {d.task_id: d for d in result.denials}
    assert by_id["CBN-020"].line == 5
    assert by_id["CBN-020"].text.startswith("- [ ] **CBN-020**")
    assert by_id["CBN-020"].seeded_line is not None


def test_denials_seeding_cannot_fix_carry_no_suggestion() -> None:
    """A `blocked` row needs its clause removed and a modelless row a hand edit.

    Offering a marker for either would be the same class of misdirection CBN-114
    closed when it put the exploratory check ahead of the marker one.
    """
    by_id = {d.task_id: d for d in coverage(parse_plan(SEEDING_PLAN)).denials}
    assert by_id["CBN-023"].reason.startswith("blocked — ")
    assert by_id["CBN-023"].seeded_line is None
    assert by_id["CBN-022"].reason.startswith("unmarked — ")
    assert by_id["CBN-022"].seeded_line is None


def test_xheavy_denials_carry_no_seeded_line() -> None:
    """Seeding an `[xheavy]` row would not make it dispatchable (CBN-114)."""
    result = coverage(parse_plan(XHEAVY_PLAN))
    assert all(d.seeded_line is None for d in result.denials)


def test_denials_are_ordered_for_seeding_not_by_document_position() -> None:
    """The list *is* the seeding order: seed the first entry and it dispatches next.

    CBN-021 is filed *below* CBN-020 but carries `[!critical]`, so the picker
    takes it first. Document order would put CBN-020 at the head and send the
    operator to seed a row that still would not run next.
    """
    denials = [d.task_id for d in coverage(parse_plan(SEEDING_PLAN)).denials]
    assert denials[0] == "CBN-021"
    assert denials == ["CBN-021", "CBN-020", "CBN-022", "CBN-023"]

    marked = SEEDING_PLAN.replace("[light]🔧 | floated", "[light] [unattended]🔧 | floated")
    chosen = pick_next(parse_plan(marked), unattended=True)
    assert chosen is not None
    assert chosen.task_id == "CBN-021"


def test_denied_and_denials_agree_about_order(tmp_path: Path) -> None:
    """Two lists of the same DTO over the same rows must not disagree (CBN-119.3)."""
    read = read_project(_repo(tmp_path, SEEDING_PLAN), unattended=True)
    assert [d.task_id for d in read.denied] == [d.task_id for d in read.coverage.denials]
    assert [d.line for d in read.denied] == [d.line for d in read.coverage.denials]


# The three shapes CBN-123 actually met on the fleet, transcribed from its F5
# table rather than invented: `Blocked by` + prose with no wikilink (one
# repo's `BE-019`), and `Blocked on [[ID]]` twice (another's `CORE-174.5` / `.7`). All
# three parse as unblocked, so all three were marked `[unattended]` on this
# report's own recommendation — one of them against a description that says
# *do not start*.
UNPARSED_BLOCKER_PLAN = """# sample

## High

- [ ] **CBN-201** [light] | prose-blocker — Blocked by an external prerequisite: the \
vendor disables its public portal. Last re-checked 2026-06-30; still blocked.
- [ ] **CBN-202** [light] | wrong-preposition — Blocked on data accrual like [[CBN-169]] \
— do not start before the rows exist.
- [ ] **CBN-203** [light] | wrong-preposition-bare — Blocked on [[CBN-202]].
- [ ] **CBN-204** [light] | parsed-clause — Blocked by [[CBN-202]] — the canonical grammar.
- [ ] **CBN-205** [light] | clean — nothing about blocking at all.
- [ ] **CBN-206** [light] | literal-example — writes `Blocked by [[CBN-999]]` as a code sample.
"""


def _unparsed_blocker_denials() -> dict[str, Denial]:
    return {d.task_id: d for d in coverage(parse_plan(UNPARSED_BLOCKER_PLAN)).denials}


@pytest.mark.parametrize(
    ("task_id", "starts_with"),
    [
        ("CBN-201", "Blocked by an external prerequisite"),
        ("CBN-202", "Blocked on data accrual like [[CBN-169]]"),
        ("CBN-203", "Blocked on [[CBN-202]]."),
    ],
)
def test_unparsed_blocker_catches_every_shape_the_fleet_writes(
    task_id: str, starts_with: str
) -> None:
    """CBN-123's three rows, verbatim: the grammar saw none of them (CBN-128)."""
    tasks = {task.task_id: task for task in parse_plan(UNPARSED_BLOCKER_PLAN)}
    task = tasks[task_id]
    assert task.blocked_by == [], "precondition: the grammar must capture nothing"
    clause = unparsed_blocked_clause(task)
    assert clause is not None
    assert clause.startswith(starts_with)


def test_unparsed_blocker_quotes_the_tail_the_elision_hides() -> None:
    """The fragment starts at the mention, so a renderer's budget buys the clause.

    CBN-119.3 sized the 96-char elision so the head decoration run stays visible;
    every clause CBN-123 met sat past it. Starting here is what lets the same
    budget show the part that decides *whether* to seed.
    """
    task = next(t for t in parse_plan(UNPARSED_BLOCKER_PLAN) if t.task_id == "CBN-202")
    clause = unparsed_blocked_clause(task)
    assert clause is not None
    assert clause.startswith("Blocked on")
    assert "do not start before the rows exist" in clause
    assert not clause.startswith("wrong-preposition")


def test_unparsed_blocker_stays_silent_where_it_would_only_repeat() -> None:
    """A parsed clause already denies as `blocked`; a clean row has nothing to say.

    The exclusion is structural rather than a special case: a captured clause
    makes `blocked_by` non-empty, so the two signals cannot both fire on one row.
    """
    tasks = {task.task_id: task for task in parse_plan(UNPARSED_BLOCKER_PLAN)}
    assert tasks["CBN-204"].blocked_by == ["CBN-202"]
    assert unparsed_blocked_clause(tasks["CBN-204"]) is None
    assert unparsed_blocked_clause(tasks["CBN-205"]) is None


def test_unparsed_blocker_ignores_a_code_span_example() -> None:
    """A `Blocked by` in backticks is literal text — `parse_blocked_by`'s own tolerance."""
    tasks = {task.task_id: task for task in parse_plan(UNPARSED_BLOCKER_PLAN)}
    assert unparsed_blocked_clause(tasks["CBN-206"]) is None


def test_unparsed_blocker_rides_the_denial_without_withholding_the_fix() -> None:
    """Report-only: the row still gets the `seeded_line` an `unmarked` denial needs.

    Suppressing it would make Caobunga decide a producer's sentence means
    "blocked" — the §"Fleet boundary" line widening the grammar was rejected for.
    """
    by_id = _unparsed_blocker_denials()
    denial = by_id["CBN-202"]
    assert denial.reason.startswith("unmarked — ")
    assert denial.unparsed_blocker is not None
    assert denial.seeded_line is not None
    assert by_id["CBN-204"].unparsed_blocker is None


def test_unparsed_blocker_reaches_every_denial_list(tmp_path: Path) -> None:
    """One `_denial` constructor, so `denied` and `coverage.denials` cannot disagree."""
    read = read_project(_repo(tmp_path, UNPARSED_BLOCKER_PLAN), unattended=True)
    assert [d.unparsed_blocker for d in read.denied] == [
        d.unparsed_blocker for d in read.coverage.denials
    ]
    assert any(d.unparsed_blocker is not None for d in read.denied)


# --- CBN-157 · unmarked triage ------------------------------------------------
#
# Splits `unmarked` denials by flaitron's `[unattended]` candidacy predicate
# (`SPEC/unattended-candidacy.md` §"Candidacy predicate", CORE-577.2), mirrored
# over the clauses `eligibility` has not already cleared by the time a row
# reaches `unmarked`. Every non-`unmarked` denial (blocked, exploratory,
# handoff) keeps `triage=None` — the predicate never judged them.


def test_obvious_and_needs_judgment_split_model_and_critical() -> None:
    """CBN-020 trips no keyword, so the miss is not obvious (CBN-361).

    CBN-021 (critical) and CBN-022 (no model) still fail their own clauses first.
    """
    by_id = {d.task_id: d for d in coverage(parse_plan(SEEDING_PLAN)).denials}
    assert by_id["CBN-020"].triage is TriageClass.NEEDS_JUDGMENT
    assert by_id["CBN-020"].triage_reason == "keyword — a miss is not a semantic pass"
    assert by_id["CBN-021"].triage is TriageClass.NEEDS_JUDGMENT
    assert by_id["CBN-021"].triage_reason is not None
    assert "critical" in by_id["CBN-021"].triage_reason
    assert by_id["CBN-022"].triage is TriageClass.NEEDS_JUDGMENT
    assert "model" in by_id["CBN-022"].triage_reason  # type: ignore[operator]
    # CBN-023 denies as `blocked`, never reaching the predicate at all.
    assert by_id["CBN-023"].triage is None


def test_needs_judgment_on_an_unparsed_blocker_mention() -> None:
    """CBN-128's report-only signal is one of the clauses this mirrors (clause 4)."""
    by_id = _unparsed_blocker_denials()
    assert by_id["CBN-201"].triage is TriageClass.NEEDS_JUDGMENT
    assert by_id["CBN-201"].triage_reason is not None
    assert "unparsed-blocker" in by_id["CBN-201"].triage_reason


HEAVY_AND_KEYWORD_PLAN = """# sample

## High

- [ ] **CBN-311** [heavy] | heavy-tier — too broad to dispatch blind.
- [ ] **CBN-312** [light] | credential-rotation — rotate the vendor's credential first.
"""


def test_needs_judgment_on_heavy_model_and_keyword_screen() -> None:
    """Clause 1 (tier) and clause 3's free-text keyword screen (no formal [handoff])."""
    by_id = {d.task_id: d for d in coverage(parse_plan(HEAVY_AND_KEYWORD_PLAN)).denials}
    assert by_id["CBN-311"].triage is TriageClass.NEEDS_JUDGMENT
    assert "model" in by_id["CBN-311"].triage_reason  # type: ignore[operator]
    assert by_id["CBN-312"].triage is TriageClass.NEEDS_JUDGMENT
    assert by_id["CBN-312"].triage_reason == "keyword — description mentions 'credential'"


def test_visual_and_raster_miss_is_not_obvious() -> None:
    """Visual and raster prose is not a mechanical hit, and the miss is not a pass."""
    plan = """## High

- [ ] **CBN-401** [light] | raster-icon — confirm the raster favicon visually.
"""
    denial = next(d for d in coverage(parse_plan(plan)).denials if d.task_id == "CBN-401")
    assert denial.triage is TriageClass.NEEDS_JUDGMENT
    assert denial.triage_reason == "keyword — a miss is not a semantic pass"


def test_clause3_keyword_list_gains_no_visual_or_raster_words() -> None:
    """The mechanical screen stays the CORE-577.2 list (CBN-361)."""
    assert _HANDOFF_KEYWORDS == (
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
    lowered = {word.casefold() for word in _HANDOFF_KEYWORDS}
    assert "visual" not in lowered
    assert "raster" not in lowered


# Clause 6's epic-child rule, exercised with `[light]` `.1` rows so clause 1
# never preempts it — a real `.1` is `[heavy]` by convention, which would
# already deny before clause 6 is ever reached.
TRIAGE_EPIC_PLAN = """# sample

## High

- [x] **CBN-320.1** [light] | discovery-closed — filed the children.
- [ ] **CBN-320.2** [light] | first-child — predecessor CBN-320.1 is closed.
- [ ] **CBN-321.1** [light] | discovery-open — still open.
- [ ] **CBN-321.2** [light] | second-child — predecessor CBN-321.1 is not closed.
- [ ] **CBN-322.1** [light] | discovery-itself — a .1 row is never a candidate.
- [ ] **CBN-323.N** [light] | audit — qualifies through clauses 1-4 alone.
"""


def test_epic_child_stem_gap_still_wins_over_a_keyword_miss() -> None:
    """A closed predecessor is not a pass: the keyword miss still declines (CBN-361).

    An open predecessor keeps the stem reason, which is the more specific clause.
    """
    by_id = {d.task_id: d for d in coverage(parse_plan(TRIAGE_EPIC_PLAN)).denials}
    assert by_id["CBN-320.2"].triage is TriageClass.NEEDS_JUDGMENT
    assert by_id["CBN-320.2"].triage_reason == "keyword — a miss is not a semantic pass"
    assert by_id["CBN-321.2"].triage is TriageClass.NEEDS_JUDGMENT
    assert "stem predecessor" in by_id["CBN-321.2"].triage_reason  # type: ignore[operator]


def test_epic_discovery_row_is_never_obvious_even_if_light() -> None:
    by_id = {d.task_id: d for d in coverage(parse_plan(TRIAGE_EPIC_PLAN)).denials}
    assert by_id["CBN-322.1"].triage is TriageClass.NEEDS_JUDGMENT
    assert "Discovery" in by_id["CBN-322.1"].triage_reason  # type: ignore[operator]


def test_epic_audit_row_needs_no_stem_check() -> None:
    """A `.N` audit skips the stem check, then the keyword miss still declines it."""
    by_id = {d.task_id: d for d in coverage(parse_plan(TRIAGE_EPIC_PLAN)).denials}
    assert by_id["CBN-323.N"].triage is TriageClass.NEEDS_JUDGMENT
    assert by_id["CBN-323.N"].triage_reason == "keyword — a miss is not a semantic pass"


TRIAGE_AUDIT_PLAN = """# sample

## High

- [ ] **CBN-330.2** [heavy] | open-sibling — still open.
- [ ] **CBN-330.N** [light] | audit — siblings open.
- [ ] **CBN-331.2** [heavy] | open-sibling — still open.
- [x] **CBN-331.3** [light] | closed-predecessor — the stem check passes.
- [ ] **CBN-331.4** [light] | gate audit — numbered audit, an earlier sibling open.
- [x] **CBN-332.2** [light] | closed-sibling — done.
- [ ] **CBN-332.N** [light] | audit — siblings closed.
"""


def test_epic_audit_row_with_open_siblings_needs_judgment() -> None:
    """CBN-353: seeding a `.N` audit offers a row eligibility then denies `audit — …`."""
    by_id = {d.task_id: d for d in coverage(parse_plan(TRIAGE_AUDIT_PLAN)).denials}
    assert by_id["CBN-330.N"].triage is TriageClass.NEEDS_JUDGMENT
    assert by_id["CBN-330.N"].triage_reason == (
        "audit — epic siblings still open (CBN-330.2); the .N audit runs after they close"
    )
    assert by_id["CBN-332.N"].triage is TriageClass.NEEDS_JUDGMENT
    assert by_id["CBN-332.N"].triage_reason == "keyword — a miss is not a semantic pass"


def test_numbered_audit_child_with_open_siblings_needs_judgment() -> None:
    """CBN-350's numbered audit child (`.4 | gate audit`) reads like `.N`.

    Its predecessor `.3` is closed, so the stem check passes and only the audit
    mirror can decide.
    """
    by_id = {d.task_id: d for d in coverage(parse_plan(TRIAGE_AUDIT_PLAN)).denials}
    assert by_id["CBN-331.4"].triage is TriageClass.NEEDS_JUDGMENT
    assert by_id["CBN-331.4"].triage_reason == (
        "audit — epic siblings still open (CBN-331.2); the .4 audit runs after they close"
    )


def test_triage_agrees_with_eligibility_once_the_audit_row_is_marked() -> None:
    """An audit gap is needs-judgment and eligibility denies the marked row.

    A mechanically clean audit (CBN-332.N) is also needs-judgment: the keyword
    miss is not a pass (CBN-361). Marking it still dispatches, because
    eligibility does not re-run the keyword screen.
    """
    tasks = parse_plan(TRIAGE_AUDIT_PLAN)
    done = completed_task_ids(tasks)
    still_open = open_task_ids(tasks)
    triage = {d.task_id: d.triage for d in coverage(tasks).denials}
    audits = ("CBN-330.N", "CBN-331.4", "CBN-332.N")
    for task in (t for t in tasks if t.task_id in audits):
        marked = task.model_copy(update={"unattended": True})
        allow, _ = eligibility(marked, unattended=True, completed_ids=done, open_ids=still_open)
        assert triage[task.task_id] is TriageClass.NEEDS_JUDGMENT
        assert allow is (task.task_id == "CBN-332.N"), task.task_id


def test_epic_child_with_a_non_numeric_suffix_needs_judgment() -> None:
    """A `.2a` suffix (`TASK_ID_BODY` allows a trailing letter) has no predecessor to confirm."""
    plan = "## High\n\n- [ ] **CBN-324.2a** [light] | lettered — non-numeric epic-child suffix.\n"
    denial = next(d for d in coverage(parse_plan(plan)).denials if d.task_id == "CBN-324.2a")
    assert denial.triage is TriageClass.NEEDS_JUDGMENT
    assert "no numeric predecessor" in denial.triage_reason  # type: ignore[operator]


def test_triage_is_none_off_the_unmarked_code() -> None:
    """`blocked` / `exploratory` / `handoff` denials never carry a triage verdict."""
    result = coverage(parse_plan(SEEDING_PLAN))
    codes = {d.task_id: d.reason.split(" — ", 1)[0] for d in result.denials}
    assert codes["CBN-023"] == "blocked"
    blocked = next(d for d in result.denials if d.task_id == "CBN-023")
    assert blocked.triage is None
    assert blocked.triage_reason is None


# --- CBN-143.2 · public import-surface pin -----------------------------------
#
# CBN-EPIC-143 splits this module into a `flaitron/` package while holding
# `from caobunga.flaitron import …` constant for all 16 import sites. That import
# surface is the seam every child of the epic crosses, and nothing else asserts
# it — `__all__` is not declared, so the surface is whatever the namespace
# happens to hold. These three tests freeze it.

#: Names `caobunga.flaitron` genuinely publishes. Frozen literal, exact in both
#: directions: a subset check would pass while a name was silently lost mid-move,
#: which is the failure this pin exists to catch.
FACADE = frozenset(
    {
        "BLOCKED_BY_BLOCK",
        "BLOCKED_MENTION",
        "CHECKBOX_ROW",
        "CODE_SPAN",
        "CORE_REL",
        "Coverage",
        "Denial",
        "EXTRA_TOKEN",
        "FENCE_DELIMITER",
        "FRONTMATTER",
        "FT_TASK_SKILL_DIR_REL",
        "HANDOFF_MARKER",
        "HEADING_LINE",
        "HTML_COMMENT",
        "LAUNCH_BUCKETS",
        "NextTask",
        "OPEN_STATUSES",
        "OpenTasknote",
        "PARENT_EPIC",
        "PARK_SEPARATOR",
        "PLAN_REL",
        "PlanPriority",
        "PlanTask",
        "ProjectRead",
        "SELF_FT_TASK_SKILL_DIR_REL",
        "SIDEQUEST_DIR_REL",
        "SUGGESTED_NEXT",
        "TASKNOTE_DIR_REL",
        "TASK_ID_BODY",
        "TASK_LINE",
        "TriageClass",
        "UNATTENDED_MARKER",
        "UNATTENDED_MODULE",
        "UNATTENDED_TOKEN",
        "UnparsedRow",
        "WIKILINK",
        "XHEAVY_TOKEN",
        "completed_task_ids",
        "coverage",
        "dispatchable",
        "eligibility",
        "epic_parent",
        "epic_stem",
        "external_review_box",
        "extract_follow_ups",
        "extract_summary",
        "fence_mask",
        "in_flight_task",
        "is_descoped",
        "list_open_tasknotes",
        "marker_drift",
        "open_task_ids",
        "parse_blocked_by",
        "parse_plan",
        "phase_progress",
        "pick_next",
        "pinned_skill_rel",
        "queue_denial",
        "read_project",
        "seeded_line",
        "split_park_reason",
        "supports_unattended",
        "try_read_project",
        "unattended_module_rel",
        "unparsed_blocked_clause",
        "unparsed_plan_rows",
    }
)

#: Stdlib / third-party names that leak into the namespace because Python has no
#: import privacy. No importer takes any of them from `flaitron`, and they are
#: exactly the names that churn during the split. `re` was the worked example, and
#: `CBN-143.5` sharpened it: `grammar.py` took every regex and `import re` *survived*,
#: because `_task_id_sort_key` still calls `re.fullmatch` — so that drop lands at `.8`
#: with the picker, not here. Subtracted rather than pinned, so a correct move that
#: drops one stays green; naming them keeps the leak visible.
INCIDENTAL = frozenset(
    {
        "annotations",
        "re",
        "yaml",
        "Path",
        "BaseModel",
        "ConfigDict",
        "StrEnum",
        "Collection",
        "Iterable",
        "Iterator",
        "UserError",
    }
)


def _bound_submodules(pkg: ModuleType) -> set[str]:
    """Names bound on `pkg` because `__init__` imports a submodule of it (CBN-143.4).

    A second leak class the CBN-143.2 pin could not have enumerated: at `.2` the
    package had no submodules, and `CBN-143.3`'s move added none. `.4` binds
    `layout`, and `.5`–`.8` each bind one more — every one of them a *correct*
    move that a name-exact pin would call a regression.

    Subtracted dynamically rather than as a frozen literal so the remaining
    children cost no edit here, and keyed on the bound **value** rather than the
    name so it can never swallow a real façade name: `coverage` is a `FACADE`
    member and a plausible module name, but `from .selection import coverage`
    binds a function, which is not a `ModuleType` and stays in the comparison.
    `re` / `yaml` are modules but not `caobunga.flaitron.*`, so they remain named
    out loud in `INCIDENTAL`.
    """  # noqa: RUF002
    return {
        name
        for name, value in vars(pkg).items()
        if isinstance(value, ModuleType)
        and getattr(value, "__name__", "").startswith("caobunga.flaitron.")
    }


#: What each production module names in its `from caobunga.flaitron import …`
#: line today. Read back from source below rather than trusted, so an importer
#: quietly widening or narrowing its list fails here instead of at the next move.
IMPORTER_NAMES = {
    "loop": {
        "NextTask",
        "ProjectRead",
        "completed_task_ids",
        "eligibility",
        "list_open_tasknotes",
        "read_project",
    },
    "run_echo": {
        "Denial",
        "LAUNCH_BUCKETS",
        "NextTask",
        "ProjectRead",
        "UnparsedRow",
        "completed_task_ids",
        "eligibility",
        "open_task_ids",
        "split_park_reason",
    },
    "launch": {
        "NextTask",
        "list_open_tasknotes",
        "pinned_skill_rel",
        "supports_unattended",
        "unattended_module_rel",
    },
    "finish": {"NextTask"},
    "wait": {"PLAN_REL", "TASKNOTE_DIR_REL", "phase_progress"},
    "report": {"PLAN_REL"},
    "close": {
        "NextTask",
        "OpenTasknote",
        "PLAN_REL",
        "PlanTask",
        "ProjectRead",
        "TASK_LINE",
        "dispatchable",
        "external_review_box",
        "extract_follow_ups",
        "extract_summary",
        "is_descoped",
        "parse_plan",
        "read_project",
        "split_park_reason",
    },
    "git": {"TASKNOTE_DIR_REL"},
    "presence": {
        "NextTask",
        "OpenTasknote",
        "PLAN_REL",
        "ProjectRead",
        "SIDEQUEST_DIR_REL",
        "TASKNOTE_DIR_REL",
        "pick_next",
    },
    "api": {"Coverage", "ProjectRead", "try_read_project"},
    "cli": {"marker_drift", "try_read_project"},
    "status_render": {"PLAN_REL", "Coverage", "Denial", "TriageClass"},
    "rollup": {"PARENT_EPIC", "epic_parent", "parse_plan"},
}


def _flaitron_imports(module_name: str) -> set[str]:
    """Names `caobunga/<module_name>.py` imports from `caobunga.flaitron`, per its AST.

    Anchored on the `caobunga` package rather than on `flaitron.__file__`, whose
    parent stops being the package directory the moment CBN-143.3 turns this
    module into `flaitron/__init__.py`.
    """
    source = (Path(caobunga.__file__).parent / f"{module_name}.py").read_text(encoding="utf-8")
    return {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module == "caobunga.flaitron"
        for alias in node.names
    }


def _fixture_line_priorities(markdown: str) -> dict[int, str | None]:
    """1-based line -> the `PlanPriority` value its section carries, or `None`.

    Test-only mirror of `reader._plan_body_lines`'s heading walk (not exported),
    used only to scope a fixture's whole-file `unparsed` diagnostic down to the
    launch-bucket rows `unparsed_plan_rows` actually reports on — see
    `test_plan_grammar_fixture_conformance`.
    """
    lines = markdown.splitlines()
    fenced = fence_mask(lines)
    priorities: dict[int, str | None] = {}
    current: str | None = None
    for number, (line, in_fence) in enumerate(zip(lines, fenced, strict=True), start=1):
        if in_fence:
            continue
        heading = HEADING_LINE.match(line)
        if heading:
            name = heading.group(1)
            current = PlanPriority.HIGH.value if name == "Critical" else name
            continue
        priorities[number] = current
    return priorities


@pytest.mark.skipif(
    not PLAN_FIXTURES_DIR.parents[3].is_dir(), reason="public export: no .flaitron/ (CBN-369)"
)
def test_plan_grammar_fixture_cases_present() -> None:
    """CBN-371: an empty glob must fail here, not skip the parametrize below silently."""
    assert PLAN_FIXTURE_CASES, (
        f"no fixture cases under {PLAN_FIXTURES_DIR} — is the .flaitron/core submodule "
        "initialized? (`git submodule update --init`)"
    )


@pytest.mark.parametrize("case", PLAN_FIXTURE_CASES)
def test_plan_grammar_fixture_conformance(case: str) -> None:
    """`parse_plan` / `unparsed_plan_rows` against flaitron's shared conformance fixtures.

    `SPEC/fixtures/plan/` (CORE-618) pins the PLAN.md task-line grammar to
    concrete input/output pairs so every parser — the reference `viz/src/parser.ts`,
    this one — tests against one shared set of cases (CBN-203). Per the fixture's
    own README "Consuming the fixtures": task membership and order are mandatory
    (rule 1); every field this reader models is asserted, `relatedTasks` and
    `completedDate` are not, since `PlanTask` has no such fields (rule 2).

    `unparsed` compares in both directions, scoped to what `unparsed_plan_rows`
    claims to cover (checkbox rows — checked or not, CBN-210 — in a
    launch-bucket section) rather than asserted byte-for-byte: every in-scope
    fixture-flagged row must survive into caobunga's output (completeness),
    and every row caobunga does flag must stay in-scope and absent from
    `tasks` (rule 3, never inverted).
    The README explicitly allows a narrower-or-differently-shaped `unparsed`
    definition ("one with its own definition of a malformed row ... asserts
    what that definition covers") — `unparsed_plan_rows` flags *any* failing
    checkbox row, bold ID or not, which is broader than the reference's
    asterisk-gated heuristic (`viz/src/parser.ts`'s `CHECKBOX_BULLET` path);
    `exclusions.md` line 16 is a bare prose checklist item the reference
    silently excludes but this reader still flags, contradicting
    `unparsed_plan_rows`'s own docstring claim that "plain prose bullets ...
    don't count" — filed as a follow-up rather than fixed here (CBN-203 is
    fixture consumption only).
    """
    markdown = (PLAN_FIXTURES_DIR / f"{case}.md").read_text(encoding="utf-8")
    fixture = json.loads((PLAN_FIXTURES_DIR / f"{case}.json").read_text(encoding="utf-8"))

    tasks = parse_plan(markdown)
    assert [task.task_id for task in tasks] == [row["id"] for row in fixture["tasks"]]
    for task, expected in zip(tasks, fixture["tasks"], strict=True):
        assert task.completed == expected["completed"]
        assert task.priority.value == expected["priority"]
        assert task.critical == expected["critical"]
        assert task.unattended == expected["unattended"]
        assert task.handoff == expected["handoff"]
        assert task.model == expected["model"]
        assert task.shortname == expected["shortname"]
        assert task.description == (expected["descriptionRaw"] or "")
        assert task.blocked_by == expected["blockedBy"]
        assert task.line == expected["line"]

    priorities = _fixture_line_priorities(markdown)
    in_scope_fixture_unparsed = [
        row
        for row in fixture["unparsed"]
        if row["text"].startswith(("- [ ] ", "- [x] "))
        and priorities.get(row["line"]) in LAUNCH_BUCKETS
    ]
    actual_unparsed = [{"line": row.line, "text": row.text} for row in unparsed_plan_rows(markdown)]
    for row in in_scope_fixture_unparsed:
        assert row in actual_unparsed

    task_lines = {task.line for task in tasks}
    for row in actual_unparsed:
        assert row["text"].startswith(("- [ ] ", "- [x] "))
        assert priorities.get(row["line"]) in LAUNCH_BUCKETS
        assert row["line"] not in task_lines


def test_public_export_set_is_exactly_the_pinned_facade() -> None:
    """The whole point of the epic is that this set does not move while the files do."""
    public = {name for name in vars(flaitron) if not name.startswith("_")}
    assert public - INCIDENTAL - _bound_submodules(flaitron) == FACADE


def test_every_production_importer_takes_exactly_its_pinned_names() -> None:
    """Catches the half a façade pin alone misses: an importer editing its own list.

    A name can survive on the module and still break a caller that stopped asking
    for it — or start being asked for by a module that has no business with it.
    """
    assert {mod: _flaitron_imports(mod) for mod in IMPORTER_NAMES} == IMPORTER_NAMES


def test_every_imported_name_resolves_on_the_facade() -> None:
    """Ties the two pins together: no importer may name something outside FACADE."""
    imported = set().union(*IMPORTER_NAMES.values())
    assert imported <= FACADE
    for name in sorted(imported):
        assert hasattr(flaitron, name), name
