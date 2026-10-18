"""Read-only status API for the v1 dashboard (CBN-040)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from caobunga.digest import format_digest
from caobunga.errors import UnknownProjectError, UserError
from caobunga.flaitron import Coverage, ProjectRead, try_read_project
from caobunga.models import Escalation, Outcome, RegistryRecord, RunRecord, iso_z
from caobunga.presence import DirtLine, read_tree
from caobunga.stats import fleet_stats, outcome_counts
from caobunga.store import Store

API_HOST = "127.0.0.1"
API_PORT = 8013

# Host-trust guard (CBN-248, DNS rebinding). Loopback binding alone doesn't
# stop a browser page from resolving an attacker-controlled name to 127.0.0.1
# and sending Host: <attacker> — TrustedHostMiddleware rejects anything else.
ALLOWED_HOSTS = [API_HOST, "localhost"]

# Status UI origins (CBN-041, Vite `strictPort` 5195). The shipped UI is
# same-origin via the Vite proxy; this allow-list covers a direct browser
# open. Both loopback spellings are distinct origins to a browser.
UI_PORT = 5195
UI_ORIGINS = [f"http://127.0.0.1:{UI_PORT}", f"http://localhost:{UI_PORT}"]

# uvicorn --reload is a dev convenience (file-watcher + re-exec); opt-in only
# so an unattended launch of `main.py` never starts a watcher process.
ENV_API_RELOAD = "CAOBUNGA_API_RELOAD"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def api_reload_enabled() -> bool:
    return os.environ.get(ENV_API_RELOAD, "").strip().lower() in _TRUTHY


# Response models (CBN-263). They declare the shape the handlers' dicts already
# build — no field is added or renamed. Every route serializes with
# `response_model_exclude_unset=True`, so a key a handler omits stays absent
# and an explicit `None` stays `null`: the `exclude_none` run-record objects
# and the list-vs-detail `coverage` / `tree` split survive validation exactly.
# Existing store / reader models (`RegistryRecord`, `RunRecord`, `Coverage`,
# `DirtLine`) are reused rather than redeclared.


class LastRunSummary(BaseModel):
    task_id: str
    outcome: Outcome
    closed_at: str
    summary: str


class CurrentTask(BaseModel):
    task_id: str
    status: str
    reason: str
    plan_model: str | None = None
    shortname: str | None = None


class TreeState(BaseModel):
    """List form carries the first three; the rest are detail-only."""

    readable: bool
    clean: bool
    blocking: int
    candidate: str | None = None
    exempt_task: str | None = None
    clean_for: str | None = None
    blocking_lines: list[DirtLine] | None = None


class ProjectListItem(RegistryRecord):
    last_run: LastRunSummary | None
    current: CurrentTask | None
    coverage: Coverage | None
    tree: TreeState | None


class ProjectsResponse(BaseModel):
    projects: list[ProjectListItem]


class ProjectReports(BaseModel):
    last_rollup: str | None


class ProjectDetail(RegistryRecord):
    current: CurrentTask | None
    coverage: Coverage | None
    tree: TreeState | None
    last_run: RunRecord | None
    runs: list[RunRecord]
    run_counts: dict[Outcome, int]
    reports: ProjectReports


class DigestResponse(BaseModel):
    markdown: str


class DurationStats(BaseModel):
    counted: int
    median_seconds: int | None
    max_seconds: int | None


class ThroughputDay(BaseModel):
    day: str
    runs: int
    outcomes: dict[Outcome, int]


class RoutingRow(BaseModel):
    key: str | None
    runs: int
    outcomes: dict[Outcome, int]
    duration: DurationStats


class Routing(BaseModel):
    provider: list[RoutingRow]
    model: list[RoutingRow]
    plan_model: list[RoutingRow]


class FleetStats(BaseModel):
    projects: int
    runs: int
    invocations: int
    outcomes: dict[Outcome, int]
    success_rate: float | None
    duration: DurationStats
    escalations: dict[Escalation, int]
    throughput: list[ThroughputDay]
    routing: Routing


router = APIRouter(prefix="/api")


def get_store() -> Store:
    return Store()


def current_task(read: ProjectRead | None) -> dict[str, Any] | None:
    """In-progress Flaitron task, if the worker repo is readable.

    Reads `in_flight` rather than `candidate` (CBN-EPIC-17.3). The two were the
    same field's two jobs until the picker stopped preferring in-progress notes
    under an unattended posture; reading the chosen candidate would then have
    reported no current task while the operator was working on one. The payload
    is unchanged — this is the same override, on the field that owns its
    display half (CBN-040 / CBN-011).

    Takes the read rather than the record (CBN-EPIC-18.3) so this and
    `coverage_payload` share one parse of the project per request instead of
    two; `try_read_project` owns the skip-not-fail wrapper both need.
    """
    if read is None:
        return None
    flight = read.in_flight
    if flight is None or flight.tasknote is None:
        return None
    payload: dict[str, Any] = {
        "task_id": flight.task_id,
        "status": flight.tasknote.status,
        "reason": flight.reason,
    }
    if flight.plan_model is not None:
        payload["plan_model"] = flight.plan_model
    if flight.shortname is not None:
        payload["shortname"] = flight.shortname
    return payload


def coverage_payload(read: ProjectRead | None, *, detailed: bool) -> dict[str, Any] | None:
    """`[unattended]` marker coverage, or `null` when the repo is unreadable.

    `detailed=False` (the list endpoint) carries the counts alone —
    `open` / `marked` / `denied` plus the `unparsed` tally (CBN-115);
    `detailed=True` adds `denials`, the ids an operator would seed,
    `unparsed_rows`, the dropped lines an operator would fix, and
    `marked_tasks`, the ids behind the `marked` count (CBN-125). Same
    aggregate-vs-detail split `runs` / `run_counts` already use (CBN-EPIC-12.3):
    the fleet view answers "which repos need work", the project view answers
    "which lines".
    """
    if read is None:
        return None
    exclude = set() if detailed else {"denials", "unparsed_rows", "marked_tasks"}
    return read.coverage.model_dump(mode="json", exclude=exclude)


def tree_payload(
    read: ProjectRead | None,
    record: RegistryRecord,
    *,
    detailed: bool,
) -> dict[str, Any] | None:
    """Target-tree state, or `null` when the repo is unreadable.

    The operator-presence gate's own verdict, served before it has to be
    discovered by a refusal (CBN-130). It is computed at the project's **own
    tier** posture — `drain` moves the candidate, and the candidate is what
    decides the exemption — so this answers "would a launch here refuse", not a
    generic "is this tree clean".

    `detailed=False` (the list endpoint) carries the verdict alone: `readable`,
    `clean`, and the blocking count. `detailed=True`
    adds the candidate, the exempted tasknote, the classified blocking lines, and
    `clean_for` — the same aggregate-vs-detail split `coverage` uses, and for the
    same reason: the fleet view answers "which repos are ready", the project view
    answers "why not".

    Unlike marker drift (§"Marker coverage"), this joins no run record — it reads
    the target repo and nothing of Caobunga's own history — so serving it here
    changes nothing about what the payload means.
    """
    if read is None:
        return None
    state = read_tree(read, record.path, record.tier)
    payload: dict[str, Any] = {
        "readable": state.readable,
        "clean": state.clean,
        "blocking": len(state.blocking),
    }
    if detailed:
        payload["candidate"] = state.candidate
        payload["exempt_task"] = state.exempt_task
        payload["clean_for"] = state.clean_for
        payload["blocking_lines"] = [entry.model_dump(mode="json") for entry in state.blocking]
    return payload


def last_run_summary(run: RunRecord) -> dict[str, str]:
    return {
        "task_id": run.task_id,
        "outcome": run.outcome.value,
        "closed_at": iso_z(run.closed_at),
        "summary": run.summary,
    }


def run_history_payload(runs: list[RunRecord]) -> list[dict[str, Any]]:
    """Closed runs newest-first, each the full record like detail `last_run`.

    Sorted here rather than leaning on `Store.list_runs`' filename order:
    newest-first is a stated property of this endpoint, not an emergent
    property of the runs directory.
    """
    ordered = sorted(runs, key=lambda run: run.closed_at, reverse=True)
    return [run.model_dump(mode="json", exclude_none=True) for run in ordered]


def project_payload(record: RegistryRecord) -> dict[str, Any]:
    return record.model_dump(mode="json", exclude_none=True)


@router.get("/projects", response_model=ProjectsResponse, response_model_exclude_unset=True)
def list_projects() -> dict[str, Any]:
    store = get_store()
    rows: list[dict[str, Any]] = []
    for record in store.list_projects():
        last = store.last_run(record.id)
        read = try_read_project(Path(record.path))
        row = project_payload(record)
        row["last_run"] = last_run_summary(last) if last else None
        row["current"] = current_task(read)
        row["coverage"] = coverage_payload(read, detailed=False)
        row["tree"] = tree_payload(read, record, detailed=False)
        rows.append(row)
    return {"projects": rows}


@router.get(
    "/projects/{project_id}", response_model=ProjectDetail, response_model_exclude_unset=True
)
def get_project(project_id: str) -> dict[str, Any]:
    store = get_store()
    record = store.get(project_id)
    last = store.last_run(project_id)
    runs = store.list_runs(project_id)
    read = try_read_project(Path(record.path))
    body = project_payload(record)
    body["current"] = current_task(read)
    body["coverage"] = coverage_payload(read, detailed=True)
    body["tree"] = tree_payload(read, record, detailed=True)
    body["last_run"] = last.model_dump(mode="json", exclude_none=True) if last else None
    body["runs"] = run_history_payload(runs)
    body["run_counts"] = outcome_counts(runs)
    body["reports"] = {
        "last_rollup": store.last_rollup(project_id),
    }
    return body


@router.get("/digest", response_model=DigestResponse, response_model_exclude_unset=True)
def digest() -> dict[str, str]:
    return {"markdown": format_digest(get_store())}


@router.get("/stats", response_model=FleetStats, response_model_exclude_unset=True)
def stats() -> dict[str, Any]:
    return fleet_stats(get_store())


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(UserError)
    async def _user_error(_request: Request, exc: UserError) -> JSONResponse:
        status = 404 if isinstance(exc, UnknownProjectError) else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)
