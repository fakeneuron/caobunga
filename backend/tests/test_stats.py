"""Fleet-wide run-record fold and `GET /api/stats` (CBN-EPIC-12.7).

The seeded fleet below is shaped to exercise every edge the fold has to get
right at once: a UTC close that belongs to the *previous* local day, records
predating `started_at` / `invocation_id` / `escalation_reason`, an unset
routing key, an even-count median, and both routing tiebreaks.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_store import green_suite

from caobunga.models import (
    CLOSURE_OUTCOMES,
    Escalation,
    ExecutionTier,
    Outcome,
    RegistryRecord,
    RunRecord,
)
from caobunga.stats import fleet_stats
from caobunga.store import Store
from main import app

REGISTERED_AT = datetime(2026, 8, 19, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the process timezone; throughput buckets on the operator's local day."""
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def api_client(caobunga_home: Path) -> TestClient:  # noqa: ARG001
    return TestClient(app, base_url="http://127.0.0.1")


def _register(store: Store, project_id: str, path: Path) -> None:
    store.register(
        RegistryRecord(
            id=project_id,
            path=str(path.resolve()),
            tier=ExecutionTier.GATED,
            registered_at=REGISTERED_AT,
            enabled=True,
        )
    )


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def _run(
    project: str,
    task_id: str,
    *,
    closed_at: datetime,
    outcome: Outcome = Outcome.CLOSED,
    started_at: datetime | None = None,
    escalation: Escalation | None = None,
    provider: str | None = None,
    model: str | None = None,
    plan_model: str | None = None,
    invocation_id: str | None = None,
) -> RunRecord:
    return RunRecord(
        project=project,
        task_id=task_id,
        invocation_id=invocation_id,
        outcome=outcome,
        commit_sha="a" * 40 if outcome is Outcome.CLOSED else None,
        started_at=started_at,
        closed_at=closed_at,
        escalation_reason=escalation,
        provider=provider,
        model=model,
        plan_model=plan_model,
        summary=f"{task_id} summary.",
        suite=green_suite() if outcome in CLOSURE_OUTCOMES else None,
    )


def _seed_fleet(home: Path, repo: Path) -> Store:
    """Five records over two projects and two invocations. See the module docstring."""
    store = Store(home)
    _register(store, "app", repo)
    _register(store, "beta", repo)
    records = [
        # Invocation A — one drain of `app`, 10 minutes then 60 minutes.
        _run(
            "app",
            "CBN-001",
            started_at=_at(19, 17, 50),
            closed_at=_at(19, 18),
            provider="grok_cli",
            model="grok-4.6",
            plan_model="medium",
            invocation_id="20260819T175000000Z",
        ),
        _run(
            "app",
            "CBN-002",
            outcome=Outcome.FAILED,
            escalation=Escalation.TIMEOUT,
            started_at=_at(20, 1),
            # 02:00 UTC on the 20th is 22:00 on the 19th in Toronto — this record
            # belongs to the local day *before* its UTC day (CBN-EPIC-95.4).
            closed_at=_at(20, 2),
            provider="grok_cli",
            model="grok-4.6",
            plan_model="heavy",
            invocation_id="20260819T175000000Z",
        ),
        # Invocation B — one gated run of `beta`, 5 minutes.
        _run(
            "beta",
            "CBN-003",
            started_at=_at(21, 11, 55),
            closed_at=_at(21, 12),
            provider="claude_code",
            model="opus",
            plan_model="heavy",
            invocation_id="20260821T115500000Z",
        ),
        # Legacy: never launched, so no provider/model; predates every new field.
        _run(
            "beta",
            "CBN-004",
            outcome=Outcome.BLOCKED,
            escalation=Escalation.NOTE_BLOCKED,
            closed_at=_at(21, 12, 30),
        ),
        # Legacy: stopped before `escalation_reason` existed.
        _run(
            "beta",
            "CBN-005",
            outcome=Outcome.FAILED,
            closed_at=_at(21, 13),
            provider="grok_cli",
            model="grok-4.6",
            plan_model="medium",
        ),
    ]
    for record in records:
        store.write_run(record)
    return store


def test_empty_registry_is_zeroed_not_null(tmp_path: Path) -> None:
    body = fleet_stats(Store(tmp_path / "home"))
    assert body["projects"] == 0
    assert body["runs"] == 0
    assert body["invocations"] == 0
    assert body["success_rate"] is None
    assert body["outcomes"] == dict.fromkeys((o.value for o in Outcome), 0)
    assert body["escalations"] == dict.fromkeys((e.value for e in Escalation), 0)
    assert body["duration"] == {"counted": 0, "median_seconds": None, "max_seconds": None}
    assert body["throughput"] == []
    assert body["routing"] == {"provider": [], "model": [], "plan_model": []}


def test_fleet_rollup_and_success_rate(tmp_path: Path) -> None:
    body = fleet_stats(_seed_fleet(tmp_path / "home", tmp_path))
    assert body["projects"] == 2
    assert body["runs"] == 5
    assert body["outcomes"] == {
        "closed": 2,
        "de-scoped": 0,
        "blocked": 1,
        "failed": 2,
        "no-tasknote": 0,
    }
    assert body["success_rate"] == 0.4


def test_invocations_count_only_keyed_records(tmp_path: Path) -> None:
    body = fleet_stats(_seed_fleet(tmp_path / "home", tmp_path))
    # Two keys across five records; the two legacy records carry none and are
    # not counted rather than being lumped into a phantom third invocation.
    assert body["invocations"] == 2


def test_shared_stamp_across_projects_stays_two(tmp_path: Path) -> None:
    """Parallel legs can mint the same millisecond stamp (CBN-EPIC-313.4)."""
    store = Store(tmp_path / "home")
    _register(store, "app", tmp_path)
    _register(store, "beta", tmp_path)
    stamp = "20260819T175000000Z"
    store.write_run(_run("app", "CBN-001", closed_at=_at(19, 18), invocation_id=stamp))
    store.write_run(_run("beta", "CBN-002", closed_at=_at(19, 18), invocation_id=stamp))
    # A second task of `app` under the same stamp is one drain, not a third invocation.
    store.write_run(_run("app", "CBN-003", closed_at=_at(19, 19), invocation_id=stamp))
    body = fleet_stats(store)
    assert body["runs"] == 3
    assert body["invocations"] == 2


def test_duration_excludes_records_with_no_start_clock(tmp_path: Path) -> None:
    body = fleet_stats(_seed_fleet(tmp_path / "home", tmp_path))
    # Spans are 600s, 3600s, 300s; the two legacy records contribute nothing.
    assert body["duration"] == {"counted": 3, "median_seconds": 600, "max_seconds": 3600}


def test_negative_span_is_dropped_not_clamped(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    _register(store, "app", tmp_path)
    store.write_run(_run("app", "CBN-001", started_at=_at(19, 18), closed_at=_at(19, 17)))
    body = fleet_stats(store)
    assert body["runs"] == 1
    assert body["duration"]["counted"] == 0


def test_escalations_may_undercount_stopped_runs(tmp_path: Path) -> None:
    body = fleet_stats(_seed_fleet(tmp_path / "home", tmp_path))
    assert body["escalations"]["timeout"] == 1
    assert body["escalations"]["note-blocked"] == 1
    assert body["escalations"]["worker-error"] == 0
    stopped = body["outcomes"]["failed"] + body["outcomes"]["blocked"]
    # CBN-005 stopped before the field existed, so the tally is short by one.
    assert sum(body["escalations"].values()) == 2
    assert sum(body["escalations"].values()) < stopped


def test_throughput_buckets_on_the_local_day_newest_first(tmp_path: Path) -> None:
    body = fleet_stats(_seed_fleet(tmp_path / "home", tmp_path))
    assert [day["day"] for day in body["throughput"]] == ["2026-08-21", "2026-08-19"]
    assert [day["runs"] for day in body["throughput"]] == [3, 2]
    # CBN-002 closed at 02:00 UTC on the 20th and still lands on the 19th.
    assert body["throughput"][1]["outcomes"] == {
        "closed": 1,
        "de-scoped": 0,
        "blocked": 0,
        "failed": 1,
        "no-tasknote": 0,
    }


def test_routing_orders_by_volume_then_key_with_null_last(tmp_path: Path) -> None:
    body = fleet_stats(_seed_fleet(tmp_path / "home", tmp_path))
    assert [row["key"] for row in body["routing"]["provider"]] == ["grok_cli", "claude_code", None]
    assert [row["key"] for row in body["routing"]["model"]] == ["grok-4.6", "opus", None]
    # `heavy` and `medium` tie at two runs each; the key-ascending tiebreak
    # decides, and the unkeyed record still sorts last.
    assert [row["key"] for row in body["routing"]["plan_model"]] == ["heavy", "medium", None]


def test_routing_rows_carry_their_own_outcome_and_duration_folds(tmp_path: Path) -> None:
    body = fleet_stats(_seed_fleet(tmp_path / "home", tmp_path))
    by_provider = {row["key"]: row for row in body["routing"]["provider"]}
    # grok_cli ran three times but only two carry a start clock — median is the
    # mean of 600 and 3600, which is where an even-count median has to be right.
    assert by_provider["grok_cli"]["runs"] == 3
    assert by_provider["grok_cli"]["duration"] == {
        "counted": 2,
        "median_seconds": 2100,
        "max_seconds": 3600,
    }
    assert by_provider["grok_cli"]["outcomes"]["failed"] == 2
    assert by_provider[None]["runs"] == 1
    assert by_provider[None]["duration"]["counted"] == 0


@pytest.mark.parametrize("dimension", ["provider", "model", "plan_model"])
def test_every_routing_dimension_accounts_for_every_run(tmp_path: Path, dimension: str) -> None:
    body = fleet_stats(_seed_fleet(tmp_path / "home", tmp_path))
    assert sum(row["runs"] for row in body["routing"][dimension]) == body["runs"]


def test_fold_invariants_hold(tmp_path: Path) -> None:
    body = fleet_stats(_seed_fleet(tmp_path / "home", tmp_path))
    assert sum(body["outcomes"].values()) == body["runs"]
    assert sum(day["runs"] for day in body["throughput"]) == body["runs"]
    for day in body["throughput"]:
        assert sum(day["outcomes"].values()) == day["runs"]
    for rows in body["routing"].values():
        for row in rows:
            assert sum(row["outcomes"].values()) == row["runs"]


def test_stats_endpoint_serves_the_fold(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    _seed_fleet(caobunga_home, tmp_path)
    response = api_client.get("/api/stats")
    assert response.status_code == 200
    assert response.json() == fleet_stats(Store(caobunga_home))


def test_stats_endpoint_on_an_empty_registry(api_client: TestClient) -> None:
    response = api_client.get("/api/stats")
    assert response.status_code == 200
    assert response.json()["runs"] == 0


def test_post_stats_method_not_allowed(api_client: TestClient) -> None:
    assert api_client.post("/api/stats").status_code == 405
