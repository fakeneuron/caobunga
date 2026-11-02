"""Epic rollup writer (CBN-031)."""

from datetime import UTC, datetime
from pathlib import Path

from test_store import green_suite

from caobunga.digest import run_bullet
from caobunga.flaitron import PLAN_REL
from caobunga.models import (
    ExternalReview,
    HermesVerdictCode,
    Outcome,
    RegistryRecord,
    RunRecord,
    Suite,
    SuiteVerb,
)
from caobunga.report import report
from caobunga.rollup import (
    cohort_complete,
    cohort_for_task,
    format_rollup,
    maybe_write_rollup,
    parse_epic_cohorts,
)
from caobunga.store import Store

PLAN_EPIC1_DONE = """# plan

## High

<!-- Epic 1 — Mechanical plow — CBN-010–014 landed -->

## Medium

- [ ] **CBN-031** [medium] | epic-rollup — writer.

## Completed

- [x] **CBN-010** [medium] | local-registry — Completed 2026-08-19.
- [x] **CBN-011** [medium] | flaitron-reader — Completed 2026-08-19.
- [x] **CBN-012** [medium] | cao-client — Completed 2026-08-19.
- [x] **CBN-013** [heavy] | gated-run-loop — Completed 2026-08-19.
- [x] **CBN-014** [light] | cli-report — Completed 2026-08-19.
"""  # noqa: RUF001

PLAN_EPIC1_OPEN = PLAN_EPIC1_DONE.replace(
    "- [x] **CBN-014**",
    "- [ ] **CBN-014**",
)


def _run(task_id: str, summary: str) -> RunRecord:
    return RunRecord(
        project="app",
        task_id=task_id,
        outcome=Outcome.CLOSED,
        suite=green_suite(),
        commit_sha="a" * 40,
        closed_at=datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC),
        summary=summary,
    )


def test_parse_epic_range_from_comment() -> None:
    cohorts = parse_epic_cohorts(PLAN_EPIC1_DONE)
    assert len(cohorts) == 1
    cohort = cohorts[0]
    assert cohort.key == "CBN-010-014"
    assert cohort.task_ids == frozenset({f"CBN-{num:03d}" for num in range(10, 15)})
    assert "Epic 1" in cohort.title


PLAN_UNPADDED_RANGE = """# plan

## High

<!-- Epic A — Docs plow — DOC-1 – 5 landed -->

## Completed

- [x] **DOC-1** [light] | one — Completed 2026-08-24.
- [x] **DOC-2** [light] | two — Completed 2026-08-24.
- [x] **DOC-3** [light] | three — Completed 2026-08-24.
- [x] **DOC-4** [light] | four — Completed 2026-08-24.
- [x] **DOC-5** [light] | five — Completed 2026-08-24.
"""  # noqa: RUF001


def test_parse_epic_range_uses_plan_id_padding() -> None:
    """CBN-099. Unpadded PLAN ids must match the comment range, not DOC-001…"""
    cohorts = parse_epic_cohorts(PLAN_UNPADDED_RANGE)
    assert len(cohorts) == 1
    cohort = cohorts[0]
    assert cohort.key == "DOC-1-5"
    assert cohort.task_ids == frozenset({f"DOC-{n}" for n in range(1, 6)})
    assert cohort_complete(cohort, PLAN_UNPADDED_RANGE)
    assert cohort_for_task(cohorts, "DOC-3") is cohort
    assert cohort_for_task(cohorts, "DOC-003") is None


def test_cohort_complete_when_all_checked() -> None:
    cohorts = parse_epic_cohorts(PLAN_EPIC1_DONE)
    assert cohort_complete(cohorts[0], PLAN_EPIC1_DONE)
    assert not cohort_complete(cohorts[0], PLAN_EPIC1_OPEN)


def test_format_rollup_uses_run_summaries_only() -> None:
    cohorts = parse_epic_cohorts(PLAN_EPIC1_DONE)
    runs = [
        _run("CBN-010", "Registry landed."),
        _run("CBN-014", "CLI dumps YAML."),
    ]
    body = format_rollup("app", cohorts[0], runs)
    assert body.startswith("# Epic rollup — app — CBN-010-014\n")
    assert "**CBN-010** (`closed`) — Registry landed." in body
    assert "**CBN-014** (`closed`) — CLI dumps YAML." in body
    assert "Phase 1" not in body


def test_format_rollup_bullet_carries_the_digest_tags() -> None:
    """CBN-402. A red / unreviewed / parked run reads the same in rollup and digest."""
    cohorts = parse_epic_cohorts(PLAN_EPIC1_DONE)
    red_suite = Suite.from_verbs(
        [
            SuiteVerb(verb="test", exit_code=1, duration=1.0),
            SuiteVerb(verb="lint", exit_code=0, duration=0.1),
            SuiteVerb(verb="typecheck", exit_code=0, duration=0.2),
        ]
    )
    run = _run("CBN-010", "Registry landed.").model_copy(
        update={
            "suite": red_suite,
            "external_review": ExternalReview.MISSING,
            "park_reason": "visual-confirm",
            "hermes_verdict_code": HermesVerdictCode.REJECT,
        }
    )
    body = format_rollup("app", cohorts[0], [run])
    expected = (
        "- **CBN-010** (`closed`) [reject] [suite: red] [review: missing]"
        " (park: visual-confirm)"
        " — Registry landed."
    )
    assert run_bullet(run) == expected
    assert expected in body.splitlines()


def test_range_outside_an_html_comment_is_not_a_cohort() -> None:
    """CBN-402. CONTRACT reads ranges from PLAN comments only, not task-row prose."""
    plan = PLAN_EPIC1_DONE.replace(
        "<!-- Epic 1 — Mechanical plow — CBN-010–014 landed -->",  # noqa: RUF001
        "- [ ] **CBN-099** [light] | note — follows CBN-010–014.",  # noqa: RUF001
    )
    assert parse_epic_cohorts(plan) == []


def test_range_in_a_fenced_comment_is_not_a_cohort() -> None:
    """CBN-402. A fenced `<!-- … -->` is an example, not a declaration."""
    plan = PLAN_EPIC1_DONE.replace(
        "<!-- Epic 1 — Mechanical plow — CBN-010–014 landed -->",  # noqa: RUF001
        "```markdown\n<!-- Epic 1 — Mechanical plow — CBN-010–014 landed -->\n```",  # noqa: RUF001
    )
    assert parse_epic_cohorts(plan) == []


def test_fence_line_inside_a_comment_opens_no_fence() -> None:
    """CBN-402 review. Comments mask before fences, as in the reader's PLAN walk."""
    plan = PLAN_EPIC1_DONE.replace(
        "<!-- Epic 1",
        "<!-- commented-out example\n```\n-->\n\n<!-- Epic 1",
    )
    cohorts = parse_epic_cohorts(plan)
    assert [cohort.key for cohort in cohorts] == ["CBN-010-014"]
    assert cohorts[0].title == "Epic 1"


def test_line_separators_in_prose_do_not_shift_the_fence_mask() -> None:
    """CBN-402 review. `splitlines` breaks on more than `\\n`; offsets must agree.

    Two extra breaks above a fence would land a `\\n`-counted index on the fenced
    `x` line two above the comment, dropping a live declaration.
    """
    plan = PLAN_EPIC1_DONE.replace("# plan\n", "# plan\nprose\u2028more\fend\n").replace(
        "<!-- Epic 1", "```\nx\n```\n<!-- Epic 1"
    )
    assert [cohort.key for cohort in parse_epic_cohorts(plan)] == ["CBN-010-014"]


def test_multi_line_comment_title_is_its_first_line() -> None:
    """CBN-402 review. A comment body is not the title; its lede line is."""
    plan = PLAN_EPIC1_DONE.replace(
        "<!-- Epic 1 — Mechanical plow — CBN-010–014 landed -->",  # noqa: RUF001
        "<!--\nEpic 1: CBN-010–014 landed\nsee notes\n-->",  # noqa: RUF001
    )
    assert parse_epic_cohorts(plan)[0].title == "Epic 1: CBN-010–014 landed"  # noqa: RUF001


def test_epic_id_range_is_not_read_as_area_epic() -> None:
    """CBN-402. The start is a whole grammar id; `CBN-EPIC-16–18` is no `EPIC-16-18`."""  # noqa: RUF002
    plan = PLAN_EPIC1_DONE.replace("CBN-010–014", "CBN-EPIC-16–18")  # noqa: RUF001
    assert parse_epic_cohorts(plan) == []


def test_spaced_hyphen_is_a_title_separator_not_a_range() -> None:
    """CBN-408. `CBN-050 - 2026` would otherwise mint a 1,977-id cohort."""
    plan = PLAN_EPIC1_DONE.replace(
        "<!-- Epic 1", "<!-- Rollout note CBN-050 - 2026 rollout -->\n\n<!-- Epic 1"
    )
    assert [cohort.key for cohort in parse_epic_cohorts(plan)] == ["CBN-010-014"]


def test_unspaced_hyphen_and_spaced_en_dash_still_declare_ranges() -> None:
    """CBN-408. Only the spaced hyphen lost its range reading."""
    for spelling in ("CBN-010-014", "CBN-010 – 014", "CBN-010–014"):  # noqa: RUF001
        plan = PLAN_EPIC1_DONE.replace("CBN-010–014", spelling)  # noqa: RUF001
        assert [cohort.key for cohort in parse_epic_cohorts(plan)] == ["CBN-010-014"], spelling


def test_title_splits_on_a_spaced_en_dash_but_not_inside_a_range() -> None:
    """CBN-408. A spaced `–` separates the title; a range's own dash never does."""  # noqa: RUF002
    plan = PLAN_EPIC1_DONE.replace(
        "<!-- Epic 1 — Mechanical plow — CBN-010–014 landed -->",  # noqa: RUF001
        "<!-- Epic 1 – CBN-010–014 landed -->",  # noqa: RUF001
    )
    assert parse_epic_cohorts(plan)[0].title == "Epic 1"
    plan = PLAN_EPIC1_DONE.replace(
        "<!-- Epic 1 — Mechanical plow — CBN-010–014 landed -->",  # noqa: RUF001
        "<!-- CBN-010 – 014 plow landed -->",  # noqa: RUF001
    )
    assert parse_epic_cohorts(plan)[0].title == "CBN-010 – 014 plow landed"  # noqa: RUF001
    plan = PLAN_EPIC1_DONE.replace(
        "<!-- Epic 1 — Mechanical plow — CBN-010–014 landed -->",  # noqa: RUF001
        "<!-- Epic 1 –CBN-010–014 landed -->",  # noqa: RUF001
    )
    assert parse_epic_cohorts(plan)[0].title == "Epic 1 –CBN-010–014 landed"  # noqa: RUF001


def test_unclosed_comment_in_a_fence_does_not_swallow_the_next_one() -> None:
    """CBN-408. A fenced `<!--` opens no comment, so the live one after stays a cohort."""
    plan = PLAN_EPIC1_DONE.replace(
        "<!-- Epic 1", "```markdown\n<!-- unclosed example\n```\n\n<!-- Epic 1"
    )
    cohorts = parse_epic_cohorts(plan)
    assert [cohort.key for cohort in cohorts] == ["CBN-010-014"]
    assert cohorts[0].title == "Epic 1"


def test_maybe_write_rollup_on_last_epic_close(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    for task_id, summary in (
        ("CBN-010", "Registry landed."),
        ("CBN-011", "Reader landed."),
        ("CBN-012", "CAO landed."),
        ("CBN-013", "Loop landed."),
        ("CBN-014", "CLI landed."),
    ):
        store.write_run(_run(task_id, summary))
    path = maybe_write_rollup(store, "app", "CBN-014", PLAN_EPIC1_DONE)
    assert path is not None
    assert path.parent.name == "app"
    text = path.read_text(encoding="utf-8")
    assert "CBN-010-014" in text
    assert "**CBN-014**" in text
    assert store.last_rollup("app") == text
    assert maybe_write_rollup(store, "app", "CBN-014", PLAN_EPIC1_DONE) is None


def test_maybe_write_skips_when_cohort_incomplete(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    store.write_run(_run("CBN-014", "CLI landed."))
    assert maybe_write_rollup(store, "app", "CBN-014", PLAN_EPIC1_OPEN) is None


def test_cohort_for_task() -> None:
    cohorts = parse_epic_cohorts(PLAN_EPIC1_DONE)
    assert cohort_for_task(cohorts, "CBN-012") is cohorts[0]
    assert cohort_for_task(cohorts, "CBN-031") is None


PLAN_TOOLKIT_SHAPE = """# plan

## High

- [ ] **DOC-EPIC-20** [light] | flaitron-doc-drift — three doc-only drift fixes.
  - [x] **DOC-20.1** [light] | flaitron-pin-line — Completed 2026-08-22.
  - [x] **DOC-20.2** [light] | tasknote-readme-layout — Completed 2026-08-22.
  - [x] **DOC-20.3** [light] | plan-task-areas-drift — Completed 2026-08-22.

## Completed

(none)
"""


def test_cohort_detects_unpadded_children_under_an_epic_parent() -> None:
    """CBN-EPIC-9.5 F6. The exact live shape that produced 0 rollups after 3 closes.

    `SPEC/epic.md` files children as `AREA-N.sub` under an `AREA-EPIC-N` parent, so
    a literal `DOC-EPIC-20.` prefix matches none of them.
    """
    cohorts = parse_epic_cohorts(PLAN_TOOLKIT_SHAPE)
    assert len(cohorts) == 1
    cohort = cohorts[0]
    assert cohort.task_ids == frozenset({"DOC-20.1", "DOC-20.2", "DOC-20.3"})
    assert cohort_for_task(cohorts, "DOC-20.2") is cohort
    assert cohort_complete(cohort, PLAN_TOOLKIT_SHAPE)


def test_cohort_detects_epic_form_children_too() -> None:
    """CBN-EPIC-9.5 F6. Caobunga's own PLAN uses the `AREA-EPIC-N.sub` child form."""
    plan = PLAN_TOOLKIT_SHAPE.replace("DOC-20.", "DOC-EPIC-20.")
    cohorts = parse_epic_cohorts(plan)
    assert len(cohorts) == 1
    assert cohorts[0].task_ids == frozenset({"DOC-EPIC-20.1", "DOC-EPIC-20.2", "DOC-EPIC-20.3"})


def test_rollup_writes_for_a_cohort_keyed_by_epic_stem(tmp_path: Path) -> None:
    """CBN-EPIC-9.5 F6. Rollup cohort keying and `epic_stem` must agree on the cohort."""
    from caobunga.flaitron import epic_stem

    store = Store(tmp_path)
    for task_id in ("DOC-20.1", "DOC-20.2", "DOC-20.3"):
        assert epic_stem(task_id) == "DOC-20"
        store.write_run(
            RunRecord(
                project="toolkit",
                task_id=task_id,
                outcome=Outcome.CLOSED,
                suite=green_suite(),
                commit_sha="b" * 40,
                closed_at=datetime(2026, 8, 22, tzinfo=UTC),
                summary=f"{task_id} closed.",
            )
        )
    path = maybe_write_rollup(store, "toolkit", "DOC-20.3", PLAN_TOOLKIT_SHAPE)
    assert path is not None
    body = path.read_text(encoding="utf-8")
    assert "DOC-20.1" in body
    assert "DOC-20.3" in body


def test_report_skips_the_rollup_on_a_non_utf8_plan(tmp_path: Path) -> None:
    """An unreadable PLAN skips the derived rollup, not the run (CBN-318)."""
    store = Store(tmp_path / "home")
    for task_id in ("CBN-010", "CBN-011", "CBN-012", "CBN-013", "CBN-014"):
        store.write_run(_run(task_id, "landed."))
    root = tmp_path / "repo"
    (root / PLAN_REL).parent.mkdir(parents=True)
    (root / PLAN_REL).write_bytes(PLAN_EPIC1_DONE.encode("utf-8") + b"\xff\xfe\n")
    record = RegistryRecord(
        id="app", path=str(root), registered_at=datetime(2026, 8, 19, tzinfo=UTC)
    )
    lines: list[str] = []

    report(
        store,
        record,
        _run("CBN-014", "landed."),
        echo=lines.append,
        publish=lambda _store, _run: tmp_path / "card.md",
    )

    assert store.last_rollup("app") is None
    assert "rollup: skipped (.flaitron/PLAN.md unreadable)" in lines


def test_trailing_unclosed_comment_is_no_comment() -> None:
    """CBN-408. A `<!--` with no `-->` after it opens nothing; earlier ones stand."""
    plan = PLAN_EPIC1_DONE + "\n<!-- CBN-020–022 never closed\n"  # noqa: RUF001
    assert [cohort.key for cohort in parse_epic_cohorts(plan)] == ["CBN-010-014"]
