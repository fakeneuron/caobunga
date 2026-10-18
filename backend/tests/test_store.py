"""Store and model validation without going through click."""

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from caobunga.errors import UnknownProjectError, UserError
from caobunga.models import (
    CLOSURE_OUTCOMES,
    Escalation,
    ExecutionTier,
    HermesReview,
    NewRow,
    Outcome,
    RegistryRecord,
    RunRecord,
    Suite,
    SuiteVerb,
    SuiteVerdict,
)
from caobunga.store import Store

CLOSED_AT = datetime(2026, 8, 19, tzinfo=UTC)


def green_suite() -> Suite:
    """Every fleet verb green — what a v2 closure-outcome fixture must carry.

    Shared across the test files that build closed records (`test_api`,
    `test_cli`, …) since CBN-EPIC-202.2 made `suite` required
    on every closure outcome.
    """
    return Suite.from_verbs(
        [
            SuiteVerb(verb="test", exit_code=0, duration=1.0),
            SuiteVerb(verb="lint", exit_code=0, duration=0.1),
            SuiteVerb(verb="typecheck", exit_code=0, duration=0.2),
        ]
    )


def _closure(outcome: Outcome, **kwargs: object) -> RunRecord:
    payload: dict[str, object] = {
        "project": "webapp",
        "task_id": "CBN-001",
        "outcome": outcome,
        "commit_sha": "a" * 40,
        "closed_at": CLOSED_AT,
        "summary": "closed",
    }
    payload.update(kwargs)
    return RunRecord.model_validate(payload)


def test_closed_run_requires_commit_sha() -> None:
    with pytest.raises(ValidationError, match="commit_sha"):
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.CLOSED,
            closed_at=datetime(2026, 8, 19, tzinfo=UTC),
            summary="no sha",
            suite=green_suite(),
        )


def test_failed_run_omits_commit_sha(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    record = RunRecord(
        project="webapp",
        task_id="CBN-001",
        outcome=Outcome.FAILED,
        closed_at=datetime(2026, 8, 19, tzinfo=UTC),
        summary="worker died",
    )
    path = store.write_run(record)
    assert "commit_sha" not in path.read_text(encoding="utf-8")
    loaded = store.last_run("webapp")
    assert loaded is not None
    assert loaded.outcome is Outcome.FAILED
    assert loaded.commit_sha is None


def test_unknown_keys_ignored() -> None:
    record = RegistryRecord.model_validate(
        {
            "schema_version": 1,
            "id": "webapp",
            "path": "/tmp/webapp",  # noqa: S108
            "tier": "gated",
            "registered_at": "2026-08-19T00:00:00Z",
            "enabled": True,
            "future_field": "ok",
        }
    )
    assert record.id == "webapp"


def test_last_run_orders_same_second_by_millis(tmp_path: Path) -> None:
    """CBN-098: same UTC second must not fall back to task-id lexicographic order."""
    store = Store(tmp_path / "home")
    base = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    earlier = base.replace(microsecond=100_000)  # +100ms
    later = base.replace(microsecond=900_000)  # +900ms
    path_early = store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-099",
            outcome=Outcome.FAILED,
            closed_at=earlier,
            summary="first in the second",
        )
    )
    path_late = store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-010",
            outcome=Outcome.FAILED,
            closed_at=later,
            summary="second in the second",
        )
    )
    assert path_early.name == "20260819T120000100Z-CBN-099.yaml"
    assert path_late.name == "20260819T120000900Z-CBN-010.yaml"
    last = store.last_run("webapp")
    assert last is not None
    assert last.task_id == "CBN-010"
    assert [r.task_id for r in store.list_runs("webapp")] == ["CBN-099", "CBN-010"]


def test_get_rejects_invalid_slug(tmp_path: Path) -> None:
    """A `../`-shaped slug must not escape `projects_dir()` (CBN-264)."""
    store = Store(tmp_path / "home")
    with pytest.raises(UnknownProjectError):
        store.get("../../etc/passwd")
    assert not (tmp_path / "etc" / "passwd.yaml").exists()


def test_project_file_rejects_invalid_slug(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    with pytest.raises(UnknownProjectError):
        store.project_file("../escape")


def test_corrupt_registry_file(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    target = store.project_file("webapp")
    target.parent.mkdir(parents=True)
    target.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(UserError, match="corrupt registry"):
        store.get("webapp")


# --- Provider allow-list persistence (CBN-091) ---


def test_registry_providers_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.register(
        RegistryRecord(
            id="app",
            path="/tmp/app",  # noqa: S108
            providers=["claude_code", "codex"],
        )
    )
    assert store.get("app").providers == ["claude_code", "codex"]


def test_registry_providers_normalized_on_validate() -> None:
    record = RegistryRecord(
        id="app",
        path="/tmp/app",  # noqa: S108
        providers=[" codex ", "claude_code", "codex", "  "],
    )
    assert record.providers == ["codex", "claude_code"]


def test_registry_empty_providers_is_none() -> None:
    assert RegistryRecord(id="app", path="/tmp/app", providers=[]).providers is None  # noqa: S108
    assert RegistryRecord(id="app", path="/tmp/app", providers=["  "]).providers is None  # noqa: S108


def test_registry_omitted_providers_stays_none() -> None:
    assert RegistryRecord(id="app", path="/tmp/app").providers is None  # noqa: S108


def test_re_register_same_path_keeps_providers(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.register(RegistryRecord(id="app", path="/tmp/app", providers=["claude_code", "codex"]))  # noqa: S108
    updated = store.register(
        RegistryRecord(id="app", path="/tmp/app", tier=ExecutionTier.PROCEED_ON_GREEN)  # noqa: S108
    )
    assert updated.tier is ExecutionTier.PROCEED_ON_GREEN
    assert updated.providers == ["claude_code", "codex"]
    assert store.get("app").providers == ["claude_code", "codex"]


# --- Case-insensitive repo identity (CBN-131) ---
#
# Tested at the `Store.register` level with literal path strings, not through
# the CLI with real directories: CI runs on `ubuntu-latest`, where a
# case-variant path does not exist and `resolve(strict=True)` would raise
# before the comparison under test is ever reached.


def test_re_register_case_variant_path_merges(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.register(
        RegistryRecord(id="toolkit", path="/Users/dev/code/toolkit", providers=["codex"])
    )
    # Read back rather than using the register() return: the YAML round-trip
    # drops sub-second precision, so only a reloaded record is comparable.
    first = store.get("toolkit")
    updated = store.register(
        RegistryRecord(
            id="toolkit",
            path="/Users/dev/Code/toolkit",
            tier=ExecutionTier.PROCEED_ON_GREEN,
        )
    )
    assert updated.tier is ExecutionTier.PROCEED_ON_GREEN
    assert updated.registered_at == first.registered_at
    assert updated.providers == ["codex"]
    # The stored spelling is the one originally registered — a case-only
    # re-register is an update, not a path rewrite.
    assert store.get("toolkit").path == "/Users/dev/code/toolkit"


def test_find_by_path_matches_case_variant_and_misses_strangers(tmp_path: Path) -> None:
    """CBN-174: the suite runner's bound is "registered", decided by CBN-131's rule."""
    store = Store(tmp_path)
    repo = tmp_path / "Code" / "toolkit"
    repo.mkdir(parents=True)
    store.register(RegistryRecord(id="toolkit", path=str(repo)))
    variant = tmp_path / "code" / "TOOLKIT"
    found = store.find_by_path(variant)
    assert found is not None and found.id == "toolkit"
    assert store.find_by_path(tmp_path / "Code" / "stranger") is None


def test_re_register_genuinely_different_path_still_refuses(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.register(RegistryRecord(id="toolkit", path="/Users/dev/code/toolkit"))
    with pytest.raises(UserError, match="--replace"):
        store.register(RegistryRecord(id="toolkit", path="/Users/dev/code/toolkit-v2"))
    replaced = store.register(
        RegistryRecord(id="toolkit", path="/Users/dev/code/toolkit-v2"), replace=True
    )
    assert replaced.path == "/Users/dev/code/toolkit-v2"
    assert store.get("toolkit").path == "/Users/dev/code/toolkit-v2"


def _registered(store: Store, path: Path, **pins: object) -> RegistryRecord:
    payload: dict[str, object] = {
        "id": "toolkit",
        "path": str(path.resolve()),
        "tier": ExecutionTier.PROCEED_ON_GREEN,
    }
    payload.update(pins)
    return store.register(RegistryRecord.model_validate(payload))


def test_allow_list_clears_a_stored_provider_pin(tmp_path: Path) -> None:
    """CBN-EPIC-95.3 F0: --providers over a pin used to leave both set.

    A record carrying both makes picker.pick short-circuit into _override before
    the allow-list is read, so the allow-list is inert while looking healthy.
    """
    store = Store(tmp_path / "home")
    repo = tmp_path / "toolkit"
    repo.mkdir()
    _registered(store, repo, provider="claude_code", model="sonnet")
    merged = _registered(store, repo, providers=["claude_code", "codex"])
    assert merged.providers == ["claude_code", "codex"]
    assert merged.provider is None
    assert merged.model is None
    assert store.get("toolkit").provider is None


def test_provider_pin_clears_a_stored_allow_list(tmp_path: Path) -> None:
    """The exclusion holds in both directions, not just the reported one."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "toolkit"
    repo.mkdir()
    _registered(store, repo, providers=["claude_code", "codex"])
    merged = _registered(store, repo, provider="codex", model="gpt-5")
    assert merged.provider == "codex"
    assert merged.model == "gpt-5"
    assert merged.providers is None


def test_tier_only_reregister_preserves_pins(tmp_path: Path) -> None:
    """Clearing is caused by the opposite flag, never by an unrelated update."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "toolkit"
    repo.mkdir()
    _registered(store, repo, providers=["claude_code", "codex"])
    merged = store.register(
        RegistryRecord(id="toolkit", path=str(repo.resolve()), tier=ExecutionTier.GATED)
    )
    assert merged.tier is ExecutionTier.GATED
    assert merged.providers == ["claude_code", "codex"]


# --- run signal fields (CBN-EPIC-12.2) -----------------------------------


def test_signal_fields_omitted_by_default(tmp_path: Path) -> None:
    """Every new field is optional, so an unannotated record serializes as before."""
    store = Store(tmp_path / "home")
    path = store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.CLOSED,
            commit_sha="a" * 40,
            closed_at=datetime(2026, 8, 19, tzinfo=UTC),
            summary="plain",
            suite=green_suite(),
        )
    )
    text = path.read_text(encoding="utf-8")
    for field in ("started_at", "escalation_reason", "hermes_review"):
        assert field not in text


def test_legacy_stopped_record_without_escalation_still_loads(tmp_path: Path) -> None:
    """The regression this field's validator is deliberately one-directional to avoid.

    Run records written before CBN-EPIC-12.2 carry `failed` / `blocked` with no
    `escalation_reason`. A required-when-stopped rule would raise inside
    `Store._load_run` and take `list_runs` — and CBN-EPIC-12.3's whole surface —
    down with it.
    """
    store = Store(tmp_path / "home")
    runs = store.runs_dir("webapp")
    runs.mkdir(parents=True)
    (runs / "20260819T000000000Z-CBN-001.yaml").write_text(
        "schema_version: 1\n"
        "project: webapp\n"
        "task_id: CBN-001\n"
        "outcome: failed\n"
        "closed_at: 2026-08-19T00:00:00Z\n"
        "summary: worker died\n",
        encoding="utf-8",
    )
    loaded = store.list_runs("webapp")
    assert len(loaded) == 1
    assert loaded[0].outcome is Outcome.FAILED
    assert loaded[0].escalation_reason is None
    assert loaded[0].started_at is None
    assert loaded[0].hermes_review is None


# The three ways a run YAML can fail to load. Before CBN-106 the middle one
# raised `UserError` (→ 400) and the other two escaped the API's exception
# handlers as unhandled 500s; all three now read as "record absent".
UNREADABLE_RUNS = {
    "yaml-syntax": "outcome: [unbalanced\n",
    "not-a-mapping": "just a string\n",
    "schema-invalid": (
        "schema_version: 1\n"
        "project: webapp\n"
        "task_id: CBN-002\n"
        "outcome: exploded\n"
        "closed_at: 2026-08-21T00:00:00Z\n"
        "summary: bad\n"
    ),
}


def _home_with_one_run(tmp_path: Path) -> Store:
    store = Store(tmp_path / "home")
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.CLOSED,
            commit_sha="a" * 40,
            closed_at=datetime(2026, 8, 20, tzinfo=UTC),
            summary="healthy",
            suite=green_suite(),
        )
    )
    return store


def _write_unreadable(store: Store, body: str) -> Path:
    """A bad file that sorts *after* the healthy one, so `last_run` hits it first."""
    path = store.runs_dir("webapp") / "20260821T000000000Z-CBN-002.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize("shape", sorted(UNREADABLE_RUNS))
def test_unreadable_run_is_skipped_not_raised(tmp_path: Path, shape: str) -> None:
    store = _home_with_one_run(tmp_path)
    _write_unreadable(store, UNREADABLE_RUNS[shape])

    loaded = store.list_runs("webapp")
    assert [run.task_id for run in loaded] == ["CBN-001"]


@pytest.mark.parametrize("shape", sorted(UNREADABLE_RUNS))
def test_last_run_falls_back_past_an_unreadable_newest(tmp_path: Path, shape: str) -> None:
    """The quiet-lie guard: skipping must not turn a full history into "no runs"."""
    store = _home_with_one_run(tmp_path)
    _write_unreadable(store, UNREADABLE_RUNS[shape])

    last = store.last_run("webapp")
    assert last is not None
    assert last.task_id == "CBN-001"


def test_non_utf8_run_file_is_skipped_not_raised(tmp_path: Path) -> None:
    """Undecodable bytes are a `ValueError`, not a `YAMLError` — CBN-EPIC-314.3."""
    store = _home_with_one_run(tmp_path)
    path = _write_unreadable(store, "")
    path.write_bytes(b"task_id: \xff\xfe\x00 not utf-8\n")

    assert [run.task_id for run in store.list_runs("webapp")] == ["CBN-001"]
    last = store.last_run("webapp")
    assert last is not None
    assert last.task_id == "CBN-001"


def test_non_utf8_latest_rollup_reads_as_none(tmp_path: Path) -> None:
    """The project page's rollup read degrades instead of raising (CBN-318)."""
    store = Store(tmp_path / "home")
    directory = store.rollups_dir("webapp")
    directory.mkdir(parents=True)
    (directory / "20260821T000000000Z-CBN-001-002.md").write_bytes(b"# rollup \xff\xfe\n")

    assert store.last_rollup("webapp") is None

    (directory / "20260820T000000000Z-CBN-003-004.md").write_text("# older\n", encoding="utf-8")
    assert store.last_rollup("webapp") == "# older\n"


def test_run_file_that_cannot_be_opened_is_skipped(tmp_path: Path) -> None:
    """A directory named like a record: `read_text` raises `OSError` — CBN-EPIC-314.3."""
    store = _home_with_one_run(tmp_path)
    (store.runs_dir("webapp") / "20260821T000000000Z-CBN-002.yaml").mkdir()

    assert [run.task_id for run in store.list_runs("webapp")] == ["CBN-001"]


def test_run_file_that_vanished_after_listing_is_skipped(tmp_path: Path) -> None:
    """A dangling symlink is globbed, then `read_text` raises `FileNotFoundError` (314.3)."""
    store = _home_with_one_run(tmp_path)
    (store.runs_dir("webapp") / "20260821T000000000Z-CBN-002.yaml").symlink_to(tmp_path / "gone")

    assert [run.task_id for run in store.list_runs("webapp")] == ["CBN-001"]


def test_every_run_unreadable_reads_as_no_runs(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    store.runs_dir("webapp").mkdir(parents=True)
    _write_unreadable(store, UNREADABLE_RUNS["not-a-mapping"])

    assert store.list_runs("webapp") == []
    assert store.last_run("webapp") is None


def test_corrupt_registry_still_raises_on_the_fleet_path(tmp_path: Path) -> None:
    """CBN-106's asymmetry, pinned on the path the fleet reads take.

    `test_corrupt_registry_file` covers `Store.get`; this covers `list_projects`,
    which is what `/api/projects` and `stats.fleet_stats` call. Runs went lenient
    there and the registry deliberately did not — skipping a corrupt registry file
    would silently unregister a project rather than degrade one row.
    """
    store = Store(tmp_path / "home")
    store.projects_dir().mkdir(parents=True)
    store.project_file("webapp").write_text("just a string\n", encoding="utf-8")
    with pytest.raises(UserError, match="corrupt registry"):
        store.list_projects()


@pytest.mark.parametrize("outcome", [Outcome.CLOSED, Outcome.DE_SCOPED, Outcome.NO_TASKNOTE])
def test_escalation_rejected_when_run_did_not_stop(outcome: Outcome) -> None:
    with pytest.raises(ValidationError, match="escalation_reason"):
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=outcome,
            commit_sha="a" * 40,
            closed_at=datetime(2026, 8, 19, tzinfo=UTC),
            summary="did not stop",
            escalation_reason=Escalation.TIMEOUT,
            suite=green_suite(),
        )


@pytest.mark.parametrize("outcome", [Outcome.FAILED, Outcome.BLOCKED])
def test_escalation_accepted_when_run_stopped(outcome: Outcome) -> None:
    record = RunRecord(
        project="webapp",
        task_id="CBN-001",
        outcome=outcome,
        closed_at=datetime(2026, 8, 19, tzinfo=UTC),
        summary="stopped",
        escalation_reason=Escalation.TIMEOUT,
    )
    assert record.escalation_reason is Escalation.TIMEOUT


# --- `suite` (CBN-EPIC-202.2): required exactly on a closure outcome at v2 ---


def test_closure_outcomes_are_the_three_that_carry_a_commit() -> None:
    assert CLOSURE_OUTCOMES == {Outcome.CLOSED, Outcome.DE_SCOPED, Outcome.NO_TASKNOTE}


@pytest.mark.parametrize("outcome", sorted(CLOSURE_OUTCOMES))
def test_suite_required_on_closure_outcomes_at_v2(outcome: Outcome) -> None:
    with pytest.raises(ValidationError, match="suite is required at schema_version 2"):
        _closure(outcome)
    record = _closure(outcome, suite=green_suite())
    assert record.schema_version == 2
    assert record.suite is not None
    assert record.suite.verdict is SuiteVerdict.GREEN


@pytest.mark.parametrize("outcome", [Outcome.FAILED, Outcome.BLOCKED])
def test_suite_rejected_when_run_did_not_close(outcome: Outcome) -> None:
    with pytest.raises(ValidationError, match="suite is only valid"):
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=outcome,
            closed_at=CLOSED_AT,
            summary="stopped",
            suite=green_suite(),
        )


@pytest.mark.parametrize("outcome", sorted(CLOSURE_OUTCOMES))
def test_legacy_v1_closure_record_without_suite_still_loads(
    tmp_path: Path, outcome: Outcome
) -> None:
    """Every run YAML written before CBN-EPIC-202.2 says `schema_version: 1` and has no suite.

    The required direction keys on the version so those keep loading through
    `Store.list_runs` — the same regression shape
    `test_legacy_stopped_record_without_escalation_still_loads` guards.
    """
    store = Store(tmp_path / "home")
    runs = store.runs_dir("webapp")
    runs.mkdir(parents=True)
    (runs / "20260819T000000000Z-CBN-001.yaml").write_text(
        "schema_version: 1\n"
        "project: webapp\n"
        "task_id: CBN-001\n"
        f"outcome: {outcome.value}\n"
        f"commit_sha: {'a' * 40}\n"
        "closed_at: 2026-08-19T00:00:00Z\n"
        "summary: before the field\n",
        encoding="utf-8",
    )
    loaded = store.list_runs("webapp")
    assert len(loaded) == 1
    assert loaded[0].schema_version == 1
    assert loaded[0].outcome is outcome
    assert loaded[0].suite is None


def test_v2_closure_record_without_suite_is_skipped_not_raised(tmp_path: Path) -> None:
    """A v2 close with no `suite` is a record that was never written — skip-not-fail drops it."""
    store = Store(tmp_path / "home")
    runs = store.runs_dir("webapp")
    runs.mkdir(parents=True)
    (runs / "20260819T000000000Z-CBN-001.yaml").write_text(
        "schema_version: 2\n"
        "project: webapp\n"
        "task_id: CBN-001\n"
        "outcome: closed\n"
        f"commit_sha: {'a' * 40}\n"
        "closed_at: 2026-08-19T00:00:00Z\n"
        "summary: half-written\n",
        encoding="utf-8",
    )
    assert store.list_runs("webapp") == []


def test_suite_round_trips_through_the_store(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    suite = Suite.from_verbs(
        [
            SuiteVerb(verb="test", exit_code=1, duration=2.5, detail="1 failed, 3 passed"),
            SuiteVerb(verb="lint", duration=120.0, timed_out=True),
            SuiteVerb(verb="typecheck", duration=0.0, refused="no justfile", tree_changed=True),
        ]
    )
    record = _closure(Outcome.CLOSED, suite=suite)
    text = store.write_run(record).read_text(encoding="utf-8")
    assert text.startswith("schema_version: 2\n")
    assert "verdict: red" in text
    assert "refused: no justfile" in text
    loaded = store.last_run("webapp")
    assert loaded is not None
    assert loaded.suite == suite


def test_green_suite_omits_the_keys_with_nothing_to_say(tmp_path: Path) -> None:
    """`exit_code` rides a green verb; `refused` and `detail` stay off it (`exclude_none`)."""
    store = Store(tmp_path / "home")
    text = store.write_run(_closure(Outcome.CLOSED, suite=green_suite())).read_text(
        encoding="utf-8"
    )
    assert "exit_code: 0" in text
    assert "refused" not in text
    assert "detail" not in text


def _verb(verb: str, **kwargs: object) -> SuiteVerb:
    payload: dict[str, object] = {"verb": verb, "duration": 1.0}
    if not ({"timed_out", "refused"} & kwargs.keys()):
        payload["exit_code"] = 0
    payload.update(kwargs)
    return SuiteVerb.model_validate(payload)


_TIMED_OUT_LINT = _verb("lint", timed_out=True)
_REFUSED_TYPECHECK = _verb("typecheck", refused="no justfile")


@pytest.mark.parametrize(
    ("verbs", "expected"),
    [
        ([_verb("test"), _verb("lint"), _verb("typecheck")], SuiteVerdict.GREEN),
        ([_verb("test", exit_code=1), _TIMED_OUT_LINT, _REFUSED_TYPECHECK], SuiteVerdict.RED),
        ([_verb("test"), _TIMED_OUT_LINT, _REFUSED_TYPECHECK], SuiteVerdict.TIMEOUT),
        ([_verb("test"), _verb("lint"), _REFUSED_TYPECHECK], SuiteVerdict.REFUSED),
    ],
    ids=["all-green", "red-wins", "timeout-over-refused", "refused-alone"],
)
def test_suite_fold_precedence(verbs: list[SuiteVerb], expected: SuiteVerdict) -> None:
    assert Suite.fold(verbs) is expected
    assert Suite.from_verbs(verbs).verdict is expected


def test_suite_verdict_must_fold_from_its_verbs() -> None:
    with pytest.raises(ValidationError, match="does not fold"):
        Suite(verdict=SuiteVerdict.GREEN, verbs=[_verb("test", exit_code=1)])


def test_suite_requires_at_least_one_verb() -> None:
    with pytest.raises(ValidationError, match="verbs"):
        Suite(verdict=SuiteVerdict.GREEN, verbs=[])


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"exit_code": 0, "timed_out": True},
        {"exit_code": 0, "refused": "no justfile"},
        {"refused": "   "},
    ],
    ids=["no-code-no-reason", "code-and-timeout", "code-and-refusal", "blank-refusal"],
)
def test_suite_verb_exit_code_absent_exactly_when_not_returned(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="exit_code must be absent exactly when"):
        SuiteVerb.model_validate({"verb": "test", "duration": 1.0, **kwargs})


def test_suite_verb_blank_detail_is_none() -> None:
    assert SuiteVerb(verb="test", exit_code=0, duration=1.0, detail="  ").detail is None
    assert SuiteVerb(verb="test", exit_code=0, duration=1.0, detail=None).detail is None


def test_started_at_round_trips_and_yields_duration(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            commit_sha="a" * 40,
            started_at=datetime(2026, 8, 19, 0, 0, 0, tzinfo=UTC),
            closed_at=datetime(2026, 8, 19, 0, 21, 30, tzinfo=UTC),
            summary="timed",
            hermes_review=HermesReview.REVIEWED,
        )
    )
    loaded = store.last_run("webapp")
    assert loaded is not None
    assert loaded.started_at == datetime(2026, 8, 19, 0, 0, 0, tzinfo=UTC)
    assert loaded.started_at is not None and loaded.closed_at is not None
    assert (loaded.closed_at - loaded.started_at).total_seconds() == 1290.0
    assert loaded.hermes_review is HermesReview.REVIEWED


def test_started_at_serializes_as_iso_z(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    path = store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            commit_sha="a" * 40,
            started_at=datetime(2026, 8, 19, 1, 2, 3, tzinfo=UTC),
            closed_at=datetime(2026, 8, 19, 1, 9, 9, tzinfo=UTC),
            summary="timed",
        )
    )
    assert "started_at: '2026-08-19T01:02:03Z'" in path.read_text(encoding="utf-8")


def test_run_lock_contention_is_user_error_exit_2(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    with store.run_lock("app", tmp_path / "app"):
        with pytest.raises(UserError, match="already active for app") as exc:
            with store.run_lock("app", tmp_path / "app"):
                pass
        assert exc.value.exit_code == 2


def test_run_lock_is_per_repo(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    with store.run_lock("app", tmp_path / "app"), store.run_lock("charts", tmp_path / "charts"):
        pass


def test_run_lock_keys_on_the_repo_not_the_id(tmp_path: Path) -> None:
    """Two ids at one repo — case-variant spelling included — share one lock (CBN-EPIC-313.2)."""
    store = Store(tmp_path / "home")
    repo = tmp_path / "Repo"
    variant = tmp_path / "repo"
    assert store.run_lock_path(repo) == store.run_lock_path(variant)
    with store.run_lock("app", repo):
        with pytest.raises(UserError, match="already active for twin's repo"):
            with store.run_lock("twin", variant):
                pass


def test_run_lock_released_on_exit(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    with store.run_lock("app", tmp_path / "app"):
        pass
    with store.run_lock("app", tmp_path / "app"):
        pass


def test_run_lock_released_on_raise(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    with pytest.raises(RuntimeError), store.run_lock("app", tmp_path / "app"):
        raise RuntimeError("boom")
    with store.run_lock("app", tmp_path / "app"):
        pass


def test_suite_lock_blocks_a_second_holder_until_released(tmp_path: Path) -> None:
    """Blocking and global (CBN-308): a second slug's suite waits, never refuses."""
    store = Store(tmp_path / "home")
    acquired = threading.Event()

    def second() -> None:
        with Store(tmp_path / "home").suite_lock():
            acquired.set()

    with store.suite_lock():
        waiter = threading.Thread(target=second, daemon=True)
        waiter.start()
        assert not acquired.wait(0.2)
    assert acquired.wait(5)
    waiter.join()


def test_suite_lock_calls_on_wait_only_when_contended(tmp_path: Path) -> None:
    """A free lock never calls `on_wait`; a held one calls it once, before blocking (CBN-310)."""
    store = Store(tmp_path / "home")
    waits: list[str] = []
    with store.suite_lock(lambda: waits.append("free")):
        pass
    assert waits == []

    waiting = threading.Event()
    acquired = threading.Event()

    def second() -> None:
        with Store(tmp_path / "home").suite_lock(waiting.set):
            acquired.set()

    with store.suite_lock():
        waiter = threading.Thread(target=second, daemon=True)
        waiter.start()
        assert waiting.wait(5)
        assert not acquired.is_set()
    assert acquired.wait(5)
    waiter.join()


def test_a_failing_on_wait_still_takes_the_lock(tmp_path: Path) -> None:
    """The wait notice is advisory: `on_wait` raising `OSError` must not skip the lock (CBN-310)."""
    store = Store(tmp_path / "home")
    waiting = threading.Event()
    held = threading.Event()
    release = threading.Event()
    second_in = threading.Event()

    def broken() -> None:
        waiting.set()
        raise BrokenPipeError

    def second() -> None:
        with Store(tmp_path / "home").suite_lock(broken):
            held.set()
            release.wait(5)

    def third() -> None:
        with Store(tmp_path / "home").suite_lock():
            second_in.set()

    with store.suite_lock():
        first = threading.Thread(target=second, daemon=True)
        first.start()
        assert waiting.wait(5)
        assert not held.wait(0.2)
    assert held.wait(5)
    other = threading.Thread(target=third, daemon=True)
    other.start()
    assert not second_in.wait(0.2)
    release.set()
    assert second_in.wait(5)
    first.join()
    other.join()


def test_suite_lock_is_not_the_write_lock(tmp_path: Path) -> None:
    """Registry/run writes must not queue behind a minutes-long suite."""
    store = Store(tmp_path / "home")
    # Path first, so a shared file fails here rather than deadlocking below.
    assert store.suite_lock_path != store.lock_path
    with store.suite_lock():
        store.register(RegistryRecord(id="app", path=str(tmp_path / "app")))


# --- Worker-proposed follow-ups (CBN-121) ---


def test_run_follow_ups_normalized_on_validate() -> None:
    record = RunRecord(
        project="app",
        task_id="CBN-121",
        outcome=Outcome.CLOSED,
        suite=green_suite(),
        commit_sha="a" * 40,
        closed_at=datetime(2026, 8, 19, tzinfo=UTC),
        summary="s",
        follow_ups=["  file it  ", "", "   "],
    )
    assert record.follow_ups == ["file it"]


def test_run_empty_follow_ups_is_none() -> None:
    """An empty declaration is undeclared — `exclude_none` then keeps the key off."""
    record = RunRecord(
        project="app",
        task_id="CBN-121",
        outcome=Outcome.CLOSED,
        suite=green_suite(),
        commit_sha="a" * 40,
        closed_at=datetime(2026, 8, 19, tzinfo=UTC),
        summary="s",
        follow_ups=[],
    )
    assert record.follow_ups is None
    assert "follow_ups" not in record.model_dump(mode="json", exclude_none=True)


# --- Worker-filed PLAN rows (CBN-138.3) ---


def test_run_empty_new_rows_is_none() -> None:
    """An empty declaration is undeclared, the same rule the string lists follow."""
    record = RunRecord(
        project="app",
        task_id="CBN-138.3",
        outcome=Outcome.CLOSED,
        suite=green_suite(),
        commit_sha="a" * 40,
        closed_at=datetime(2026, 8, 19, tzinfo=UTC),
        summary="s",
        new_rows=[],
    )
    assert record.new_rows is None
    assert "new_rows" not in record.model_dump(mode="json", exclude_none=True)


def test_run_new_rows_round_trip_through_the_store(tmp_path: Path) -> None:
    store = Store(tmp_path / "home")
    record = RunRecord(
        project="app",
        task_id="CBN-138.3",
        outcome=Outcome.CLOSED,
        suite=green_suite(),
        commit_sha="a" * 40,
        closed_at=datetime(2026, 8, 19, tzinfo=UTC),
        summary="s",
        new_rows=[NewRow(task_id="CBN-999", unattended=True)],
    )
    store.write_run(record)
    loaded = store.last_run("app")
    assert loaded is not None
    assert loaded.new_rows == [NewRow(task_id="CBN-999", unattended=True)]


def test_default_home_is_absolute_even_when_the_env_var_is_relative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CBN-251: a relative `$CAOBUNGA_HOME` must not resolve against whichever
    process's cwd happens to call `default_home()` — the worker's hook resolves
    it against the project cwd, not this one."""
    from caobunga.store import ENV_HOME, default_home

    monkeypatch.setenv(ENV_HOME, "relative-home")
    home = default_home()
    assert home.is_absolute()
    assert home == Path.cwd() / "relative-home"


def test_save_suite_lands_beside_the_run_it_measured(tmp_path: Path) -> None:
    """CBN-EPIC-202.3: same stamp and name as the run, under `suites/`."""
    store = Store(tmp_path / "home")
    log = store.save_suite("webapp", "20260819T000000123Z", "CBN-010", "verdict: green\n")
    assert log == tmp_path / "home" / "suites" / "webapp" / "20260819T000000123Z-CBN-010.log"
    assert log.read_text(encoding="utf-8") == "verdict: green\n"
    assert not list(log.parent.glob("*.tmp"))


def test_invocation_receipts_roundtrip_atomic_and_unique(tmp_path: Path) -> None:
    from caobunga.invocation import InvocationTrace

    store = Store(tmp_path)
    first = InvocationTrace("missing", store)
    second = InvocationTrace("missing", store)
    first.checkpoint()
    second.checkpoint()
    assert first.result.receipt_id != second.result.receipt_id
    loaded = store.load_invocation(first.result.receipt_id)
    assert loaded == first.result
    assert loaded.status.value == "running" and not loaded.successful
    assert not list((tmp_path / "invocations").glob("*.tmp"))
    with pytest.raises(UserError, match="invalid invocation id"):
        store.invocation_file("../escape")


def test_running_receipt_cannot_claim_success(tmp_path: Path) -> None:
    from caobunga.invocation import InvocationResult, InvocationTrace

    raw = InvocationTrace("app", Store(tmp_path)).result.model_dump()
    raw["successful"] = True
    with pytest.raises(ValidationError, match="success does not match"):
        InvocationResult.model_validate(raw)
