"""Daily fleet digest (CBN-032); local-day window (CBN-EPIC-95.4)."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_store import green_suite

from caobunga.digest import format_digest
from caobunga.models import (
    CLOSURE_OUTCOMES,
    ExecutionTier,
    ExternalReview,
    HermesVerdictCode,
    NewRow,
    Outcome,
    RegistryRecord,
    RunRecord,
    Suite,
    SuiteVerb,
)
from caobunga.store import Store

NOW = datetime(2026, 8, 19, 18, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the process timezone; the digest window is the operator's local day."""
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _register(store: Store, project_id: str, path: Path, *, enabled: bool = True) -> None:
    store.register(
        RegistryRecord(
            id=project_id,
            path=str(path.resolve()),
            tier=ExecutionTier.GATED,
            registered_at=NOW,
            enabled=enabled,
        )
    )


def _run(
    project: str,
    task_id: str,
    summary: str,
    *,
    closed_at: datetime = NOW,
    outcome: Outcome = Outcome.CLOSED,
) -> RunRecord:
    return RunRecord(
        project=project,
        task_id=task_id,
        outcome=outcome,
        commit_sha="a" * 40 if outcome is Outcome.CLOSED else None,
        closed_at=closed_at,
        summary=summary,
        suite=green_suite() if outcome in CLOSURE_OUTCOMES else None,
    )


def test_empty_registry(tmp_path: Path) -> None:
    body = format_digest(Store(tmp_path / "home"), now=NOW)
    assert body == "# Fleet digest — 2026-08-19\n\n(no registered projects)\n"


def test_empty_day_keeps_project_headings(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    body = format_digest(store, now=NOW)
    assert body == "# Fleet digest — 2026-08-19\n\n## app\n\n(no runs)\n"


def test_today_runs_only_and_summary_fields(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    app = tmp_path / "app"
    charts = tmp_path / "charts"
    app.mkdir()
    charts.mkdir()
    _register(store, "app", app)
    _register(store, "charts", charts, enabled=False)
    store.write_run(
        _run(
            "app",
            "CBN-031",
            "Epic rollup writer.",
            closed_at=datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC),
        )
    )
    store.write_run(
        _run(
            "app",
            "CBN-014",
            "Yesterday.",
            closed_at=datetime(2026, 8, 18, 23, 0, 0, tzinfo=UTC),
        )
    )
    store.write_run(
        _run(
            "app",
            "CBN-013",
            "Worker died.",
            outcome=Outcome.FAILED,
            closed_at=datetime(2026, 8, 19, 18, 0, 0, tzinfo=UTC),
        )
    )
    body = format_digest(store, now=NOW)
    assert body.startswith("# Fleet digest — 2026-08-19\n")
    assert "## app" in body
    assert "**CBN-031** (`closed`) — Epic rollup writer." in body
    assert "**CBN-013** (`failed`) — Worker died." in body
    assert "CBN-014" not in body
    assert "Yesterday." not in body
    assert "## charts" in body
    assert "(no runs)" in body
    assert "Phase 1" not in body
    assert "hermes" not in body.lower()
    assert body.index("## app") < body.index("## charts")
    assert body.index("CBN-031") < body.index("CBN-013")


def test_evening_runs_stay_in_the_operators_local_day(tmp_path: Path) -> None:
    """CBN-EPIC-95.3 F1: a UTC-day window empties at 20:00 for a UTC−4 operator.

    The three runs closed 20:50–20:57 UTC on Aug 23 — 16:50–16:57 local, a normal
    afternoon. Reading the digest at 21:03 local returned `(no runs)` because the
    UTC date had already rolled to Aug 24.
    """  # noqa: RUF002
    store = Store(tmp_path / "home")
    repo = tmp_path / "toolkit"
    repo.mkdir()
    _register(store, "toolkit", repo)
    store.write_run(
        _run(
            "toolkit",
            "TEST-4.1",
            "Slugify unit coverage.",
            closed_at=datetime(2026, 8, 23, 20, 50, 26, tzinfo=UTC),
        )
    )
    evening = datetime(2026, 8, 24, 1, 3, 0, tzinfo=UTC)  # 21:03 local, same workday
    body = format_digest(store, now=evening)
    assert body.startswith("# Fleet digest — 2026-08-23\n")
    assert "**TEST-4.1** (`closed`) — Slugify unit coverage." in body
    assert "(no runs)" not in body


def test_local_day_boundary_excludes_the_neighbouring_day(tmp_path: Path) -> None:
    """The window is still one calendar day — just the operator's, not UTC's."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "toolkit"
    repo.mkdir()
    _register(store, "toolkit", repo)
    store.write_run(
        _run(
            "toolkit",
            "TEST-3.9",
            "Late last night.",
            closed_at=datetime(2026, 8, 23, 3, 30, 0, tzinfo=UTC),  # 23:30 local Aug 22
        )
    )
    body = format_digest(store, now=datetime(2026, 8, 23, 18, 0, 0, tzinfo=UTC))
    assert body.startswith("# Fleet digest — 2026-08-23\n")
    assert "TEST-3.9" not in body
    assert "(no runs)" in body


def test_follow_ups_render_count_then_lines(tmp_path: Path) -> None:
    """CBN-121: a drain's discoveries reach the operator here, not only the archive."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(run.model_copy(update={"follow_ups": ["guard the marker", "file upstream"]}))
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n"
        "\n"
        "## app\n"
        "\n"
        "- **CBN-031** (`closed`) — Epic rollup writer.\n"
        "  - **follow-ups proposed (2):**\n"
        "    - guard the marker\n"
        "    - file upstream\n"
    )


def test_a_run_without_follow_ups_renders_exactly_as_before(tmp_path: Path) -> None:
    """No empty heading, no blank block — the field is invisible when unset."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    store.write_run(_run("app", "CBN-031", "Epic rollup writer."))
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n\n## app\n\n- **CBN-031** (`closed`) — Epic rollup writer.\n"
    )


def test_hermes_verdict_closes_the_bullet_after_the_worker_blocks(tmp_path: Path) -> None:
    """CBN-148: what the worker said and wrote first, the reviewer's judgment last."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(
        run.model_copy(
            update={
                "follow_ups": ["guard the marker"],
                "hermes_verdict": "Sound close; could not verify the commit from the brief.",
            }
        )
    )
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n"
        "\n"
        "## app\n"
        "\n"
        "- **CBN-031** (`closed`) — Epic rollup writer.\n"
        "  - **follow-ups proposed (1):**\n"
        "    - guard the marker\n"
        "  - **hermes:** Sound close; could not verify the commit from the brief.\n"
    )


def test_a_run_without_a_verdict_renders_exactly_as_before(tmp_path: Path) -> None:
    """A blank verdict normalizes to absent, so the line is omitted whole."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(run.model_copy(update={"hermes_verdict": "   "}))
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n\n## app\n\n- **CBN-031** (`closed`) — Epic rollup writer.\n"
    )


def test_new_rows_render_count_unmarked_and_state_per_row(tmp_path: Path) -> None:
    """CBN-138.3: the rows a drain filed, and whether any of them can dispatch yet."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(
        run.model_copy(
            update={
                "new_rows": [
                    NewRow(task_id="CBN-998", unattended=False),
                    NewRow(task_id="CBN-999", unattended=True),
                ]
            }
        )
    )
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n"
        "\n"
        "## app\n"
        "\n"
        "- **CBN-031** (`closed`) — Epic rollup writer.\n"
        "  - **new rows (2, 1 unmarked):**\n"
        "    - CBN-998 — unmarked\n"
        "    - CBN-999 — marked\n"
    )


def test_proposals_render_above_the_rows_they_produced(tmp_path: Path) -> None:
    """A proposal is what the worker said; a new row is what it wrote."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(
        run.model_copy(
            update={
                "follow_ups": ["file the guard"],
                "new_rows": [NewRow(task_id="CBN-999")],
            }
        )
    )
    body = format_digest(store, now=NOW)
    assert body.index("follow-ups proposed") < body.index("new rows")


def test_a_run_without_new_rows_renders_exactly_as_before(tmp_path: Path) -> None:
    """Byte-identical to the pre-CBN-138.3 bullet — no empty heading."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(run.model_copy(update={"new_rows": None}))
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n\n## app\n\n- **CBN-031** (`closed`) — Epic rollup writer.\n"
    )


def test_the_verdict_code_tags_the_task_id_line(tmp_path: Path) -> None:
    """CBN-160: the parsed code is a scannable tag, not folded into the `hermes:` prose."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(
        run.model_copy(
            update={
                "hermes_verdict": "VERDICT: accept — sound close.",
                "hermes_verdict_code": HermesVerdictCode.ACCEPT,
            }
        )
    )
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n"
        "\n"
        "## app\n"
        "\n"
        "- **CBN-031** (`closed`) [accept] — Epic rollup writer.\n"
        "  - **hermes:** VERDICT: accept — sound close.\n"
    )


def test_the_park_reason_tags_the_task_id_line(tmp_path: Path) -> None:
    """CBN-187: a blocked bullet names why it stopped, not just that it stopped."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Waiting on a 👁️ visual check.", outcome=Outcome.BLOCKED)
    store.write_run(run.model_copy(update={"park_reason": "visual-confirm"}))
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n"
        "\n"
        "## app\n"
        "\n"
        "- **CBN-031** (`blocked`) (park: visual-confirm) — Waiting on a 👁️ visual check.\n"
    )


def test_a_run_without_a_park_reason_renders_exactly_as_before(tmp_path: Path) -> None:
    """No code, no affix — the bullet is byte-identical to before CBN-187."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(run.model_copy(update={"park_reason": None}))
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n\n## app\n\n- **CBN-031** (`closed`) — Epic rollup writer.\n"
    )


def test_a_non_green_suite_tags_the_task_id_line(tmp_path: Path) -> None:
    """CBN-EPIC-202.5: a red/timeout/refused suite is the anomaly worth a scan."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    red_suite = Suite.from_verbs(
        [
            SuiteVerb(verb="test", exit_code=1, duration=1.0),
            SuiteVerb(verb="lint", exit_code=0, duration=0.1),
            SuiteVerb(verb="typecheck", exit_code=0, duration=0.2),
        ]
    )
    store.write_run(run.model_copy(update={"suite": red_suite}))
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n"
        "\n"
        "## app\n"
        "\n"
        "- **CBN-031** (`closed`) [suite: red] — Epic rollup writer.\n"
    )


def test_the_hermes_and_suite_tags_both_ride_the_task_id_line(tmp_path: Path) -> None:
    """Order is fixed: the Hermes code tag first, the suite tag second, then park."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    red_suite = Suite.from_verbs(
        [
            SuiteVerb(verb="test", exit_code=1, duration=1.0),
            SuiteVerb(verb="lint", exit_code=0, duration=0.1),
            SuiteVerb(verb="typecheck", exit_code=0, duration=0.2),
        ]
    )
    store.write_run(
        run.model_copy(update={"hermes_verdict_code": HermesVerdictCode.REJECT, "suite": red_suite})
    )
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n"
        "\n"
        "## app\n"
        "\n"
        "- **CBN-031** (`closed`) [reject] [suite: red] — Epic rollup writer.\n"
    )


def test_a_green_suite_tags_the_task_id_line_with_nothing(tmp_path: Path) -> None:
    """The common case: a green suite needs no comment, same as an unparked outcome."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(run)
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n\n## app\n\n- **CBN-031** (`closed`) — Epic rollup writer.\n"
    )


def test_a_missing_external_review_tags_the_task_id_line(tmp_path: Path) -> None:
    """CBN-257: only `missing` earns a tag; it rides after the suite tag, before park."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(run.model_copy(update={"external_review": ExternalReview.MISSING}))
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n"
        "\n"
        "## app\n"
        "\n"
        "- **CBN-031** (`closed`) [review: missing] — Epic rollup writer.\n"
    )


@pytest.mark.parametrize("review", [ExternalReview.TICKED, ExternalReview.NA])
def test_a_recorded_external_review_tags_nothing(tmp_path: Path, review: ExternalReview) -> None:
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(run.model_copy(update={"external_review": review}))
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n\n## app\n\n- **CBN-031** (`closed`) — Epic rollup writer.\n"
    )


def test_a_run_without_a_verdict_code_renders_exactly_as_before(tmp_path: Path) -> None:
    """No code, no tag — the bullet is byte-identical to before CBN-160."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "app"
    repo.mkdir()
    _register(store, "app", repo)
    run = _run("app", "CBN-031", "Epic rollup writer.")
    store.write_run(run.model_copy(update={"hermes_verdict_code": None}))
    body = format_digest(store, now=NOW)
    assert body == (
        "# Fleet digest — 2026-08-19\n\n## app\n\n- **CBN-031** (`closed`) — Epic rollup writer.\n"
    )
