"""Last-run status card (CBN-033; caller-side since CBN-188.2). No live GitHub."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from test_store import green_suite

from caobunga.models import CLOSURE_OUTCOMES, Outcome, RunRecord
from caobunga.status_card import format_status, publish_status
from caobunga.store import Store

NOW = datetime(2026, 8, 19, tzinfo=UTC)


def _run(
    *,
    outcome: Outcome = Outcome.CLOSED,
    commit_sha: str | None = "a" * 40,
    next_task: str | None = "CBN-040",
    summary: str = "The reader picks the next task.",
) -> RunRecord:
    return RunRecord(
        project="app",
        task_id="CBN-011",
        outcome=outcome,
        commit_sha=commit_sha,
        closed_at=NOW,
        next_task=next_task,
        summary=summary,
        suite=green_suite() if outcome in CLOSURE_OUTCOMES else None,
    )


def test_format_status_omits_optional_fields() -> None:
    body = format_status(_run(outcome=Outcome.FAILED, commit_sha=None, next_task=None))
    assert body.startswith("# Caobunga status\n")
    assert "- **project:** app\n" in body
    assert "- **task:** CBN-011\n" in body
    assert "- **outcome:** failed\n" in body
    assert "- **summary:** The reader picks the next task.\n" in body
    assert "**commit:**" not in body
    assert "**next:**" not in body
    assert "transcript" not in body.lower()
    assert "## 📝" not in body


def test_format_status_includes_sha_and_next() -> None:
    body = format_status(_run())
    assert "- **commit:** " + ("a" * 40) in body
    assert "- **next:** CBN-040\n" in body


def test_publish_status_writes_the_caller_side_card(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    path = publish_status(store, _run())
    assert path == store.status_file("app")
    assert path.read_text(encoding="utf-8") == format_status(_run())


def test_publish_status_overwrites_rather_than_appends(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    publish_status(store, _run())
    publish_status(store, _run(summary="a later close"))
    body = store.status_file("app").read_text(encoding="utf-8")
    assert body.count("# Caobunga status") == 1
    assert "a later close" in body
