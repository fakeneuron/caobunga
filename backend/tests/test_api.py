"""Read-only status API. Isolated via CAOBUNGA_HOME."""

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_loop import _git
from test_store import green_suite

from caobunga.api import ENV_API_RELOAD, UI_ORIGINS, api_reload_enabled, current_task
from caobunga.errors import UnknownProjectError, UserError
from caobunga.flaitron import NextTask, OpenTasknote, PlanPriority, ProjectRead
from caobunga.models import (
    CLOSURE_OUTCOMES,
    Escalation,
    ExecutionTier,
    HermesReview,
    HermesVerdictCode,
    Outcome,
    RegistryRecord,
    RunRecord,
)
from caobunga.store import Store
from main import app

PLAN = """# sample

## High

- [ ] **CBN-011** [medium] | flaitron-reader — reader work.

## Medium

(none)

## Low

(none)
"""


# Two unmarked open rows, one marked, plus rows the base dimensions exclude:
# a checked row, a `## Future Opportunities` row, and a parent-epic tracking
# row. Only the three open launch rows may reach `coverage`.
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


@pytest.fixture
def api_client(caobunga_home: Path) -> TestClient:  # noqa: ARG001
    return TestClient(app, base_url="http://127.0.0.1")


def _closed_at() -> datetime:
    return datetime(2026, 8, 19, tzinfo=UTC)


def _register(
    home: Path,
    project_id: str,
    path: Path,
    *,
    enabled: bool = True,
    tier: ExecutionTier = ExecutionTier.GATED,
) -> RegistryRecord:
    store = Store(home)
    record = RegistryRecord(
        id=project_id,
        path=str(path.resolve()),
        tier=tier,
        registered_at=_closed_at(),
        enabled=enabled,
    )
    return store.register(record)


def _run(
    project: str,
    task_id: str,
    summary: str,
    *,
    stamp_hour: int = 0,
    outcome: Outcome = Outcome.CLOSED,
    escalation: Escalation | None = None,
    started_at: datetime | None = None,
    hermes_review: HermesReview | None = None,
) -> RunRecord:
    return RunRecord(
        project=project,
        task_id=task_id,
        outcome=outcome,
        commit_sha="a" * 40 if outcome is Outcome.CLOSED else None,
        started_at=started_at,
        closed_at=datetime(2026, 8, 19, stamp_hour, tzinfo=UTC),
        escalation_reason=escalation,
        hermes_review=hermes_review,
        suite=green_suite() if outcome in CLOSURE_OUTCOMES else None,
        provider="grok_cli",
        model="grok-4.6",
        plan_model="medium",
        next_task="CBN-012",
        tasknote_path=f".flaitron/tasknote/archive/cbn/{task_id}.md",
        summary=summary,
    )


def _flaitron_repo(tmp_path: Path, *, in_progress: bool = False, plan: str = PLAN) -> Path:
    repo = tmp_path / "worker"
    notes = repo / ".flaitron" / "tasknote"
    notes.mkdir(parents=True)
    (repo / ".flaitron" / "PLAN.md").write_text(plan, encoding="utf-8")
    if in_progress:
        (notes / "CBN-011.md").write_text(
            "---\ntitle: t\nstatus: in-progress\ncreated: 2026-08-19\n---\n",
            encoding="utf-8",
        )
    return repo


def test_health_still_ok(api_client: TestClient) -> None:
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_empty(api_client: TestClient) -> None:
    response = api_client.get("/api/projects")
    assert response.status_code == 200
    assert response.json() == {"projects": []}


def test_list_and_detail_last_run_and_reports(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    repo = _flaitron_repo(tmp_path)
    _register(caobunga_home, "webapp", repo)
    store = Store(caobunga_home)
    store.write_run(_run("webapp", "CBN-010", "Registry landed.", stamp_hour=1))
    later = _run("webapp", "CBN-011", "Reader landed.", stamp_hour=2)
    store.write_run(later)
    store.save_rollup("webapp", "20260819T020000Z", "CBN-010-014", "# rollup\n")

    listed = api_client.get("/api/projects")
    assert listed.status_code == 200
    rows = listed.json()["projects"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "webapp"
    assert row["tier"] == "gated"
    assert row["enabled"] is True
    assert row["current"] is None
    assert row["last_run"] == {
        "task_id": "CBN-011",
        "outcome": "closed",
        "closed_at": "2026-08-19T02:00:00Z",
        "summary": "Reader landed.",
    }
    assert "transcript" not in listed.text.lower()
    assert "## 📝" not in listed.text

    detail = api_client.get("/api/projects/webapp")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == "webapp"
    assert body["path"] == str(repo.resolve())
    assert body["current"] is None
    assert body["last_run"]["task_id"] == "CBN-011"
    assert body["last_run"]["commit_sha"] == "a" * 40
    assert body["last_run"]["outcome"] == "closed"
    assert body["reports"] == {"last_rollup": "# rollup\n"}
    assert "transcript" not in detail.text.lower()


def test_detail_carries_the_hermes_verdict_code_with_no_endpoint_change(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """CBN-160: the field rides the existing full run-record dump, unlike `coverage`/`tree`."""
    repo = _flaitron_repo(tmp_path)
    _register(caobunga_home, "webapp", repo)
    store = Store(caobunga_home)
    store.write_run(
        _run(
            "webapp",
            "CBN-011",
            "Reader landed.",
            hermes_review=HermesReview.REVIEWED,
        ).model_copy(
            update={
                "hermes_verdict": "VERDICT: accept — sound close.",
                "hermes_verdict_code": HermesVerdictCode.ACCEPT,
            }
        )
    )
    body = api_client.get("/api/projects/webapp").json()
    assert body["last_run"]["hermes_verdict_code"] == "accept"
    assert body["runs"][0]["hermes_verdict_code"] == "accept"


def test_detail_run_history_newest_first_with_signals(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """`runs` is the full record per entry, newest-first, with the CBN-EPIC-12.2 signals."""
    repo = _flaitron_repo(tmp_path)
    _register(caobunga_home, "webapp", repo)
    store = Store(caobunga_home)
    store.write_run(_run("webapp", "CBN-010", "Registry landed.", stamp_hour=1))
    store.write_run(
        _run(
            "webapp",
            "CBN-011",
            "Reader landed.",
            stamp_hour=2,
            started_at=datetime(2026, 8, 19, 1, 30, tzinfo=UTC),
            hermes_review=HermesReview.REVIEWED,
        )
    )
    store.write_run(
        _run(
            "webapp",
            "CBN-012",
            "Worker timed out.",
            stamp_hour=3,
            outcome=Outcome.FAILED,
            escalation=Escalation.TIMEOUT,
        )
    )

    body = api_client.get("/api/projects/webapp").json()
    runs = body["runs"]
    assert [run["task_id"] for run in runs] == ["CBN-012", "CBN-011", "CBN-010"]

    failed, reader, oldest = runs
    assert failed["outcome"] == "failed"
    assert failed["escalation_reason"] == "timeout"
    assert "commit_sha" not in failed
    assert reader["started_at"] == "2026-08-19T01:30:00Z"
    assert reader["hermes_review"] == "reviewed"
    assert reader["commit_sha"] == "a" * 40
    # exclude_none, like detail `last_run`: unset optional signals are absent.
    assert "started_at" not in oldest
    assert "hermes_review" not in oldest

    # The fleet list and detail `last_run` are untouched by this field.
    assert body["last_run"]["task_id"] == "CBN-012"
    row = api_client.get("/api/projects").json()["projects"][0]
    assert "runs" not in row
    assert "run_counts" not in row


def test_detail_run_counts_tally_all_outcomes(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    repo = _flaitron_repo(tmp_path)
    _register(caobunga_home, "webapp", repo)
    store = Store(caobunga_home)
    store.write_run(_run("webapp", "CBN-010", "Closed.", stamp_hour=1))
    store.write_run(_run("webapp", "CBN-011", "Closed.", stamp_hour=2))
    store.write_run(
        _run(
            "webapp",
            "CBN-012",
            "Parked.",
            stamp_hour=3,
            outcome=Outcome.BLOCKED,
            escalation=Escalation.WORKER_PARKED,
        )
    )
    store.write_run(
        _run("webapp", "CBN-013", "Nothing to do.", stamp_hour=4, outcome=Outcome.NO_TASKNOTE)
    )

    body = api_client.get("/api/projects/webapp").json()
    assert body["run_counts"] == {
        "closed": 2,
        "de-scoped": 0,
        "blocked": 1,
        "failed": 0,
        "no-tasknote": 1,
    }
    assert sum(body["run_counts"].values()) == len(body["runs"])


def test_detail_run_history_empty_state(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """No runs is `[]` and an all-zero tally, never `null`."""
    repo = _flaitron_repo(tmp_path)
    _register(caobunga_home, "webapp", repo)
    body = api_client.get("/api/projects/webapp").json()
    assert body["last_run"] is None
    assert body["runs"] == []
    assert body["run_counts"] == {outcome.value: 0 for outcome in Outcome}


# CBN-106 — the fleet reads must survive one file they cannot parse. Kept in
# lockstep with `test_store.UNREADABLE_RUNS`; the store tests prove the skip,
# these prove it reaches the three endpoints CBN-EPIC-12.N found failing.
UNREADABLE_RUN_SHAPES = ["yaml-syntax", "not-a-mapping", "schema-invalid"]


def _write_unreadable_run(home: Path, project: str, shape: str) -> None:
    """A bad file stamped after every healthy record, so `last_run` reads it first."""
    bodies = {
        "yaml-syntax": "outcome: [unbalanced\n",
        "not-a-mapping": "just a string\n",
        "schema-invalid": (
            "schema_version: 1\n"
            f"project: {project}\n"
            "task_id: CBN-999\n"
            "outcome: exploded\n"
            "closed_at: 2026-08-19T09:00:00Z\n"
            "summary: bad\n"
        ),
    }
    runs = Store(home).runs_dir(project)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "20260819T090000000Z-CBN-999.yaml").write_text(bodies[shape], encoding="utf-8")


@pytest.mark.parametrize("shape", UNREADABLE_RUN_SHAPES)
def test_unreadable_run_does_not_fail_the_reads(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path, shape: str
) -> None:
    """One bad file in one project used to 400/500 all three reads, fleet-wide.

    `/api/stats` folds every project's records, so the blast radius crossed
    project boundaries — a bad file under `webapp` failed the read for `other`
    too. Both projects are registered here to pin that.
    """
    repo = _flaitron_repo(tmp_path)
    _register(caobunga_home, "webapp", repo)
    _register(caobunga_home, "other", repo)
    store = Store(caobunga_home)
    store.write_run(_run("webapp", "CBN-010", "Registry landed.", stamp_hour=1))
    store.write_run(_run("other", "CBN-020", "Routing landed.", stamp_hour=1))
    _write_unreadable_run(caobunga_home, "webapp", shape)

    assert api_client.get("/api/projects").status_code == 200
    assert api_client.get("/api/projects/webapp").status_code == 200
    assert api_client.get("/api/stats").status_code == 200


@pytest.mark.parametrize("shape", UNREADABLE_RUN_SHAPES)
def test_unreadable_run_is_absent_and_invariants_hold(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path, shape: str
) -> None:
    """Skipped means absent everywhere, not absent from some folds and not others.

    The two CONTRACT §"Status API" invariants — `sum(run_counts) == len(runs)`
    and `runs[0] == last_run` — only survive because every consumer folds the
    same skipped set, which is why the guard lives in `Store`.
    """
    repo = _flaitron_repo(tmp_path)
    _register(caobunga_home, "webapp", repo)
    store = Store(caobunga_home)
    store.write_run(_run("webapp", "CBN-010", "Registry landed.", stamp_hour=1))
    store.write_run(_run("webapp", "CBN-011", "Reader landed.", stamp_hour=2))
    _write_unreadable_run(caobunga_home, "webapp", shape)

    body = api_client.get("/api/projects/webapp").json()
    assert [run["task_id"] for run in body["runs"]] == ["CBN-011", "CBN-010"]
    assert sum(body["run_counts"].values()) == len(body["runs"])
    assert body["last_run"] == body["runs"][0]

    # Not "no runs": the unreadable file is the newest, and falling back to the
    # newest readable record is what keeps the list/detail `last_run` identity.
    listed = api_client.get("/api/projects").json()["projects"][0]
    assert listed["last_run"]["task_id"] == "CBN-011"

    stats = api_client.get("/api/stats").json()
    assert stats["runs"] == 2


def test_current_in_progress(api_client: TestClient, caobunga_home: Path, tmp_path: Path) -> None:
    repo = _flaitron_repo(tmp_path, in_progress=True)
    _register(caobunga_home, "webapp", repo)
    body = api_client.get("/api/projects/webapp").json()
    assert body["current"] == {
        "task_id": "CBN-011",
        "status": "in-progress",
        "reason": "in-progress-override",
        "plan_model": "medium",
        "shortname": "flaitron-reader",
    }
    row = api_client.get("/api/projects").json()["projects"][0]
    assert row["current"]["task_id"] == "CBN-011"


def test_current_null_when_path_unreadable(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "gone"
    missing.mkdir()
    _register(caobunga_home, "webapp", missing)
    missing.rmdir()
    body = api_client.get("/api/projects/webapp").json()
    assert body["current"] is None
    assert body["last_run"] is None


def test_unknown_project_404(api_client: TestClient) -> None:
    response = api_client.get("/api/projects/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "unknown project: nope"}


def test_404_maps_by_type_not_message(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-263: a plain `UserError` that merely *reads* like an unknown project
    is a 400 — only the typed `UnknownProjectError` is a 404."""

    def _lookalike(_self: Store, project_id: str) -> RegistryRecord:
        raise UserError(f"unknown project: {project_id}")

    monkeypatch.setattr(Store, "get", _lookalike)
    response = api_client.get("/api/projects/nope")
    assert response.status_code == 400
    assert response.json() == {"detail": "unknown project: nope"}


def test_store_get_raises_typed_unknown_project(caobunga_home: Path) -> None:
    with pytest.raises(UnknownProjectError) as caught:
        Store(caobunga_home).get("nope")
    assert caught.value.project_id == "nope"
    assert caught.value.exit_code == 2


def test_post_projects_method_not_allowed(api_client: TestClient) -> None:
    assert api_client.post("/api/projects").status_code == 405
    assert api_client.post("/api/projects/webapp").status_code == 405
    assert api_client.post("/api/digest").status_code == 405


def test_digest_matches_cli_shape(
    api_client: TestClient,
    caobunga_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The digest window is the operator's local day (CBN-EPIC-95.4), so pin the
    # zone and stamp the run inside the same local day as `now`.
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    monkeypatch.setattr("caobunga.digest.utc_now", lambda: datetime(2026, 8, 19, 12, tzinfo=UTC))
    repo = _flaitron_repo(tmp_path)
    _register(caobunga_home, "webapp", repo)
    Store(caobunga_home).write_run(_run("webapp", "CBN-011", "Reader landed.", stamp_hour=12))
    response = api_client.get("/api/digest")
    assert response.status_code == 200
    markdown = response.json()["markdown"]
    assert markdown.startswith("# Fleet digest — 2026-08-19\n")
    assert "## webapp" in markdown
    assert "**CBN-011** (`closed`) — Reader landed." in markdown


@pytest.mark.parametrize("origin", UI_ORIGINS)
def test_cors_allows_ui_origins(api_client: TestClient, origin: str) -> None:
    response = api_client.get("/api/projects", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_cors_refuses_foreign_origin(api_client: TestClient) -> None:
    response = api_client.get("/api/projects", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_preflight_refuses_foreign_origin(api_client: TestClient) -> None:
    headers = {"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    response = api_client.options("/api/projects", headers=headers)
    assert "access-control-allow-origin" not in response.headers


def test_no_origin_header_is_unaffected(api_client: TestClient) -> None:
    """Non-browser clients (curl, the Vite proxy hop) send no Origin."""
    response = api_client.get("/api/projects")
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    assert api_client.get("/health").json() == {"status": "ok"}


def test_reload_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_API_RELOAD, raising=False)
    assert api_reload_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on", " on "])
def test_reload_enabled_for_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV_API_RELOAD, value)
    assert api_reload_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "garbage", ""])
def test_reload_disabled_for_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV_API_RELOAD, value)
    assert api_reload_enabled() is False


def test_current_reads_in_flight_not_the_picker_candidate() -> None:
    """The dashboard's current task is the display half, not the dispatch half.

    `read_project` is the seam: `candidate` answers "what would be dispatched
    next" and `in_flight` answers "what is open right now". They coincide on an
    attended read today, so this pins the *source* rather than the value — the
    picker's preference changed once (CBN-EPIC-17.3) and would otherwise have
    blanked the dashboard while the operator was mid-task (CBN-040 / CBN-011).
    """
    note = OpenTasknote(
        task_id="CBN-012",
        path=".flaitron/tasknote/CBN-012.md",
        status="in-progress",
    )
    read = ProjectRead(
        tasks=[],
        open_tasknotes=[note],
        candidate=NextTask(
            task_id="CBN-011",
            priority=PlanPriority.HIGH,
            reason="spec-selection",
        ),
        in_flight=NextTask(
            task_id="CBN-012",
            plan_model="medium",
            shortname="cao-client",
            priority=PlanPriority.HIGH,
            reason="in-progress-override",
            tasknote=note,
        ),
    )
    assert current_task(read) == {
        "task_id": "CBN-012",
        "status": "in-progress",
        "reason": "in-progress-override",
        "plan_model": "medium",
        "shortname": "cao-client",
    }


def test_coverage_counts_on_list_and_denials_on_detail(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """Aggregate surfaces carry counts; the project surface names the ids.

    Same detail-only split `runs` / `run_counts` already use (CBN-EPIC-12.3):
    the fleet view answers "which repos need seeding", the project view answers
    "which lines".
    """
    repo = _flaitron_repo(tmp_path, plan=COVERAGE_PLAN)
    _register(caobunga_home, "webapp", repo)

    row = api_client.get("/api/projects").json()["projects"][0]
    assert row["coverage"] == {"open": 3, "marked": 1, "denied": 2, "unparsed": 0}

    body = api_client.get("/api/projects/webapp").json()
    assert body["coverage"]["open"] == 3
    assert body["coverage"]["marked"] == 1
    assert [denial["task_id"] for denial in body["coverage"]["denials"]] == [
        "CBN-002",
        "CBN-003",
    ]
    assert all(denial["reason"].startswith("unmarked — ") for denial in body["coverage"]["denials"])
    # The seeding detail rides the same detail-only split (CBN-119.3): the list
    # endpoint carries counts, the project endpoint carries the lines to edit.
    first = body["coverage"]["denials"][0]
    assert first["line"] > 0
    assert first["text"].startswith("- [ ] **")
    assert "[unattended]" in first["seeded_line"]
    # `marked_tasks` (CBN-125) rides the same split: the exact-dict assertion on
    # the list row above is what pins it off the counts-only endpoint.
    assert body["coverage"]["marked_tasks"] == ["CBN-001"]


def test_coverage_unparsed_rows_on_detail_only(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """A grammar-dropped row (CBN-115) is a count on the list, a named line on detail."""
    dropped = "- [ ] **CBN-010** [xheavy] [!unattended] | dropped — bang-shape."
    plan = COVERAGE_PLAN.replace("- [ ] **CBN-002**", f"{dropped}\n- [ ] **CBN-002**")
    repo = _flaitron_repo(tmp_path, plan=plan)
    _register(caobunga_home, "webapp", repo)

    row = api_client.get("/api/projects").json()["projects"][0]
    assert row["coverage"]["unparsed"] == 1
    assert "unparsed_rows" not in row["coverage"]

    detail = api_client.get("/api/projects/webapp").json()["coverage"]
    assert detail["unparsed"] == 1
    assert detail["unparsed_rows"][0]["text"].startswith("- [ ] **CBN-010**")
    assert detail["unparsed_rows"][0]["line"] > 0


def test_coverage_is_null_for_an_unreadable_path(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """A moved or non-Flaitron path costs that one field, never the response."""
    missing = tmp_path / "gone"
    missing.mkdir()
    _register(caobunga_home, "webapp", missing)

    row = api_client.get("/api/projects").json()["projects"][0]
    assert row["coverage"] is None
    assert api_client.get("/api/projects/webapp").json()["coverage"] is None


TREE_PLAN = """# sample

## High

- [ ] **CBN-001** [medium] [unattended] | head — first in dispatch order.
- [ ] **CBN-002** [medium] [unattended] | second — carries the open note.
"""


def _git_worker_repo(tmp_path: Path, plan: str) -> Path:
    repo = _flaitron_repo(tmp_path, plan=plan)
    _git(repo, "init")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--no-gpg-sign",
        "-m",
        "init",
    )
    return repo


def test_tree_verdict_on_list_and_the_coupling_on_detail(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """Same aggregate-vs-detail split coverage uses (CBN-130).

    The registered tier here is `gated`, so the in-progress override lifts
    `CBN-002` to candidate and its own note is exempt — the tree reads clean with
    an uncommitted file present, which is the exemption doing its job.
    """
    repo = _git_worker_repo(tmp_path, TREE_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-002.md").write_text(
        "---\nstatus: in-progress\n---\n", encoding="utf-8"
    )
    _register(caobunga_home, "webapp", repo)

    row = api_client.get("/api/projects").json()["projects"][0]
    assert row["tree"] == {
        "readable": True,
        "clean": True,
        "blocking": 0,
    }

    body = api_client.get("/api/projects/webapp").json()
    assert body["tree"]["candidate"] == "CBN-002"
    assert body["tree"]["exempt_task"] == "CBN-002"
    assert body["tree"]["blocking_lines"] == []


def test_tree_detail_names_the_blocking_note_and_the_counterfactual(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """A drain drops the override, so the same note becomes foreign dirt (CBN-123 F3)."""
    repo = _git_worker_repo(tmp_path, TREE_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-002.md").write_text(
        "---\nstatus: in-progress\n---\n", encoding="utf-8"
    )
    _register(caobunga_home, "webapp", repo, tier=ExecutionTier.PROCEED_ON_GREEN)

    row = api_client.get("/api/projects").json()["projects"][0]
    assert row["tree"] == {
        "readable": True,
        "clean": False,
        "blocking": 1,
    }
    assert "blocking_lines" not in row["tree"]

    tree = api_client.get("/api/projects/webapp").json()["tree"]
    assert tree["candidate"] == "CBN-001"
    assert tree["exempt_task"] is None
    assert tree["clean_for"] == "CBN-002"
    assert tree["blocking_lines"] == [
        {
            "text": "?? .flaitron/tasknote/CBN-002.md",
            "path": ".flaitron/tasknote/CBN-002.md",
            "task_id": "CBN-002",
        }
    ]


def test_tree_is_null_for_an_unreadable_path(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """Same degradation `current` and `coverage` make, for the same reason."""
    missing = tmp_path / "gone"
    missing.mkdir()
    _register(caobunga_home, "webapp", missing)

    assert api_client.get("/api/projects").json()["projects"][0]["tree"] is None
    assert api_client.get("/api/projects/webapp").json()["tree"] is None


def test_tree_unreadable_git_is_not_reported_as_clean(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """A readable PLAN in a non-git directory: the third state, stated (CBN-EPIC-17.3)."""
    repo = _flaitron_repo(tmp_path, plan=TREE_PLAN)
    _register(caobunga_home, "webapp", repo)

    row = api_client.get("/api/projects").json()["projects"][0]
    assert row["tree"] == {
        "readable": False,
        "clean": False,
        "blocking": 0,
    }
