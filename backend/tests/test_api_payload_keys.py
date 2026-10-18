"""fixtures/api-payload-keys.json vs. the live `/api/projects/{id}` payload.

CBN-EPIC-231.3. `frontend/src/api/types.ts` is hand-maintained against this
endpoint with no shared schema, and Discovery found it had already drifted in
three places (`RegistryRecord.providers`, `Coverage.marked_tasks`, and four
`RunRecord` fields) with nothing to catch the next one — and, one level
deeper, `Denial.triage` / `triage_reason` (CBN-234, which extended the guard
to the nested element shapes). This file derives each
payload shape's key set from the live endpoint / the pydantic models that
build it and diffs it against the checked-in fixture; the sibling
`frontend/tests/unit/api-payload-keys.test.ts` derives the same key sets from
`types.ts` and diffs against the same file. A key added on one side and not
the other fails whichever suite reads the fixture next — same pattern as
`test_contract.py`'s `model_fields`-vs-CONTRACT.md guards, aimed at a
TypeScript file instead of a markdown doc.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from test_api import _flaitron_repo
from test_store import green_suite

from caobunga import api
from caobunga.flaitron import Coverage, Denial, TriageClass, UnparsedRow
from caobunga.models import (
    Escalation,
    ExecutionTier,
    ExternalReview,
    ForeignCommit,
    HermesReview,
    HermesVerdictCode,
    NewRow,
    Outcome,
    RegistryRecord,
    RunRecord,
    Suite,
    SuiteVerb,
    SuiteVerdict,
)
from caobunga.presence import DirtLine
from caobunga.store import Store
from main import app

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "api-payload-keys.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

# Plan carrying one marked row, one unmarked (denied) row, and one dropped
# (unparsed) row — the three `Coverage.denials` / `unparsed_rows` producers at
# once, so a single project's detail read exercises every optional key.
# CBN-011 is the row `_flaitron_repo(..., in_progress=True)` opens a tasknote
# for (hardcoded filename); it needs its own PLAN row so `current_task()` can
# resolve `plan_model` / `shortname` and populate those two optional keys too.
PLAN = """# sample

## High

- [ ] **CBN-001** [medium] [unattended] | marked — carries the marker.
- [ ] **CBN-002** [medium] | unmarked — no marker.
- [ ] **CBN-005** [xheavy] [!unattended] | dropped — bang-shape.
- [ ] **CBN-011** [medium] | open — the in-progress tasknote.
"""


def _closed_run(project: str) -> RunRecord:
    """A realistic closed run — not every `RunRecord` field, since several are
    mutually exclusive by validation (e.g. `escalation_reason` only applies to
    `blocked` / `failed` outcomes). `RunRecord.model_fields` — asserted against
    the fixture directly below — is the ground truth for the field *set*; a
    single record can only ever carry a *consistent* subset of it live."""
    return RunRecord(
        project=project,
        task_id="CBN-001",
        invocation_id="20260921T000000000Z",
        outcome=Outcome.CLOSED,
        commit_sha="a" * 40,
        started_at=datetime(2026, 9, 21, tzinfo=UTC),
        closed_at=datetime(2026, 9, 21, 1, tzinfo=UTC),
        eligibility_reason="obvious — matched dispatch order",
        hermes_review=HermesReview.REVIEWED,
        hermes_verdict="VERDICT: accept — sound close.",
        hermes_verdict_code=HermesVerdictCode.ACCEPT,
        provider="grok_cli",
        model="grok-4.6",
        plan_model="medium",
        next_task="CBN-002",
        tasknote_path=".flaitron/tasknote/archive/cbn/CBN-001.md",
        summary="Guard fixture landed.",
        suite=green_suite(),
    )


@pytest.fixture
def api_client(caobunga_home: Path) -> TestClient:  # noqa: ARG001
    return TestClient(app, base_url="http://127.0.0.1")


def _register_maximal(home: Path, project_id: str, path: Path) -> RegistryRecord:
    """A registration with every optional `RegistryRecord` field set, so the
    endpoint's top-level dict carries `provider` / `model` / `providers` too."""
    store = Store(home)
    return store.register(
        RegistryRecord(
            id=project_id,
            path=str(path.resolve()),
            provider="grok_cli",
            model="grok-4.6",
            providers=["grok_cli", "claude_code"],
            registered_at=datetime(2026, 9, 21, tzinfo=UTC),
        )
    )


def test_project_detail_payload_keys_match_fixture(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    repo = _flaitron_repo(tmp_path, in_progress=True, plan=PLAN)
    _register_maximal(caobunga_home, "webapp", repo)
    store = Store(caobunga_home)
    store.write_run(_closed_run("webapp"))

    body = api_client.get("/api/projects/webapp").json()

    assert set(body) == set(FIXTURE["project_detail"]), (
        f"live /api/projects/{{id}} top-level keys {sorted(body)} != "
        f"fixture project_detail {sorted(FIXTURE['project_detail'])}"
    )
    assert set(body["coverage"]) == set(FIXTURE["coverage_detail"])
    assert set(body["tree"]) == set(FIXTURE["tree_detail"])
    assert set(body["reports"]) == set(FIXTURE["reports"])


def test_current_task_payload_keys_match_fixture(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    repo = _flaitron_repo(tmp_path, in_progress=True, plan=PLAN)
    _register_maximal(caobunga_home, "webapp", repo)
    body = api_client.get("/api/projects/webapp").json()
    assert set(body["current"]) == set(FIXTURE["current_task"])


def test_run_record_model_fields_match_fixture() -> None:
    """Ground truth for the `run_record` shape: every field the model declares,
    independent of which subset any one record dumps live (see `_closed_run`)."""
    assert set(RunRecord.model_fields) == set(FIXTURE["run_record"])


def test_coverage_model_fields_match_fixture() -> None:
    assert set(Coverage.model_fields) == set(FIXTURE["coverage_detail"])


# Element shapes of the arrays / objects nested inside the shapes above
# (CBN-234). Checked by `model_fields` rather than the live endpoint for the
# reason `_closed_run` gives: `new_rows` / `foreign_commits` are absent on a
# clean run and `suite` on a `blocked` one, so no single payload carries every
# element, but the model declares every field regardless.
@pytest.mark.parametrize(
    ("fixture_key", "model"),
    [
        ("denial", Denial),
        ("unparsed_row", UnparsedRow),
        ("dirt_line", DirtLine),
        ("new_row", NewRow),
        ("foreign_commit", ForeignCommit),
        ("suite", Suite),
        ("suite_verb", SuiteVerb),
    ],
)
def test_nested_model_fields_match_fixture(fixture_key: str, model: type[BaseModel]) -> None:
    assert set(model.model_fields) == set(FIXTURE[fixture_key]), (
        f"{model.__name__}.model_fields {sorted(model.model_fields)} != "
        f"fixture {fixture_key} {sorted(FIXTURE[fixture_key])}"
    )


# StrEnum values the payload serializes (CBN-263). The key guards above catch a
# renamed *field*; this catches a renamed or added *value* — a new `Outcome` the
# UI's fixed chip set, or a union narrowed in `types.ts`, would otherwise miss.
@pytest.mark.parametrize(
    ("fixture_key", "enum"),
    [
        ("execution_tier", ExecutionTier),
        ("outcome", Outcome),
        ("escalation_reason", Escalation),
        ("hermes_review", HermesReview),
        ("hermes_verdict_code", HermesVerdictCode),
        ("suite_verdict", SuiteVerdict),
        ("external_review", ExternalReview),
        ("triage_class", TriageClass),
    ],
)
def test_enum_values_match_fixture(fixture_key: str, enum: type[StrEnum]) -> None:
    assert {member.value for member in enum} == set(FIXTURE["enum_values"][fixture_key])


def test_enum_values_block_covers_every_guarded_enum() -> None:
    """A block entry with no backend enum behind it is a stale fixture row."""
    assert set(FIXTURE["enum_values"]) == {
        "execution_tier",
        "outcome",
        "escalation_reason",
        "hermes_review",
        "hermes_verdict_code",
        "suite_verdict",
        "external_review",
        "triage_class",
    }


def test_response_models_preserve_the_handler_payloads(
    api_client: TestClient, caobunga_home: Path, tmp_path: Path
) -> None:
    """`response_model=` declares the shape; it must not change it (CBN-263).

    Each route's live body equals the dict its handler builds, so a key the
    handler omits (`exclude_none` run records, list-form `coverage` / `tree`)
    stays absent and an explicit `null` (`current`, `coverage` on an unreadable
    path) stays `null` — and a key a response model forgot to declare would be
    dropped here rather than shipped silently.
    """
    repo = _flaitron_repo(tmp_path, in_progress=True, plan=PLAN)
    _register_maximal(caobunga_home, "webapp", repo)
    gone = tmp_path / "gone"
    Store(caobunga_home).register(RegistryRecord(id="gone", path=str(gone.resolve())))
    store = Store(caobunga_home)
    store.write_run(_closed_run("webapp"))
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-002",
            outcome=Outcome.BLOCKED,
            started_at=datetime(2026, 9, 22, tzinfo=UTC),
            closed_at=datetime(2026, 9, 22, 1, tzinfo=UTC),
            escalation_reason=Escalation.WORKER_PARKED,
            park_reason="dependency",
            summary="Parked.",
        )
    )

    assert api_client.get("/api/projects").json() == api.list_projects()
    for project_id in ("webapp", "gone"):
        live = api_client.get(f"/api/projects/{project_id}").json()
        assert live == api.get_project(project_id)
    assert api_client.get("/api/stats").json() == api.stats()
    assert api_client.get("/api/digest").json() == api.digest()
