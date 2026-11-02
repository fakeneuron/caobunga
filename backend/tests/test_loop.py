"""Gated / observe run loop: tiers, drain, eligibility, dirt. In-memory fake; tmp git repos."""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    CLOSED_PLAN,
    EXHAUSTED_PLAN,
    OPEN_PLAN,
    TWO_CLOSED,
    FakeWorker,
    _BusyWorker,
    _close_worker,
    _closing_client,
    _commit,
    _dirty,
    _git,
    _git_repo,
    _green_suite,
    _not_green_suite,
    _note,
    _register,
    _run,
    _usage,
)

import caobunga
from caobunga import finish, launch, loop, prompt, report, run_echo, wait
from caobunga.errors import GitUnreadable, NoTaskError, Unlaunchable, UserError
from caobunga.flaitron import (
    FT_TASK_SKILL_DIR_REL,
    PARK_UNCOMMITTED,
    SELF_FT_TASK_SKILL_DIR_REL,
    UNATTENDED_MODULE,
    NextTask,
    OpenTasknote,
    PlanPriority,
    ProjectRead,
    parse_plan,
)
from caobunga.git import git_dirt, main_worktree
from caobunga.loop import execute_run
from caobunga.models import (
    Escalation,
    ExecutionTier,
    GlobalConfig,
    InvocationStatus,
    NewRow,
    Outcome,
    RegistryRecord,
    RunRecord,
    StopReason,
    utc_now,
)
from caobunga.prompt import FOLLOWUP_FILE, FOREGROUND_ONLY, PARK_CHECK, PINNED_SKILL, worker_message
from caobunga.store import Store
from caobunga.suite import SuiteRun
from caobunga.worker import STATUS_EXITED, Session

# The pinned-skill cue (CBN-291) as each layout renders it: a seeded
# `.flaitron/core` pin, or a core-less flaitron-self tree. A fixture with
# neither on disk gets no cue at all.
PINNED = PINNED_SKILL.format(skill=(FT_TASK_SKILL_DIR_REL / "SKILL.md").as_posix())
SELF_PINNED = PINNED_SKILL.format(skill=(SELF_FT_TASK_SKILL_DIR_REL / "SKILL.md").as_posix())
FILED = FOLLOWUP_FILE


def test_observe_does_not_launch(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.OBSERVE)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker:
        result = execute_run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            echo=lines.append,
        )
    assert result is None
    assert fake.created is None
    assert store.last_run("app") is None
    assert any(line.startswith("next: CBN-011") for line in lines)


@pytest.mark.parametrize(
    ("pins", "expected"),
    [
        ({}, "codex"),
        ({"providers": ["claude_code"]}, "claude_code"),
        ({"provider": "grok_cli"}, "grok_cli"),
    ],
    ids=["global-applies", "record-list-wins", "pin-wins"],
)
def test_global_providers_default_only_unlisted_records(
    tmp_path: Path, caobunga_home: Path, pins: dict[str, object], expected: str
) -> None:
    """CBN-414: `config.yaml` providers stand in only for a record with no pin and no list."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    store.save_config(GlobalConfig(providers=["codex", "claude_code"]))
    _register(store, repo, **pins)  # type: ignore[arg-type]
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.provider == expected
    # Resolved for the run only — never written back into the record.
    assert store.get("app").providers == pins.get("providers")


def test_observe_reports_suite_fitness(tmp_path: Path, caobunga_home: Path) -> None:
    """Report-only (CBN-215): observe names whether a close could ever be `suite: green`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.OBSERVE)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker:
        result = execute_run("app", store=store, worker=worker, poll_interval=0, echo=lines.append)
    assert result is None
    assert f"suite-fitness: refused — no justfile at {repo.resolve()}" in lines


def test_gated_writes_closed_run(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.task_id == "CBN-011"
    assert result.commit_sha is not None
    assert result.next_task == "CBN-012"
    assert result.plan_model == "medium"
    assert result.tasknote_path == ".flaitron/tasknote/archive/cbn/CBN-011.md"
    assert result.provider == "codex"
    assert result.model == "gpt-6.1-sol"
    assert fake.created is not None
    assert fake.created["provider"] == "codex"
    assert fake.created["model"] == "gpt-6.1-sol"
    loaded = store.last_run("app")
    assert loaded is not None
    assert loaded.task_id == "CBN-011"
    assert loaded.model == "gpt-6.1-sol"
    assert result.effort == "medium"
    assert loaded.effort == "medium"
    assert fake.messages == [
        f"/ft-task CBN-011 --unattended {PINNED} {FOREGROUND_ONLY} {PARK_CHECK} {FILED}"
    ]
    assert fake.deleted


def test_gated_record_has_no_effort_when_the_provider_takes_no_flag(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-453: cursor_cli bakes effort into the model id, so `EFFORT` has no row to record."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, provider="cursor_cli")
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.provider == "cursor_cli"
    assert result.effort is None


def test_record_effort_is_the_rung_launch_passed(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-453: grok_cli light/medium/heavy record the same rungs `launch` was given."""
    for task_class, rung in (("light", "medium"), ("medium", "high"), ("heavy", "xhigh")):
        plan = OPEN_PLAN.replace("**CBN-011** [medium]", f"**CBN-011** [{task_class}]")
        repo = _git_repo(tmp_path / task_class, plan)
        store = Store(caobunga_home)
        _register(store, repo, slug=task_class, provider="grok_cli")
        worker = _BusyWorker(lambda repo=repo: _close_worker(repo), busy_polls=0)
        result = _run(task_class, store=store, worker=worker, poll_interval=0, timeout=2)
        assert result is not None
        assert worker.effort == rung
        assert result.effort == rung
        loaded = store.last_run(task_class)
        assert loaded is not None
        assert loaded.effort == rung


def test_gated_records_the_preflight_marked_set(tmp_path: Path, caobunga_home: Path) -> None:
    """The marker-drift baseline (CBN-125) is the queue state this pick decided from.

    Both marked rows are recorded — including `CBN-012`, which this run did not
    dispatch. The record answers "what was armed when we launched", not "what we
    launched", which is what makes a later `caobunga status` able to see that an
    unrelated line edit disarmed the cohort.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.marked_tasks == ["CBN-011", "CBN-012"]
    loaded = store.last_run("app")
    assert loaded is not None
    assert loaded.marked_tasks == ["CBN-011", "CBN-012"]


def test_gated_run_echoes_launch_line(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-EPIC-449.2: a started worker names its task, pick, and tier — echo-only."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    lines: list[str] = []
    with _closing_client(FakeWorker(), lambda _msg: _close_worker(repo)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert [line for line in lines if line.startswith("launch:")] == [
        f"launch: CBN-011 · provider {result.provider} · model {result.model} · tier gated"
    ]


def test_sub_floor_pin_degrades_to_fast(tmp_path: Path, caobunga_home: Path) -> None:
    """A target whose pin predates `--unattended` gets `--fast`, plus an echo.

    Degrade, never refuse (CBN-108): `--fast` is the prompt CBN-EPIC-14.3
    replaced and every pin accepts it, so the run still closes normally.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN, unattended=False)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    # No skill dir on disk at all, so no pinned-skill cue (CBN-291).
    assert fake.messages == [f"/ft-task CBN-011 --fast {FOREGROUND_ONLY} {PARK_CHECK} {FILED}"]
    assert "app: flaitron pin does not recognize --unattended; sending --fast" in lines
    assert not any("does not commit" in line for line in lines)  # one cause, one echo


def test_pre_commit_park_recipe_warns_at_dispatch(tmp_path: Path, caobunga_home: Path) -> None:
    """A capable pin whose park never commits is warned, not refused (CBN-459)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE).write_text("posture\n", encoding="utf-8")
    _commit(repo, "v7 recipe")
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert fake.messages == [
        f"/ft-task CBN-011 --unattended {PINNED} {FOREGROUND_ONLY} {PARK_CHECK} {FILED}"
    ]
    assert PARK_UNCOMMITTED.format(id="app") in lines


def test_half_seeded_pin_degrades_to_fast(tmp_path: Path, caobunga_home: Path) -> None:
    """Both halves are required: the module without the token is not capable."""
    repo = _git_repo(tmp_path, OPEN_PLAN, unattended=False)
    skill_dir = repo / FT_TASK_SKILL_DIR_REL
    skill_dir.mkdir(parents=True)
    (skill_dir / UNATTENDED_MODULE).write_text("posture\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text("Trailing `--fast` / `-f` only.\n", encoding="utf-8")
    _commit(repo, "seed skill")
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert fake.messages == [
        f"/ft-task CBN-011 --fast {PINNED} {FOREGROUND_ONLY} {PARK_CHECK} {FILED}"
    ]


def test_capable_pin_sends_unattended_without_echo(tmp_path: Path, caobunga_home: Path) -> None:
    """The capable path stays silent — the echo marks the exception, not the norm."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append)
    assert fake.messages == [
        f"/ft-task CBN-011 --unattended {PINNED} {FOREGROUND_ONLY} {PARK_CHECK} {FILED}"
    ]
    # The probe does not echo on a capable pin — the degrade notice marks the
    # exception, not the norm (CBN-108).
    assert not any("does not recognize" in line for line in lines)
    assert not any("does not commit" in line for line in lines)


def test_in_progress_sends_continue(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _note("in-progress")})
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert fake.messages[0].startswith("Continue CBN-011")
    assert not fake.messages[0].startswith("/ft-task")


PARKED_HEAD_PLAN = OPEN_PLAN.replace("[medium] [unattended] | cao-client", "[medium] | cao-client")


def test_parked_only_row_is_exit_3_without_record_or_review(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-252: a parked row is denied, not re-picked — no launch, no record."""
    repo = _git_repo(
        tmp_path, PARKED_HEAD_PLAN, notes={"CBN-011": _note("blocked", "drift — premise")}
    )
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker, pytest.raises(NoTaskError) as exc:
        _run("app", store=store, worker=worker, poll_interval=0, echo=lines.append)
    assert exc.value.exit_code == 3
    assert "CBN-011 (parked)" in str(exc.value)
    assert (
        "eligibility: CBN-011 deny: parked — tasknote is blocked (drift); "
        "resume attended with /ft-task CBN-011"
    ) in lines
    assert fake.created is None
    assert store.last_run("app") is None


def test_parked_head_falls_through_to_the_next_marked_row(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _note("blocked")})
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    closed = OPEN_PLAN.replace("- [ ] **CBN-012**", "- [x] **CBN-012**")
    with _closing_client(fake, lambda _msg: _close_worker(repo, "CBN-012", closed)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.task_id == "CBN-012"
    assert "CBN-012" in fake.messages[0]


def test_no_task_is_exit_3(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, EXHAUSTED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    with pytest.raises(NoTaskError) as exc:
        execute_run("app", store=store, poll_interval=0)
    assert exc.value.exit_code == 3
    assert store.last_run("app") is None


def test_later_tier_exhausted_is_exit_3(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, EXHAUSTED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    with pytest.raises(NoTaskError) as exc:
        execute_run("app", store=store, poll_interval=0)
    assert exc.value.exit_code == 3
    assert store.last_run("app") is None


def test_disabled_refuses(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    rec = RegistryRecord(
        id="app",
        path=str(repo.resolve()),
        tier=ExecutionTier.GATED,
        registered_at=utc_now(),
        enabled=False,
    )
    store.register(rec)
    with pytest.raises(UserError, match="disabled") as exc:
        execute_run("app", store=store)
    assert exc.value.exit_code == 2


def test_timeout_writes_failed(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=0.05)
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert "timed out" in result.summary
    assert result.commit_sha is None
    assert result.provider == "codex"
    assert result.model == "gpt-6.1-sol"


def test_worker_exit_without_close_writes_failed(tmp_path: Path, caobunga_home: Path) -> None:
    """A one-shot worker can quit cleanly having closed nothing — escalate, don't stall."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def flip(_message: str) -> None:
        fake.state = STATUS_EXITED

    with _closing_client(fake, flip) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=600)
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert "exited without closing CBN-011" in result.summary


def test_worker_exit_after_close_still_lands_closed(tmp_path: Path, caobunga_home: Path) -> None:
    """The close may land on the worker's last action — re-check before escalating."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def flip(_message: str) -> None:
        _close_worker(repo)
        fake.state = STATUS_EXITED

    with _closing_client(fake, flip) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=600)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.task_id == "CBN-011"


def test_registry_change_during_run_escalates(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-424: a registry write while the worker ran fails the run and names the files."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close_and_retier(_message: str) -> None:
        _close_worker(repo)
        project = store.project_file("app")
        project.write_text(project.read_text(encoding="utf-8") + "# retiered\n", encoding="utf-8")
        store.config_file().write_text("providers: [codex]\n", encoding="utf-8")

    fake.on_send = close_and_retier
    with fake as worker:
        receipt = loop.execute_invocation(
            "app",
            store=store,
            worker=worker,
            usage=_usage(),
            poll_interval=0,
            timeout=2,
            run_suite=_green_suite,
        )
    result = store.last_run("app")
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.REGISTRY_CHANGED
    assert result.summary.startswith(
        "registry changed during run: config.yaml, projects/app.yaml — "
    )
    assert result.commit_sha is not None
    assert result.suite is None
    assert receipt.legacy_exit_code == 4
    assert any("registry changed while the worker ran" in step for step in receipt.recovery)
    assert not any("no task record carries it" in step for step in receipt.recovery)


def test_unchanged_registry_leaves_the_close(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-424: the loop's own writes during a run never trip the registry digest."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    store.config_file().write_text("providers: [grok_cli]\n", encoding="utf-8")
    with _closing_client(FakeWorker(), lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.escalation_reason is None
    assert result.suite is not None


def test_not_git_is_usage_error(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "repo"
    tasknotes = repo / ".flaitron" / "tasknote"
    tasknotes.mkdir(parents=True)
    (repo / ".flaitron" / "PLAN.md").write_text(OPEN_PLAN, encoding="utf-8")
    store = Store(caobunga_home)
    _register(store, repo)
    with pytest.raises(UserError, match="not a git repository"):
        execute_run("app", store=store, poll_interval=0)


def test_unreadable_git_is_not_reported_as_not_a_repo(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-269: a hung or missing `git` says so, rather than "not a git repository"."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def unreadable(root: Path) -> str | None:
        raise GitUnreadable(root)

    monkeypatch.setattr("caobunga.loop.git_head", unreadable)
    with pytest.raises(UserError, match="git unreadable") as info:
        execute_run("app", store=store, poll_interval=0)
    assert info.value.exit_code == 5
    assert store.list_runs("app") == []


def test_worker_message_shapes() -> None:
    plain = NextTask(
        task_id="CBN-011",
        priority=PlanPriority.HIGH,
        reason="spec-selection",
    )
    fresh = worker_message(
        plain,
        unattended=True,
        unattended_module=FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE,
        pinned_skill=FT_TASK_SKILL_DIR_REL / "SKILL.md",
    )
    assert fresh.startswith("/ft-task CBN-011 --unattended ")
    assert fresh.endswith("`/ft-file-task --unattended`.")
    # The pinned-skill cue names the probed dir's SKILL.md (CBN-291).
    assert f"--unattended {PINNED} " in fresh
    self_fresh = worker_message(
        plain,
        unattended=False,
        unattended_module=SELF_FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE,
        pinned_skill=SELF_FT_TASK_SKILL_DIR_REL / "SKILL.md",
    )
    assert f"--fast {SELF_PINNED} " in self_fresh
    assert ".flaitron/core" not in self_fresh
    # No pinned SKILL.md on disk → no cue, never a pointer at a missing file.
    bare = worker_message(
        plain,
        unattended=False,
        unattended_module=FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE,
        pinned_skill=None,
    )
    assert bare == f"/ft-task CBN-011 --fast {FOREGROUND_ONLY} {PARK_CHECK} {FILED}"
    assert worker_message(
        plain,
        unattended=False,
        unattended_module=FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE,
        pinned_skill=FT_TASK_SKILL_DIR_REL / "SKILL.md",
    ).startswith("/ft-task CBN-011 --fast ")
    in_flight = NextTask(
        task_id="CBN-011",
        priority=PlanPriority.HIGH,
        reason="in-progress-override",
        tasknote=OpenTasknote(
            task_id="CBN-011",
            path=".flaitron/tasknote/CBN-011.md",
            status="in-progress",
        ),
    )
    # The continue prompt carries the posture too (CBN-109) — the probe reaches
    # this branch as well, and it is the branch entered only after a prior
    # session already stranded the note.
    resumed = worker_message(
        in_flight,
        unattended=True,
        unattended_module=FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE,
        pinned_skill=FT_TASK_SKILL_DIR_REL / "SKILL.md",
    )
    assert resumed.startswith("Continue CBN-011.")
    assert "--unattended posture" in resumed
    assert "(`.flaitron/core/claude/skills/ft-task/unattended-mode.md`)" in resumed
    assert "`status: blocked`" in resumed
    # The path is rendered from the probe's skill dir, not a literal (CBN-189):
    # a core-less flaitron-self keeps the module at `claude/skills/ft-task/`.
    self_resumed = worker_message(
        in_flight,
        unattended=True,
        unattended_module=SELF_FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE,
        pinned_skill=SELF_FT_TASK_SKILL_DIR_REL / "SKILL.md",
    )
    assert "(`claude/skills/ft-task/unattended-mode.md`)" in self_resumed
    assert ".flaitron/core" not in self_resumed
    degraded = worker_message(
        in_flight,
        unattended=False,
        unattended_module=FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE,
        pinned_skill=FT_TASK_SKILL_DIR_REL / "SKILL.md",
    )
    assert degraded.startswith("Continue CBN-011.")
    assert "--fast posture" in degraded
    assert "unattended-mode.md" not in degraded
    # The cue rides every shape, unprobed since the v7 hard cut (CBN-138.2,
    # CBN-330). The foreground-only fact (CBN-199) rides every shape too,
    # unconditionally — it is static and provider-neutral, with no probe and
    # no degrade.
    for unattended in (True, False):
        for candidate in (plain, in_flight):
            msg = worker_message(
                candidate,
                unattended=unattended,
                unattended_module=FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE,
                pinned_skill=FT_TASK_SKILL_DIR_REL / "SKILL.md",
            )
            assert msg.endswith(FOLLOWUP_FILE)
            assert FOREGROUND_ONLY in msg
            assert PARK_CHECK in msg  # CBN-327: static, every shape
            # Only a fresh launch invokes `/ft-task`, so only it gets the
            # pinned-skill cue (CBN-291).
            assert (PINNED in msg) is (candidate is plain)


def test_gated_close_writes_no_hermes_surface(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-258: the Hermes review is retired — no `hermes_*` key, no brief or review copy."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    raw = store.run_file(result).read_text(encoding="utf-8")
    assert "hermes" not in raw
    assert not (caobunga_home / "briefs").exists()
    assert not (caobunga_home / "reviews").exists()
    assert not [line for line in lines if line.startswith(("brief:", "hermes:", "deliver:"))]


def _session_count(fake: FakeWorker) -> int:
    return len(fake.launches)


def test_proceed_on_green_walks_while_closed(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    dumped: list[str] = []
    closes = iter(
        [
            lambda: _close_worker(repo, "CBN-011", CLOSED_PLAN),
            lambda: _close_worker(repo, "CBN-012", TWO_CLOSED),
        ]
    )
    with _closing_client(fake, lambda _msg: next(closes)()) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            on_run=lambda run: dumped.append(run.task_id),
        )
    assert result is not None
    assert result.task_id == "CBN-012"
    assert result.outcome is Outcome.CLOSED
    assert dumped == ["CBN-011", "CBN-012"]
    assert _session_count(fake) == 2
    assert [run.task_id for run in store.list_runs("app")] == ["CBN-011", "CBN-012"]


def test_proceed_on_green_stops_on_descoped(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    with _closing_client(
        fake, lambda _msg: _close_worker(repo, "CBN-011", CLOSED_PLAN, descoped=True)
    ) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.DE_SCOPED
    assert result.task_id == "CBN-011"
    assert _session_count(fake) == 1
    assert [run.task_id for run in store.list_runs("app")] == ["CBN-011"]


def test_drain_park_exit_names_the_outcome_and_park_code(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-119.4. The third exit — the one a queue headed by a 🗄️ hand-off takes.

    The head row requires an operator action, so an unattended worker parks and
    `BLOCKED` stops the drain at task one. The echo names the park code, so that
    stop is never read as a walk.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked", "destructive — sql"), encoding="utf-8")

    with _closing_client(fake, park) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.outcome is Outcome.BLOCKED
    # The worker left its park uncommitted, so the park holds the repo (CBN-443).
    assert (
        "drain: outcome — CBN-011 outcome blocked (destructive) — repo held: "
        "1 uncommitted change(s): ?? .flaitron/tasknote/CBN-011.md"
    ) in lines


def _park_committed(repo: Path, *, extra: str | None = None) -> Callable[[str], None]:
    """A worker that parks CBN-011 `drift` and commits, as the park-check cue asks (CBN-443).

    `extra` names a non-workflow file committed with the park, which must hold the repo.
    """

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked", "drift — plan moved"), encoding="utf-8")
        if extra is not None:
            (repo / extra).write_text("source\n", encoding="utf-8")
        _commit(repo, "chore: park CBN-011 — drift")

    return park


def _plan_with_012_closed() -> str:
    return OPEN_PLAN.replace(
        "- [ ] **CBN-012** [medium] [unattended] | cao-client — client work.\n", ""
    ).replace(
        "## Completed\n\n(none)",
        "## Completed\n\n- [x] **CBN-012** [medium] | cao-client — Completed 2026-08-19.",
    )


def test_drain_continues_past_a_committed_clean_park(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-443. A park whose worker committed only its note lets the drain go on.

    The re-read denies the parked row `parked`, so the drain takes CBN-012 and
    then exits `incomplete` on that denial; the receipt names the park, the
    resume, and what ran after it, and the invocation is not successful.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    sends = iter(
        [
            _park_committed(repo),
            lambda _m: _close_worker(repo, "CBN-012", _plan_with_012_closed()),
        ]
    )

    with _closing_client(fake, lambda message: next(sends)(message)) as worker:
        result = loop.execute_invocation(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            usage=_usage(),
            run_suite=_green_suite,
        )

    runs = store.list_runs("app")
    assert [(run.task_id, run.outcome) for run in runs] == [
        ("CBN-011", Outcome.BLOCKED),
        ("CBN-012", Outcome.CLOSED),
    ]
    assert runs[0].commit_sha is not None
    assert "park: CBN-011 committed with a clean tree — continuing the drain" in lines
    handoff = (
        "parked CBN-011 (drift — plan moved) → resume /ft-task CBN-011; continued with CBN-012"
    )
    assert f"parked: {handoff}" in lines
    assert result.status is InvocationStatus.INCOMPLETE
    assert not result.successful
    assert result.remaining_tasks == ["CBN-011"]
    assert [park.handoff() for park in result.parked] == [handoff]
    card = store.status_file("app").read_text(encoding="utf-8")
    assert f"- **parked:** {handoff}" in card


def test_drain_holds_a_park_that_committed_source(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-443. A park whose window carries a non-workflow path holds the repo.

    A park runs no suite, so the drain must not hand that tree to the next worker.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []

    with _closing_client(fake, _park_committed(repo, extra="src.py")) as worker:
        result = loop.execute_invocation(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            usage=_usage(),
            run_suite=_green_suite,
        )

    assert _session_count(fake) == 1
    held = "non-workflow path(s) committed since launch: src.py"
    assert f"drain: outcome — CBN-011 outcome blocked (drift) — repo held: {held}" in lines
    assert result.stop_reason is StopReason.OUTCOME
    assert [(park.task_id, park.held, park.continued_with) for park in result.parked] == [
        ("CBN-011", held, [])
    ]
    handoff = f"parked CBN-011 (drift — plan moved) → resume /ft-task CBN-011; repo held: {held}"
    assert f"parked: {handoff}" in lines
    # The parking run's own card names the hold: no later run overwrites it.
    card = store.status_file("app").read_text(encoding="utf-8")
    assert f"- **parked:** {handoff}" in card


def test_drain_names_a_descoped_park_as_nothing_to_resume(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-443 review. A De-scope park continues, but its handoff says close it, not resume."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    lines: list[str] = []

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        body = _note("blocked", "drift — premise gone")
        note.write_text(body + "\n**Verdict:** De-scope\n", encoding="utf-8")
        _commit(repo, "chore: park CBN-011 — drift")

    sends = iter([park, lambda _m: _close_worker(repo, "CBN-012", _plan_with_012_closed())])
    with _closing_client(FakeWorker(), lambda message: next(sends)(message)) as worker:
        result = loop.execute_invocation(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            usage=_usage(),
            run_suite=_green_suite,
        )

    assert [park.handoff() for park in result.parked] == [
        "parked CBN-011 (drift — premise gone) → De-scope verdict; confirm and close it attended; "
        "continued with CBN-012"
    ]


def test_uncommitted_park_refusal_names_the_resume(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-443. A worker that skipped its park commit leaves a filing-shaped refusal.

    The parked note is the last run's own, so the refusal offers the commit; it
    now also names the resume.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked", "drift — plan moved"), encoding="utf-8")

    with _closing_client(FakeWorker(), park) as worker:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2)

    with pytest.raises(UserError) as excinfo:
        _run("app", store=store, worker=FakeWorker(), poll_interval=0, timeout=2)
    message = str(excinfo.value)
    assert "flaitron filing artifacts" in message
    assert message.endswith("— CBN-011 is parked; resume it attended with /ft-task CBN-011")


def test_proceed_on_green_exhaustion_is_complete_when_nothing_was_denied(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-119.4. Exhaustion with nothing denied: the whole queue was walked."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    closes = iter(
        [
            lambda: _close_worker(repo, "CBN-011", CLOSED_PLAN),
            lambda: _close_worker(repo, "CBN-012", TWO_CLOSED),
        ]
    )
    with _closing_client(fake, lambda _msg: next(closes)()) as worker:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append)
    assert "drain: complete — no eligible candidate remains" in lines


def test_proceed_on_green_exhaustion_is_incomplete_when_a_row_was_denied(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-119.4. The marker exit — the pre-existing
    `test_drain_stops_at_unmarked_task_and_records_the_allow` scenario, which
    until now ended without ever saying that an unmarked row was why."""
    repo = _git_repo(tmp_path, MIXED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo, "CBN-011", MIXED_AFTER)) as worker:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append)
    assert "drain: incomplete — no eligible candidate; 1 denied: CBN-012 (unmarked)" in lines


def test_drain_does_not_dispatch_a_row_its_worker_marked(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-246. The worker that closes CBN-011 also arms CBN-012 in its commit.

    The re-read would pick CBN-012; the drain is pinned to the marked set its
    first read saw, so it stops instead of dispatching a row it never had
    operator consent for.
    """
    repo = _git_repo(tmp_path, MIXED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    self_armed = MIXED_AFTER.replace("CBN-012** [medium] |", "CBN-012** [medium] [unattended] |")
    assert self_armed != MIXED_AFTER
    with _closing_client(fake, lambda _msg: _close_worker(repo, "CBN-011", self_armed)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.task_id == "CBN-011"
    assert _session_count(fake) == 1
    assert [run.task_id for run in store.list_runs("app")] == ["CBN-011"]
    assert (
        "drain: unpinned — CBN-012 not dispatchable on the drain's first read; "
        "not dispatched — check who changed it before re-running" in lines
    )


def test_drain_does_not_dispatch_a_row_its_worker_disarmed(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-260. CBN-012 is marked but `[handoff]` at first read; the worker strips the token.

    The row carried the marker all along, so the literal-marker pin used to
    hold it and the re-read dispatched it headless. The pin is now what the
    first read would dispatch, so the stripped row stops the drain.
    """
    handoff_plan = MIXED_PLAN.replace(
        "CBN-012** [medium] |", "CBN-012** [medium] [unattended] [handoff] |"
    )
    stripped = MIXED_AFTER.replace("CBN-012** [medium] |", "CBN-012** [medium] [unattended] |")
    assert handoff_plan != MIXED_PLAN
    assert stripped != MIXED_AFTER
    repo = _git_repo(tmp_path, handoff_plan)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo, "CBN-011", stripped)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.task_id == "CBN-011"
    assert _session_count(fake) == 1
    assert any(line.startswith("drain: unpinned — CBN-012 ") for line in lines)


def _pin_read(row: str, *, note: OpenTasknote | None = None) -> ProjectRead:
    plan = f"## High\n\n- [x] **CBN-001** [medium] | done — closed.\n{row}\n"
    return ProjectRead(tasks=parse_plan(plan), open_tasknotes=[note] if note else [])


@pytest.mark.parametrize(
    ("row", "pinned"),
    [
        ("- [ ] **CBN-012** [medium] [unattended] | ok — plain.", True),
        ("- [ ] **CBN-012** [medium] [unattended] | ok — Blocked by [[CBN-099]]", True),
        ("- [ ] **CBN-012** [medium] | ok — unmarked.", False),
        ("- [ ] **CBN-012** [medium] [unattended] [handoff] | h — hand-off.", False),
        ("- [ ] **CBN-012** [handoff] [unattended] | h — model-slot hand-off.", False),
        ("- [ ] **CBN-012** [xheavy] [unattended] | x — exploratory.", False),
        ("- [ ] **CBN-012** [heavy] [xheavy] [unattended] | x — extras xheavy.", False),
        ("- [ ] **CBN-012** [fable] [unattended] | f — frontier band.", False),
        (
            "- [ ] **CBN-012** [medium] [unattended] [handoff] | h — Blocked by [[CBN-099]]",
            False,
        ),
    ],
)
def test_drain_pin_is_what_the_first_read_would_dispatch(row: str, *, pinned: bool) -> None:
    """CBN-260. Eligible, or eligible once `Blocked by` clears — nothing else."""
    assert (loop._marked_ids(_pin_read(row)) == {"CBN-012"}) is pinned


def test_drain_pin_keeps_an_audit_whose_siblings_are_open() -> None:
    """CBN-309. Siblings can close mid-drain, so the `audit` gate stays out of the pin."""
    plan = (
        "## High\n\n"
        "- [ ] **CBN-EPIC-41.2** [light] [unattended] | a — open sibling.\n"
        "- [ ] **CBN-EPIC-41.N** [light] [unattended] | audit — marked with the cohort.\n"
    )
    read = ProjectRead(tasks=parse_plan(plan), open_tasknotes=[])
    assert loop._marked_ids(read) == {"CBN-EPIC-41.2", "CBN-EPIC-41.N"}


def test_drain_pin_skips_a_parked_row() -> None:
    parked = OpenTasknote(
        task_id="CBN-012", path="x", status="blocked", park_reason="input-needed — x"
    )
    row = "- [ ] **CBN-012** [medium] [unattended] | ok — plain."
    assert loop._marked_ids(_pin_read(row, note=parked)) == frozenset()


def test_gated_is_not_a_drain_and_echoes_no_exit(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-119.4. The vocabulary describes walks; a one-shot did not walk."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.GATED)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append)
    assert not [line for line in lines if line.startswith("drain:")]


@pytest.mark.parametrize(
    ("verdict", "detail"),
    [
        ("red", "test exit 1"),
        ("timeout", "test timeout"),
        ("refused", "test refused: no justfile"),
    ],
)
def test_proceed_on_green_stops_on_a_non_green_suite(
    tmp_path: Path,
    caobunga_home: Path,
    verdict: str,
    detail: str,
) -> None:
    """CBN-EPIC-202.4. The close closed; its suite did not pass; the drain stops.

    Continue is `closed` **and** `green`: the second eligible row is never run,
    and the exit names the fourth reason with the failing verb. `refused` stops
    too: a drain that cannot measure its suite is the state the gate exists for.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    closes = iter(
        [
            lambda: _close_worker(repo, "CBN-011", CLOSED_PLAN),
            lambda: _close_worker(repo, "CBN-012", TWO_CLOSED),
        ]
    )
    with _closing_client(fake, lambda _msg: next(closes)()) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            run_suite=_not_green_suite(verdict),
        )
    assert result is not None
    assert result.task_id == "CBN-011"
    assert result.outcome is Outcome.CLOSED
    assert result.suite is not None
    assert result.suite.verdict.value == verdict
    assert _session_count(fake) == 1
    assert f"drain: suite — CBN-011 suite {verdict} — {detail}" in lines
    assert [run.task_id for run in store.list_runs("app")] == ["CBN-011"]


@pytest.mark.parametrize("tier", [ExecutionTier.GATED, ExecutionTier.PROCEED_ON_GREEN])
@pytest.mark.parametrize(
    ("verdict", "detail"),
    [
        ("red", "test exit 1"),
        ("timeout", "test timeout"),
        ("refused", "test refused: no justfile"),
    ],
)
def test_non_green_baseline_refuses_before_launch(
    tmp_path: Path, caobunga_home: Path, tier: ExecutionTier, verdict: str, detail: str
) -> None:
    """CBN-420. A suite already failing on the clean tree refuses the repo before any
    worker exists: exit 2, no task record, the log kept, the pick's allow withdrawn."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=tier)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker, pytest.raises(UserError) as caught:
        _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            run_suite=_not_green_suite(verdict, baseline_green=False),
        )
    assert caught.value.exit_code == 2
    (log,) = store.suites_dir("app").glob("*-CBN-011-baseline.log")
    message = str(caught.value)
    assert f"baseline is {verdict} on the clean tree before launch ({detail})" in message
    assert f"suite log: {log}" in message
    assert not fake.launches
    assert store.list_runs("app") == []
    assert any(line.startswith(f"baseline: {verdict} — test ") for line in lines)
    assert f"eligibility: CBN-011 allow withdrawn: baseline-{verdict} — not launching" in lines


def test_green_baseline_runs_once_per_drain(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-420. Only the first dispatch measures a baseline: a drain continues only on
    a green post-close suite, which already measured the tree the next task starts on."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    measured: list[int] = []

    def runner(root: Path, st: Store) -> SuiteRun:
        measured.append(len(fake.launches))
        return _green_suite(root, st)

    closes = iter(
        [
            lambda: _close_worker(repo, "CBN-011", CLOSED_PLAN),
            lambda: _close_worker(repo, "CBN-012", TWO_CLOSED),
        ]
    )
    with _closing_client(fake, lambda _msg: next(closes)()) as worker:
        _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            run_suite=runner,
        )
    # Baseline before any launch, then one post-close suite per task.
    assert measured == [0, 1, 2]
    baselines = [line for line in lines if line.startswith("baseline:")]
    assert baselines == ["baseline: green — test 0 1.5s · lint 0 0.2s · typecheck 0 0.4s"]
    assert not list(store.suites_dir("app").glob("*-baseline.log"))


def test_dirty_tree_refuses_before_the_baseline(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-420. Dirt is the more specific refusal, and a suite would measure it."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    (repo / "partial.py").write_text("partial\n", encoding="utf-8")
    measured: list[Path] = []

    def runner(root: Path, st: Store) -> SuiteRun:
        measured.append(root)
        return _green_suite(root, st)

    with FakeWorker() as worker, pytest.raises(UserError, match="uncommitted change"):
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2, run_suite=runner)
    assert measured == []


def test_red_baseline_still_refuses_when_its_log_cannot_be_saved(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-420. A failed log write names itself; it never turns the refusal into a launch."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def no_space(*_args: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_suite", no_space)
    fake = FakeWorker()
    with fake as worker, pytest.raises(UserError, match="suite log not saved: disk full"):
        _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            run_suite=_not_green_suite("red", baseline_green=False),
        )
    assert not fake.launches


def test_baseline_without_just_is_the_machines_state(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-420 review: no `just` on PATH is exit 5, never a per-repo exit-2 skip that
    `fleet` would fold green (CBN-EPIC-313.5)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / "justfile").write_text("test:\n\ttrue\n", encoding="utf-8")
    _commit(repo, "add justfile")
    store = Store(caobunga_home)
    _register(store, repo)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: None)
    fake = FakeWorker()
    with fake as worker, pytest.raises(Unlaunchable, match="`just` is not on PATH") as caught:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2, run_suite=None)
    assert caught.value.exit_code == 5
    assert not fake.launches


def test_baseline_rereads_head_and_tree_before_launch(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-420 review: the suite takes minutes, so the launch's `start_sha` and dirt
    gate are read again after it — an operator commit meanwhile is not the worker's."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def operator_commits(root: Path, st: Store) -> SuiteRun:
        if not fake.launches:
            (repo / "operator.txt").write_text("x\n", encoding="utf-8")
            _commit(repo, "operator: land during the baseline")
        return _green_suite(root, st)

    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            run_suite=operator_commits,
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.foreign_commits is None


def test_baseline_that_dirties_the_tree_refuses_as_dirt(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-420 review: suite output left untracked is dirt the worker must not inherit."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def leaves_output(root: Path, st: Store) -> SuiteRun:
        (repo / "coverage.xml").write_text("<coverage/>\n", encoding="utf-8")
        return _green_suite(root, st)

    fake = FakeWorker()
    with fake as worker, pytest.raises(UserError, match=r"coverage\.xml"):
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2, run_suite=leaves_output)
    assert not fake.launches


def test_gated_writes_a_red_suite_without_a_drain_exit(tmp_path: Path, caobunga_home: Path) -> None:
    """The gate is `proceed-on-green`'s; `gated` records the verdict and stops anyway."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.GATED)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            run_suite=_not_green_suite("red"),
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.suite is not None
    assert result.suite.verdict.value == "red"
    assert not [line for line in lines if line.startswith("drain:")]


def test_gated_run_stamps_an_invocation_key(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-EPIC-12.7. The key is a `compact_utc` stamp minted at invocation start."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.invocation_id is not None
    assert re.fullmatch(r"\d{8}T\d{9}Z", result.invocation_id)
    # Read it back off disk rather than trusting the in-memory return.
    loaded = store.last_run("app")
    assert loaded is not None
    assert loaded.invocation_id == result.invocation_id


def test_one_invocation_groups_every_record_it_wrote(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-EPIC-12.6 F3: a drain's records must be groupable without timestamp arithmetic."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    closes = iter(
        [
            lambda: _close_worker(repo, "CBN-011", CLOSED_PLAN),
            lambda: _close_worker(repo, "CBN-012", TWO_CLOSED),
        ]
    )
    with _closing_client(fake, lambda _msg: next(closes)()) as worker:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    runs = store.list_runs("app")
    assert [run.task_id for run in runs] == ["CBN-011", "CBN-012"]
    keys = {run.invocation_id for run in runs}
    assert len(keys) == 1
    assert None not in keys


def test_separate_invocations_get_separate_keys(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        first = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    fake_two = FakeWorker()
    with _closing_client(
        fake_two, lambda _msg: _close_worker(repo, "CBN-012", TWO_CLOSED)
    ) as worker:
        second = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert first is not None
    assert second is not None
    assert first.invocation_id != second.invocation_id


def test_execute_run_locks_on_record_id(tmp_path: Path, caobunga_home: Path) -> None:
    """The mutex is taken with the registry's own validated `record` — its id
    (CBN-264) and its repo's git toplevel (CBN-EPIC-313.2, CBN-EPIC-313.6) — not
    the raw caller-supplied `project_id`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    locked_with: list[tuple[str, str | Path]] = []
    original_run_lock = Store.run_lock

    def spy_run_lock(self: Store, project_id: str, repo_path: str | Path):
        locked_with.append((project_id, repo_path))
        return original_run_lock(self, project_id, repo_path)

    store.run_lock = spy_run_lock.__get__(store, Store)  # type: ignore[method-assign]
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        record = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert record is not None
    assert locked_with == [(record.project, repo.resolve())]


def test_concurrent_run_on_same_slug_refuses(tmp_path: Path, caobunga_home: Path) -> None:
    """A held run lock refuses a second run: exit 2, no record, no launch (CBN-EPIC-16.2)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with store.run_lock("app", repo):
        with pytest.raises(UserError, match="already active for app") as exc:
            _run("app", store=store, worker=fake, poll_interval=0)
        assert exc.value.exit_code == 2
    assert store.last_run("app") is None
    assert fake.created is None


def test_concurrent_run_on_same_repo_under_another_id_refuses(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Two ids registered at one repo exclude each other (CBN-EPIC-313.2)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    _register(store, repo, slug="twin")
    fake = FakeWorker()
    with store.run_lock("app", repo):
        with pytest.raises(UserError, match="already active for twin's repo") as exc:
            _run("twin", store=store, worker=fake, poll_interval=0)
        assert exc.value.exit_code == 2
    assert store.last_run("twin") is None
    assert fake.created is None


@pytest.mark.parametrize(("holder", "refused"), [("app", "inner"), ("inner", "app")])
def test_concurrent_run_at_nested_path_of_same_repo_refuses(
    tmp_path: Path, caobunga_home: Path, holder: str, refused: str
) -> None:
    """Ids at a checkout and its subdirectory share one lock, either way round (CBN-EPIC-313.6)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    nested = repo / "backend"
    (nested / ".flaitron").mkdir(parents=True)
    (nested / ".flaitron" / "PLAN.md").write_text(OPEN_PLAN, encoding="utf-8")
    store = Store(caobunga_home)
    _register(store, repo)
    _register(store, nested, slug="inner")
    held = store.get(holder)
    fake = FakeWorker()
    with store.run_lock(holder, main_worktree(Path(held.path)) or held.path):
        with pytest.raises(UserError, match=f"already active for {refused}'s repo") as exc:
            _run(refused, store=store, worker=fake, poll_interval=0)
        assert exc.value.exit_code == 2
    assert store.last_run(refused) is None
    assert fake.created is None


@pytest.mark.parametrize(("holder", "refused"), [("app", "linked"), ("linked", "app")])
def test_concurrent_run_in_a_linked_worktree_of_same_repo_refuses(
    tmp_path: Path, caobunga_home: Path, holder: str, refused: str
) -> None:
    """A linked worktree shares its main checkout's lock, either way round (CBN-EPIC-450.2).

    Reverses CBN-EPIC-313.6, which gave each linked worktree its own: a branch
    run in one lands into the main checkout, so the two must not run at once.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "side", str(linked))
    store = Store(caobunga_home)
    _register(store, repo)
    _register(store, linked, slug="linked")
    held = store.get(holder)
    fake = FakeWorker()
    with store.run_lock(holder, main_worktree(Path(held.path)) or held.path):
        with pytest.raises(UserError, match=f"already active for {refused}'s repo") as exc:
            _run(refused, store=store, worker=fake, poll_interval=0)
        assert exc.value.exit_code == 2
    assert store.last_run(refused) is None
    assert fake.created is None


def test_run_lock_key_for_an_ordinary_checkout_is_unchanged(tmp_path: Path) -> None:
    """The family key is the toplevel CBN-EPIC-313.6 hashed, so live lock files still match."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    nested = repo / "backend"
    nested.mkdir()
    store = Store(tmp_path / "home")
    for member in (repo, nested):
        found = main_worktree(member)
        assert found is not None
        assert store.run_lock_path(found) == store.run_lock_path(repo)
    outside = tmp_path / "plain"
    outside.mkdir()
    assert main_worktree(outside) is None


def test_observe_refused_while_run_active(tmp_path: Path, caobunga_home: Path) -> None:
    """The mutex covers every tier — observe included, by design; `status` stays the peek."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.OBSERVE)
    with store.run_lock("app", repo):
        with pytest.raises(UserError, match="already active for app"):
            _run("app", store=store, echo=lambda _line: None)


# --- Task-level unattended eligibility (CBN-EPIC-17.2) ---

UNMARKED_PLAN = """# sample

## High

- [ ] **CBN-011** [medium] | flaitron-reader — reader work.

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

(none)
"""

MIXED_PLAN = """# sample

## High

- [ ] **CBN-011** [medium] [unattended] | flaitron-reader — reader work.
- [ ] **CBN-012** [medium] | cao-client — needs an operator.

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

(none)
"""

MIXED_AFTER = """# sample

## High

- [ ] **CBN-012** [medium] | cao-client — needs an operator.

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

- [x] **CBN-011** [medium] [unattended] | flaitron-reader — Completed 2026-08-19.
"""


def test_unattended_tier_denies_unmarked_at_start(tmp_path: Path, caobunga_home: Path) -> None:
    """A drain over an unmarked PLAN refuses to start and names every denial."""
    repo = _git_repo(tmp_path, UNMARKED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker:
        with pytest.raises(NoTaskError) as exc:
            execute_run("app", store=store, worker=worker, poll_interval=0, echo=lines.append)
    assert exc.value.exit_code == 3
    assert "1 task(s) denied under headless dispatch: CBN-011 (unmarked)" in str(exc.value)
    assert fake.created is None
    assert store.last_run("app") is None
    assert "eligibility: CBN-011 deny: unmarked — no [unattended] marker on the PLAN line" in lines


def test_drain_stops_at_unmarked_task_and_records_the_allow(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Mixed queue: the marked task drains, the unmarked one ends the drain legibly."""
    repo = _git_repo(tmp_path, MIXED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo, "CBN-011", MIXED_AFTER)) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.task_id == "CBN-011"
    assert result.eligibility_reason == "marked — [unattended] on the PLAN line"
    assert _session_count(fake) == 1
    assert [run.task_id for run in store.list_runs("app")] == ["CBN-011"]
    assert "eligibility: CBN-011 allow: marked — [unattended] on the PLAN line" in lines
    assert "eligibility: CBN-012 deny: unmarked — no [unattended] marker on the PLAN line" in lines
    # CBN-117: the record cannot claim a `next` this same drain just denied —
    # `status_card.py` renders it into the last-run status card.
    assert result.next_task is None


def test_gated_omits_the_next_its_own_posture_would_deny(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-117's honesty rule reaches `gated` for free once CBN-118 lands.

    `status_card.py` renders `next_task` into the last-run status card, so a
    `gated` run that closed `CBN-011` must not publish `CBN-012` —
    the same `gated` run's next pick refuses it as unmarked. Before CBN-118 this
    field named it, because `gated` was the one launching tier that read
    attended.
    """
    repo = _git_repo(tmp_path, MIXED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo, "CBN-011", MIXED_AFTER)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.next_task is None


def test_gated_requires_the_marker(tmp_path: Path, caobunga_home: Path) -> None:
    """`gated` consults the marker: every launching tier dispatches headless.

    CBN-118 overturns CBN-EPIC-17.2's "the operator is present by definition" —
    `worker_message` sends `--unattended` / `--fast` at every tier, and `gated`'s
    stop lands after the worker's commit. Nothing launches, nothing is recorded,
    and the refusal names the row and the reason.
    """
    repo = _git_repo(tmp_path, UNMARKED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker:
        with pytest.raises(NoTaskError) as exc:
            execute_run("app", store=store, worker=worker, poll_interval=0, echo=lines.append)
    assert exc.value.exit_code == 3
    assert "1 task(s) denied under headless dispatch: CBN-011 (unmarked)" in str(exc.value)
    assert fake.created is None
    assert store.last_run("app") is None
    assert "eligibility: CBN-011 deny: unmarked — no [unattended] marker on the PLAN line" in lines


def test_gated_dispatches_a_marked_row_and_records_the_allow(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """The other half of CBN-118: a seeded repo runs at `gated` exactly as before.

    The refusal above is the whole cost of the change; a marked row is untouched,
    and its record carries the marked reason rather than the attended one.
    """
    repo = _git_repo(tmp_path, MIXED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    with _closing_client(fake, lambda _msg: _close_worker(repo, "CBN-011", MIXED_AFTER)) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.task_id == "CBN-011"
    assert result.eligibility_reason == "marked — [unattended] on the PLAN line"
    assert fake.messages == [
        f"/ft-task CBN-011 --unattended {PINNED} {FOREGROUND_ONLY} {PARK_CHECK} {FILED}"
    ]
    assert "eligibility: CBN-011 allow: marked — [unattended] on the PLAN line" in lines


SECOND_CLOSED = """# sample

## High

- [ ] **CBN-011** [medium] [unattended] | flaitron-reader — reader work.

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

- [x] **CBN-012** [medium] [unattended] | cao-client — Completed 2026-08-19.
"""


def test_gated_keeps_the_in_progress_override(tmp_path: Path, caobunga_home: Path) -> None:
    """The CBN-118 split at the tier level: `gated` is headless but watched.

    A drain drops the override so it cannot race the operator in their own repo
    (CBN-EPIC-17.3); `gated` is a deliberate one-shot on one named repo, so it
    still continues the note the operator left open — the CBN-109 resume path.
    `CBN-012` is not the spec-selection head here, so only the override can
    reach it.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN, {"CBN-012": _note("in-progress")})
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(
        fake, lambda _msg: _close_worker(repo, "CBN-012", SECOND_CLOSED)
    ) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.task_id == "CBN-012"
    assert len(fake.messages) == 1
    assert fake.messages[0].startswith("Continue CBN-012.")


def test_drain_drops_the_override_the_gated_run_above_honors(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Same fixture, drain tier: the contrast that proves the two sets differ."""
    repo = _git_repo(tmp_path, OPEN_PLAN, {"CBN-012": _note("in-progress")})
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    with _closing_client(
        fake, lambda _msg: _close_worker(repo, "CBN-011", SECOND_CLOSED)
    ) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.task_id == "CBN-011"
    assert (
        fake.messages[0]
        == f"/ft-task CBN-011 --unattended {PINNED} {FOREGROUND_ONLY} {PARK_CHECK} {FILED}"
    )


def test_observe_reports_per_task_eligibility_without_launching(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, MIXED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.OBSERVE)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker:
        result = execute_run("app", store=store, worker=worker, poll_interval=0, echo=lines.append)
    assert result is None
    assert fake.created is None
    assert "eligibility: CBN-011 allow: marked — [unattended] on the PLAN line" in lines
    assert "eligibility: CBN-012 deny: unmarked — no [unattended] marker on the PLAN line" in lines


def test_dirty_target_repo_refuses_to_dispatch(tmp_path: Path, caobunga_home: Path) -> None:
    """The operator owns the repo they are standing in (CBN-EPIC-17.3).

    Exit 2 alongside the run mutex — the task is eligible, the repo is not
    ready — and, like the mutex, nothing is written and no worker launches.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    _dirty(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert exc.value.exit_code == 2
    assert "target repo has 1 uncommitted change(s)" in str(exc.value)
    assert "scratch.txt" in str(exc.value)
    assert fake.created is None
    assert store.last_run("app") is None


def test_dirty_refusal_withdraws_the_allow_it_followed(tmp_path: Path, caobunga_home: Path) -> None:
    """The pick's `allow` echo precedes the dirt read; the refusal retracts it (CBN-288)."""
    repo = _git_repo(tmp_path, MIXED_PLAN)
    _dirty(repo)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    lines: list[str] = []
    with FakeWorker() as worker:
        with pytest.raises(UserError):
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append)
    allow = "eligibility: CBN-011 allow: marked — [unattended] on the PLAN line"
    withdrawn = "eligibility: CBN-011 allow withdrawn: dirty-tree — not launching"
    assert lines.index(withdrawn) > lines.index(allow)


def test_dirty_read_git_hang_refuses_unlaunchable(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-317: a hung `git` on the dirt read is `GitUnreadable` (exit 5), not dirt."""
    repo = _git_repo(tmp_path, MIXED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)

    def hung(root: Path, **_: object) -> list[str] | None:
        raise GitUnreadable(root)

    monkeypatch.setattr("caobunga.presence.git_porcelain", hung)
    lines: list[str] = []
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(GitUnreadable, match="git unreadable") as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append)
    assert exc.value.exit_code == 5
    assert fake.created is None
    assert store.last_run("app") is None
    withdrawn = "eligibility: CBN-011 allow withdrawn: git-unreadable — not launching"
    assert lines.index(withdrawn) > lines.index(
        "eligibility: CBN-011 allow: marked — [unattended] on the PLAN line"
    )
    with FakeWorker() as worker:
        receipt = loop.execute_invocation("app", store=store, worker=worker)
    assert receipt.stop_reason == "git-unreadable"
    assert receipt.legacy_exit_code == 5


def test_git_unreadable_receipt_names_recovery_and_skips_snapshot_reads(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-320: the refusal gets its own recovery line, and the receipt's snapshot
    does not wait on the same hung `git` twice more."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def unreadable(root: Path) -> str | None:
        raise GitUnreadable(root)

    snapshot_reads: list[str] = []
    monkeypatch.setattr("caobunga.loop.git_head", unreadable)
    monkeypatch.setattr("caobunga.invocation.git_head", lambda _root: snapshot_reads.append("head"))
    monkeypatch.setattr("caobunga.invocation.git_dirt", lambda _root: snapshot_reads.append("dirt"))
    with FakeWorker() as worker:
        receipt = loop.execute_invocation("app", store=store, worker=worker)
    assert receipt.stop_reason == "git-unreadable"
    assert snapshot_reads == []
    assert receipt.head_sha is None
    assert receipt.tree is None
    assert any("no hung git process" in step for step in receipt.recovery)
    assert not any("configuration/readiness" in step for step in receipt.recovery)


@pytest.mark.parametrize("unborn", [True, False], ids=["unborn-head", "plain-dir"])
def test_no_head_receipt_stops_not_a_repo(
    tmp_path: Path, caobunga_home: Path, unborn: bool
) -> None:
    """CBN-321: git answering "no HEAD" is its own stop, not `git-unreadable`."""
    repo = tmp_path / "repo"
    (repo / ".flaitron" / "tasknote").mkdir(parents=True)
    (repo / ".flaitron" / "PLAN.md").write_text(OPEN_PLAN, encoding="utf-8")
    if unborn:
        _git(repo, "init")
    store = Store(caobunga_home)
    _register(store, repo)
    with FakeWorker() as worker:
        receipt = loop.execute_invocation("app", store=store, worker=worker)
    assert receipt.stop_reason == "not-a-repo"
    assert receipt.legacy_exit_code == 2
    # The snapshot still reads the tree; only a repo yields one.
    assert (receipt.tree is not None) is unborn
    assert any("at least one commit" in step for step in receipt.recovery)
    assert not any("hung git process" in step for step in receipt.recovery)


def test_head_lost_after_launch_receipt_stops_head_lost(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-322: HEAD vanishing after launch is not `worker-start`'s provider failure."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def lose_head(_msg: str) -> None:
        (repo / ".git").rename(tmp_path / "gone.git")

    fake.on_send = lose_head
    with fake as worker:
        receipt = loop.execute_invocation(
            "app",
            store=store,
            worker=worker,
            usage=_usage(),
            poll_interval=0,
            timeout=2,
            run_suite=_green_suite,
        )
    assert receipt.stop_reason == "head-lost", receipt.detail
    assert receipt.legacy_exit_code == 2
    assert any("partial work" in step for step in receipt.recovery)
    assert not any("CLI installation" in step for step in receipt.recovery)
    assert not any("registry changed" in step for step in receipt.recovery)


@pytest.mark.parametrize("when", ["send", "shutdown"])
def test_registry_change_on_head_lost_names_files_on_receipt(
    tmp_path: Path, caobunga_home: Path, when: str
) -> None:
    """CBN-428: a raise after launch writes no task record, so the receipt names the
    change, including a write the worker lands while it is being shut down."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def retier() -> None:
        store.config_file().write_text("providers: [codex]\n", encoding="utf-8")

    class LateWriter(FakeWorker):
        def shutdown(self, session: Session) -> None:
            if when == "shutdown":
                retier()
            super().shutdown(session)

    fake = LateWriter()

    def lose_head(_msg: str) -> None:
        if when == "send":
            retier()
        (repo / ".git").rename(tmp_path / "gone.git")

    fake.on_send = lose_head
    with fake as worker:
        receipt = loop.execute_invocation(
            "app",
            store=store,
            worker=worker,
            usage=_usage(),
            poll_interval=0,
            timeout=2,
            run_suite=_green_suite,
        )
    assert receipt.stop_reason == "head-lost", receipt.detail
    assert receipt.legacy_exit_code == 2
    assert store.last_run("app") is None
    assert any(
        "registry changed during the run and no task record carries it: config.yaml." in step
        for step in receipt.recovery
    )


def test_registry_change_on_interrupt_names_files_on_receipt(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-428: an interrupt mid-run is an exception path too; the receipt stays `interrupted`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def retier_and_interrupt(_msg: str) -> None:
        store.project_file("app").write_text(
            store.project_file("app").read_text(encoding="utf-8") + "# retiered\n",
            encoding="utf-8",
        )
        raise KeyboardInterrupt

    fake.on_send = retier_and_interrupt
    with fake as worker:
        receipt = loop.execute_invocation(
            "app",
            store=store,
            worker=worker,
            usage=_usage(),
            poll_interval=0,
            timeout=2,
            run_suite=_green_suite,
        )
    assert receipt.stop_reason == "interrupted"
    assert receipt.legacy_exit_code == 130
    assert any("no task record carries it: projects/app.yaml." in step for step in receipt.recovery)


def test_dirty_read_nonzero_status_still_refuses_exit_2(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-317: git *answering* no (nonzero exit) keeps `cannot read git status`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    monkeypatch.setattr("caobunga.presence.git_porcelain", lambda _root, **_: None)
    with FakeWorker() as worker:
        with pytest.raises(UserError, match="cannot read git status") as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert exc.value.exit_code == 2
    assert store.last_run("app") is None


@pytest.mark.parametrize("tier", [ExecutionTier.GATED, ExecutionTier.PROCEED_ON_GREEN])
def test_dirty_refusal_covers_every_launching_tier(
    tmp_path: Path, caobunga_home: Path, tier: ExecutionTier
) -> None:
    """`gated` is not exempt: Caobunga always launches a *headless* worker, so an
    operator waiting at the orchestrator terminal is still nobody inside the target."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    _dirty(repo)
    store = Store(caobunga_home)
    _register(store, repo, tier=tier)
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert exc.value.exit_code == 2
    assert fake.created is None


def test_observe_ignores_a_dirty_tree(tmp_path: Path, caobunga_home: Path) -> None:
    """Unaffected rather than exempted — observe never dispatches."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    _dirty(repo)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.OBSERVE)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker:
        result = execute_run("app", store=store, worker=worker, poll_interval=0, echo=lines.append)
    assert result is None
    assert "next: CBN-011" in lines


def test_candidate_tasknote_alone_is_not_foreign_dirt(tmp_path: Path, caobunga_home: Path) -> None:
    """The CBN-109 resume path survives the gate.

    An active tasknote is uncommitted for its whole run, so a stranded
    `in-progress` note always makes the tree dirty. Refusing on it would refuse
    every resume — the one path this gate is the *only* stop on, since the
    continue prompt tells the worker not to re-invoke `/ft-task` and so never
    reaches flaitron's own foreign-dirt gate.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-011.md").write_text(
        _note("in-progress"), encoding="utf-8"
    )
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    sent: list[str] = []
    with _closing_client(fake, sent.append) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=0)
    # The dispatch happened — this run only fails later, on the zero timeout.
    assert sent
    assert sent[0].startswith("Continue CBN-011.")
    assert result is not None
    assert result.escalation_reason is Escalation.TIMEOUT


def test_other_dirt_still_refuses_a_resume(tmp_path: Path, caobunga_home: Path) -> None:
    """Only the candidate's own tasknote is exempt; a half-edited source tree is not."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-011.md").write_text(
        _note("in-progress"), encoding="utf-8"
    )
    _dirty(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert exc.value.exit_code == 2
    assert "scratch.txt" in str(exc.value)
    assert "CBN-011.md" not in str(exc.value)
    assert fake.created is None


def test_a_filing_only_tree_is_named_as_a_filing_with_a_commit_to_run(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Exit 2 keeps its meaning and only gains detail (CBN-138.4).

    Since CBN-138.2 the prompt asks a worker to file what it finds, so the dirt
    this refuses on is now routinely something Caobunga asked for — and it read
    exactly like an operator's half-edited tree. The refusal names the filing and
    offers the commit; it still refuses, and still writes nothing.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / ".flaitron" / "PLAN.md").write_text(
        OPEN_PLAN + "\n- [ ] **CBN-099** [medium] | worker-filed — filed mid-run.\n",
        encoding="utf-8",
    )
    store = Store(caobunga_home)
    _register(store, repo)
    store.write_run(_filed_run("CBN-099"))
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    message = str(exc.value)
    assert exc.value.exit_code == 2
    assert "flaitron filing artifacts a dispatched worker writes" in message
    # The blocking path on both `add` and `commit`, never the directory (CBN-435).
    assert f"git -C {repo} add -- .flaitron/PLAN.md && " in message
    assert '"chore: commit worker filing" -- .flaitron/PLAN.md — then re-run' in message
    assert "add .flaitron" not in message
    # Named, not exempted: the count and the porcelain line are still reported.
    assert "1 uncommitted change(s)" in message
    assert ".flaitron/PLAN.md" in message
    # Printed, never run — the tree is exactly as dirty as before (§Fleet boundary).
    assert fake.created is None
    # No record of its own: the last run is still the one that filed the row.
    last = store.last_run("app")
    assert last is not None and last.summary == "parked after filing"
    assert git_dirt(repo) != []


def _filed_run(*filed: str) -> RunRecord:
    """A parked last run that filed `filed` as `new_rows`."""
    return RunRecord(
        project="app",
        task_id="CBN-010",
        outcome=Outcome.BLOCKED,
        closed_at=utc_now(),
        summary="parked after filing",
        new_rows=[NewRow(task_id=row) for row in filed],
    )


def test_the_offered_commit_never_names_the_exempt_candidate_note(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """The command scopes to the blocking paths on `add` and `commit` (CBN-435).

    `git add .flaitron` also staged the candidate's own exempt note. Here that
    note is already staged, so a bare `commit` would have carried it too; the
    path-scoped `commit -- <paths>` leaves it out.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
    note.write_text("---\nstatus: in-progress\n---\n", encoding="utf-8")
    _git(repo, "add", str(note))
    (repo / ".flaitron" / "PLAN.md").write_text(
        OPEN_PLAN + "\n- [ ] **CBN-099** [medium] | worker-filed — filed mid-run.\n",
        encoding="utf-8",
    )
    store = Store(caobunga_home)
    _register(store, repo)
    store.write_run(_filed_run("CBN-099"))
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    message = str(exc.value)
    assert exc.value.exit_code == 2
    assert "1 uncommitted change(s)" in message
    assert '-m "chore: commit worker filing" -- .flaitron/PLAN.md — then re-run' in message
    assert "CBN-011.md" not in message
    assert fake.created is None


def test_a_plan_edit_with_no_filing_run_gets_the_generic_refusal(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """An operator's PLAN edit is not a worker's filing (CBN-435).

    With no run record that filed rows, path shape alone no longer earns the
    commit offer; the edit still blocks and is still named.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / ".flaitron" / "PLAN.md").write_text(
        OPEN_PLAN + "\n- [ ] **CBN-099** [medium] | hand-edit — operator's row.\n",
        encoding="utf-8",
    )
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    message = str(exc.value)
    assert exc.value.exit_code == 2
    assert "filing artifacts" not in message
    assert "git -C" not in message
    assert ".flaitron/PLAN.md" in message
    assert fake.created is None


def test_a_live_sessions_tasknote_gets_the_generic_refusal(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """No commit offer for a note no run dispatched or filed (CBN-434, CBN-429 F1).

    A fleet repo's refusal offered `git add .flaitron && git commit` over a live
    session's in-flight `DEPLOY-061.md`. With no run record owning that id, the
    refusal is the generic one, and the note is still named and still blocks.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / ".flaitron" / "tasknote" / "DEPLOY-061.md").write_text(
        "---\nstatus: in-progress\n---\n", encoding="utf-8"
    )
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    message = str(exc.value)
    assert exc.value.exit_code == 2
    assert "filing artifacts" not in message
    assert "git -C" not in message
    assert "commit, stash, or discard them" in message
    assert ".flaitron/tasknote/DEPLOY-061.md" in message
    assert fake.created is None


def test_a_stub_the_last_run_filed_still_gets_the_commit_offer(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Ownership comes from the last record's `new_rows` (CBN-434)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    stubs = repo / ".flaitron" / "sidequest"
    stubs.mkdir(parents=True)
    (stubs / "CBN-099.md").write_text("---\nstatus: starter\n---\n", encoding="utf-8")
    store = Store(caobunga_home)
    _register(store, repo)
    store.write_run(_filed_run("CBN-099"))
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    message = str(exc.value)
    assert exc.value.exit_code == 2
    assert "flaitron filing artifacts a dispatched worker writes" in message
    assert f"git -C {repo} add -- .flaitron/sidequest/CBN-099.md && " in message
    assert fake.created is None


def test_mixed_dirt_keeps_the_generic_refusal_verbatim(tmp_path: Path, caobunga_home: Path) -> None:
    """The affix must not leak onto a tree the predicate did not clear.

    CBN-119.2's discipline for enriching a refusal: assert the whole message when
    the condition is absent, so an accidental unconditional affix fails here
    rather than in a live run. A worker's filing beside an operator's edit is not
    a filing to commit, and telling the operator otherwise would be advice to
    stage their own unreviewed work.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / ".flaitron" / "PLAN.md").write_text(
        OPEN_PLAN + "\n- [ ] **CBN-099** [medium] | worker-filed — filed mid-run.\n",
        encoding="utf-8",
    )
    _dirty(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    message = str(exc.value)
    assert exc.value.exit_code == 2
    assert message.startswith(
        "app: target repo has 2 uncommitted change(s); "
        "commit, stash, or discard them, then re-run: "
    )
    assert "filing" not in message
    assert "git -C" not in message


def test_parked_head_in_a_dirty_repo_refuses_the_next_row_on_dirt(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-252: the park's leftover edits still hold the repo; the refusal names the resume.

    A fleet repo's `SCI-102` shape: the pick skips the parked row, the next row
    meets the park's uncommitted work, and the dirt gate refuses it (exit 2) —
    with no record and no review, where it used to re-record the park.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN, {"CBN-011": _note("blocked", "dependency — upstream")})
    _dirty(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker, pytest.raises(UserError) as exc:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append)
    assert exc.value.exit_code == 2
    assert any(line.startswith("eligibility: CBN-011 deny: parked") for line in lines)
    assert fake.created is None
    assert store.last_run("app") is None


def test_dirty_refusal_lists_every_blocking_line(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-287: the recovery step says "inspect the listed changes", so list them all."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    names = [f"scratch-{n}.txt" for n in range(8)]
    for name in names:
        _dirty(repo, name)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker, pytest.raises(UserError) as exc:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    message = str(exc.value)
    assert "8 uncommitted change(s)" in message
    assert all(name in message for name in names)
    assert "more)" not in message


def test_dirty_refusal_names_the_parked_tasknote_and_its_resume(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-287: dirt that is a parked run's open note points at `/ft-task <ID>`."""
    repo = _git_repo(tmp_path, OPEN_PLAN, {"CBN-011": _note("blocked", "dependency — upstream")})
    note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
    note.write_text(note.read_text(encoding="utf-8") + "\npartial work\n", encoding="utf-8")
    _dirty(repo)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker, pytest.raises(UserError) as exc:
        _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    message = str(exc.value)
    assert exc.value.exit_code == 2
    assert "open tasknote CBN-011 holds partial work" in message
    assert "/ft-task CBN-011" in message
    assert message.index("scratch.txt") < message.index("open tasknote CBN-011")


def test_leftover_status_snippet_refuses_like_any_dirt(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-188.3: the CBN-154 exemption is retired with the write that earned it."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / "caobunga-status.md").write_text("# stale card\n", encoding="utf-8")
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert exc.value.exit_code == 2
    assert "caobunga-status.md" in str(exc.value)
    assert fake.created is None
    assert store.last_run("app") is None


def test_drain_survives_its_own_per_task_dirty_gate(tmp_path: Path, caobunga_home: Path) -> None:
    """A per-dispatch check is safe because Caobunga's own writes never dirty the tree.

    The worker's closure commit is atomic, and `status_card.publish_status` writes
    the last-run card to `$CAOBUNGA_HOME` rather than the target repo
    (CBN-188.2), so the tree is clean again at the next pick and a drain is
    not cut short by its own output.
    """
    repo = _git_repo(tmp_path, MIXED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo, "CBN-011", MIXED_AFTER)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.task_id == "CBN-011"


# --- CBN-119.2: unparsed rows are visible on the dispatch path, and never refuse -------------

UNPARSED_ONLY_PLAN = """# sample

## High

- [ ] **CBN-011** [medium] [!unattended] | flaitron-reader — bang-shape; row is dropped.

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

(none)
"""

UNPARSED_MIXED_PLAN = """# sample

## High

- [ ] **CBN-011** [medium] [unattended] | flaitron-reader — reader work.
- [ ] **CBN-012** [medium] [!unattended] | cao-client — bang-shape; row is dropped.

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

(none)
"""

UNPARSED_MIXED_AFTER = """# sample

## High

- [ ] **CBN-012** [medium] [!unattended] | cao-client — bang-shape; row is dropped.

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

- [x] **CBN-011** [medium] [unattended] | flaitron-reader — Completed 2026-08-30.
"""


def _line_of(plan: str, needle: str) -> int:
    """1-based PLAN line number of the row containing `needle`."""
    return next(n for n, line in enumerate(plan.splitlines(), start=1) if needle in line)


def test_drain_echoes_unparsed_rows_without_refusing(tmp_path: Path, caobunga_home: Path) -> None:
    """The verdict of CBN-119.2, both halves at once.

    A row the grammar dropped is named on the dispatch path — and the marked
    row beside it still dispatches. A dropped row causes under-dispatch, never
    an unsafe one, so it must not wedge a repo whose other rows are seeded.
    """
    repo = _git_repo(tmp_path, UNPARSED_MIXED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    close = lambda _msg: _close_worker(repo, "CBN-011", UNPARSED_MIXED_AFTER)  # noqa: E731
    with _closing_client(fake, close) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.task_id == "CBN-011"
    dropped = _line_of(UNPARSED_MIXED_PLAN, "**CBN-012**")
    assert f"unparsed: line {dropped} — - [ ] **CBN-012** [medium] [!unattended]" in "\n".join(
        lines
    )


def test_exit_three_names_the_unparsed_row_the_operator_just_botched(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """A dropped row misleads most at "nothing to run" (CBN-119.2).

    The only row in the queue was seeded into the bang-shape and vanished, so
    there is no denial to report either. Before this the message was the bare
    `no next task for app` and the operator's seed left no trace at all.
    """
    repo = _git_repo(tmp_path, UNPARSED_ONLY_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker:
        with pytest.raises(NoTaskError) as exc:
            execute_run("app", store=store, worker=worker, poll_interval=0, echo=lines.append)
    assert exc.value.exit_code == 3
    dropped = _line_of(UNPARSED_ONLY_PLAN, "**CBN-011**")
    assert f"1 unparsed row(s) — see line {dropped}" in str(exc.value)
    assert f"unparsed: line {dropped} — " in "\n".join(lines)
    assert fake.created is None


def test_unparsed_clause_and_echo_are_absent_on_a_clean_plan(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Zero unparsed rows leave both surfaces byte-identical to before CBN-119.2."""
    repo = _git_repo(tmp_path, UNMARKED_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    fake = FakeWorker()
    lines: list[str] = []
    with fake as worker:
        with pytest.raises(NoTaskError) as exc:
            execute_run("app", store=store, worker=worker, poll_interval=0, echo=lines.append)
    assert str(exc.value) == (
        "no next task for app; 1 task(s) denied under headless dispatch: CBN-011 (unmarked)"
    )
    assert not any(line.startswith("unparsed:") for line in lines)


# --- loop.py / prompt.py / wait.py / report.py import-surface pin + watchdog
# --- characterization (CBN-EPIC-232.2, extended at .3, .5 and .6) ---
#
# `loop.py` is a composer, not a façade: nothing declares `__all__`, and the
# surface that matters is whatever its importers reach in for. The
# `CBN-EPIC-232` split moves most of that surface out to `prompt.py` / `wait.py`
# / `report.py` / the flaitron reader, so these tests freeze it first; each
# extract child edits the pin deliberately as names move. `.3` moved the
# prompt constants + `worker_message` to `prompt.py`, `.5` moved the close
# wait to `wait.py`, and `.6` moved the report surfaces to `review.py` (renamed
# `report.py` at CBN-EPIC-244.7), so their surfaces now pin against those
# modules instead. CBN-EPIC-363.3 moved the pick → launch → wait re-pick to
# `launch.py` (`LAUNCH_SURFACE`), CBN-EPIC-363.4 the post-close tail to
# `finish.py` (`FINISH_SURFACE`), and CBN-EPIC-363.5 the echo and exit-detail
# text to `run_echo.py` (`RUN_ECHO_SURFACE`).
# Narrower than the `test_flaitron.py` FACADE pin on purpose (CBN-143.2):
# pinning every public attribute here would make each move fight the guard
# for no signal.
#
# One consumer is not a name and so is not pinned: `test_contract.py`'s drain
# guard source-reads `loop.__file__` for every `drain:` reason the loop emits.
# A child that moves a `"drain"` call site out of `loop.py` must move that
# guard's read with it.
#
# CBN-EPIC-383.3 split this file's tests out to `test_launch.py` /
# `test_finish.py` / `test_park_escalate.py` / `test_receipts.py`, and its shared
# helpers to `conftest.py`. CBN-404 stopped keying the pins by hand: importers,
# patch sites, and attribute reach are discovered by AST scan over
# `caobunga/` + `tests/` + `main.py`, and each must match its pin exactly, so a file nobody
# remembered to key fails the pin instead of slipping past it.

#: Names each importer takes `from caobunga.loop`, exact in both directions,
#: keyed by path relative to `backend/`. The leaves (`launch` / `finish` /
#: `run_echo`) take nothing back from `loop` (CBN-EPIC-363.3); discovery fails
#: if one starts to `from`-import it.
LOOP_SURFACE = {
    "caobunga/cli.py": frozenset({"execute_run"}),
    "tests/conftest.py": frozenset({"execute_run"}),
    "tests/test_finish.py": frozenset({"execute_run"}),
    "tests/test_fleet.py": frozenset({"execute_run"}),
    "tests/test_loop.py": frozenset({"execute_run"}),
}

#: Names each importer takes `from caobunga.prompt`, exact in both directions,
#: keyed by path relative to `backend/` (CBN-EPIC-232.3).
PROMPT_SURFACE = {
    "caobunga/launch.py": frozenset({"worker_message"}),
    "tests/test_contract.py": frozenset({"worker_message"}),
    "tests/test_loop.py": frozenset(
        {
            "FOLLOWUP_FILE",
            "FOREGROUND_ONLY",
            "PARK_CHECK",
            "PINNED_SKILL",
            "worker_message",
        }
    ),
    "tests/test_park_escalate.py": frozenset({"PARK_CHECK"}),
}

#: Names each importer takes `from caobunga.wait`, exact in both directions,
#: keyed by path relative to `backend/` (CBN-EPIC-232.5).
WAIT_SURFACE = {
    "caobunga/launch.py": frozenset({"is_usage_limit_line", "wait_for_close"}),
    "caobunga/finish.py": frozenset({"SuiteHeartbeat", "settle_worker"}),
    "caobunga/loop.py": frozenset({"DEFAULT_POLL", "SETTLE_GRACE", "SuiteHeartbeat"}),
    "tests/test_finish.py": frozenset({"SuiteHeartbeat"}),
    "tests/test_launch.py": frozenset(
        {"ACTIVITY_MAX", "SuiteHeartbeat", "format_elapsed", "settle_worker", "wait_for_close"}
    ),
    "tests/test_local.py": frozenset({"is_usage_limit_line"}),
}

#: Names each importer takes `from caobunga.launch`, exact in both directions,
#: keyed by path relative to `backend/` (CBN-EPIC-363.3).
LAUNCH_SURFACE = {
    "caobunga/finish.py": frozenset({"Posture", "RunDeps"}),
    "caobunga/loop.py": frozenset(
        {
            "Attempt",
            "RunDeps",
            "attribution",
            "launch_posture",
            "launch_until_close",
            "retire_session",
            "shutdown_session",
        }
    ),
    "tests/test_launch.py": frozenset({"_failed_from_worker"}),
}

#: Names each importer takes `from caobunga.finish`, exact in both directions,
#: keyed by path relative to `backend/` (CBN-EPIC-363.4).
FINISH_SURFACE = {
    "caobunga/loop.py": frozenset(
        {"finish", "measure_suite", "refresh_tree", "settle_and_measure"}
    ),
}

#: Names each importer takes `from caobunga.run_echo`, exact in both directions,
#: keyed by path relative to `backend/` (CBN-EPIC-363.5).
RUN_ECHO_SURFACE = {
    "caobunga/loop.py": frozenset(
        {
            "denial_codes",
            "echo_eligibility",
            "echo_observe",
            "echo_unparsed",
            "no_task_message",
            "non_green_verbs",
            "outcome_exit_detail",
            "run_tally",
            "suite_exit_detail",
            "unpinned_exit_detail",
        }
    ),
}

#: Names each importer takes `from caobunga.report`, exact in both directions,
#: keyed by path relative to `backend/` (CBN-EPIC-232.6). Narrowed to `review`
#: alone when CBN-258 retired the Hermes window, flush, and verdict recording;
#: renamed `review` → `report` at CBN-EPIC-244.7; its caller moved to `finish.py`
#: at CBN-EPIC-363.4.
REPORT_SURFACE = {
    "caobunga/finish.py": frozenset({"report"}),
    "tests/test_rollup.py": frozenset({"report"}),
}

#: Module attributes the run-loop test files reach through `monkeypatch.setattr`
#: rather than an import. A move must re-point the patch, not just the name.
#: `LOOP_PATCHED` emptied at `.6` when `send_hermes` left with the review;
#: `REPORT_PATCHED` emptied at CBN-258 when `send_hermes` was retired outright.
#: `settle_worker` added at CBN-EPIC-363.2 by the kwarg pass-through pin.
#: `LAUNCH_PATCHED` added at CBN-EPIC-363.3: the re-pick `git_head` read moved.
#: `FINISH_PATCHED` added at CBN-EPIC-363.4: `settle_worker` left `LOOP_PATCHED`
#: with the settle, and the queued-suite `run_fleet_suite` patch is pinned too.
#: `RUN_ECHO_PATCHED` added empty at CBN-EPIC-363.5: nothing patches the text.
#: CBN-404 added the patches the hand-kept sets had missed (three on `loop`, `launch.pick`).
#: CBN-436 added `launch.try_read_project`: the unreadable-PLAN failure snapshot.
LOOP_PATCHED = frozenset({"SuiteHeartbeat", "git_head", "read_project", "select_worker"})
PROMPT_PATCHED: frozenset[str] = frozenset()
WAIT_PATCHED = frozenset({"inspect_close", "_start_watch"})
REPORT_PATCHED: frozenset[str] = frozenset()
LAUNCH_PATCHED = frozenset({"git_head", "pick", "try_read_project"})
FINISH_PATCHED = frozenset({"SuiteHeartbeat", "run_fleet_suite", "settle_worker"})
RUN_ECHO_PATCHED: frozenset[str] = frozenset()

#: Attributes reached as `<module>.<name>` after `from caobunga import <module>`
#: (CBN-404). Dunders are left out: `loop.__file__` is the drain guard's source
#: read, covered above.
LOOP_REACHED = frozenset(
    {
        "ENV_TIMEOUT",
        "INTERRUPT_NOTICE",
        "_interrupt_guard",
        "_marked_ids",
        "execute_invocation",
        "execute_run",
    }
)
WAIT_REACHED = frozenset({"_start_watch"})
REPORT_REACHED: frozenset[str] = frozenset()
LAUNCH_REACHED: frozenset[str] = frozenset()
FINISH_REACHED = frozenset({"refresh_tree"})
RUN_ECHO_REACHED: frozenset[str] = frozenset()
PROMPT_REACHED: frozenset[str] = frozenset()

_BACKEND = Path(caobunga.__file__).parent.parent


@functools.cache
def _scanned() -> dict[str, ast.Module]:
    """Every `caobunga/` and `tests/` source under `backend/`, plus `main.py`, parsed."""
    paths = [_BACKEND / "main.py"] + [
        path for root in ("caobunga", "tests") for path in sorted((_BACKEND / root).rglob("*.py"))
    ]
    return {
        path.relative_to(_BACKEND).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in paths
    }


def _imports_from(node: ast.ImportFrom, importer: str, module: str) -> bool:
    """`node` is `from caobunga.<module> import …`, or `from .<module>` inside `caobunga/`."""
    if node.level == 0:
        return node.module == f"caobunga.{module}"
    return node.level == 1 and node.module == module and importer.startswith("caobunga/")


def _importers(module: str) -> dict[str, frozenset[str]]:
    """Names each scanned file takes `from caobunga.<module>`, per its AST."""
    found: dict[str, frozenset[str]] = {}
    for importer, tree in _scanned().items():
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and _imports_from(node, importer, module)
            for alias in node.names
        }
        if names:
            found[importer] = frozenset(names)
    return found


def _bound(tree: ast.Module, module: str) -> set[str]:
    """Local names bound to `caobunga.<module>` in `tree`.

    Both `from caobunga import <module> [as x]` and `import caobunga.<module> as x`.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "caobunga":
            bound |= {a.asname or a.name for a in node.names if a.name == module}
        elif isinstance(node, ast.Import):
            bound |= {a.asname for a in node.names if a.name == f"caobunga.{module}" and a.asname}
    return bound


#: Call names that patch a module attribute: `monkeypatch.setattr` / `delattr`,
#: builtin `setattr` / `delattr`, `mock.patch("…")`, and `patch.object(mod, "…")`.
_PATCHERS = frozenset({"setattr", "delattr", "patch", "object"})


def _patched(module: str) -> frozenset[str]:
    """Names any scanned file patches on `caobunga.<module>`, in either form.

    String form `setattr("caobunga.<module>.<name>", …)` and object form
    `setattr(<module>, "<name>", …)` both count.
    """
    found: set[str] = set()
    for tree in _scanned().values():
        bound = _bound(tree, module)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in _PATCHERS:
                continue
            target = node.args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                parts = target.value.split(".")
                if len(parts) == 3 and parts[:2] == ["caobunga", module]:
                    found.add(parts[2])
            elif (
                isinstance(target, ast.Name)
                and target.id in bound
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                found.add(node.args[1].value)
    return frozenset(found)


def _reached(module: str) -> frozenset[str]:
    """Non-dunder attributes any scanned file reads off a bound `<module>`.

    Pooled per module, not per file: a second file reaching an already-pinned
    name passes, since the pin guards the module's surface, not its callers.
    """
    found: set[str] = set()
    for tree in _scanned().values():
        bound = _bound(tree, module)
        found |= {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in bound
            and not node.attr.startswith("__")
        }
    return frozenset(found)


#: Each pinned module object with its import, patch, and reach pins.
PINS = {
    "loop": (loop, LOOP_SURFACE, LOOP_PATCHED, LOOP_REACHED),
    "prompt": (prompt, PROMPT_SURFACE, PROMPT_PATCHED, PROMPT_REACHED),
    "wait": (wait, WAIT_SURFACE, WAIT_PATCHED, WAIT_REACHED),
    "report": (report, REPORT_SURFACE, REPORT_PATCHED, REPORT_REACHED),
    "launch": (launch, LAUNCH_SURFACE, LAUNCH_PATCHED, LAUNCH_REACHED),
    "finish": (finish, FINISH_SURFACE, FINISH_PATCHED, FINISH_REACHED),
    "run_echo": (run_echo, RUN_ECHO_SURFACE, RUN_ECHO_PATCHED, RUN_ECHO_REACHED),
}


@pytest.mark.parametrize("module", sorted(PINS))
def test_loop_surface_every_importer_takes_exactly_its_pinned_names(module: str) -> None:
    """An importer editing its own list is the half a module-side pin misses."""
    assert _importers(module) == PINS[module][1]


@pytest.mark.parametrize("module", sorted(PINS))
def test_loop_surface_every_patch_and_reach_is_pinned(module: str) -> None:
    """A patch or `module.attr` reach nobody pinned fails here, not at a move."""
    _, _, patched, reached = PINS[module]
    assert (_patched(module), _reached(module)) == (patched, reached)


@pytest.mark.parametrize("module", sorted(PINS))
def test_loop_surface_every_pinned_name_resolves_on_the_module(module: str) -> None:
    target, surface, patched, reached = PINS[module]
    for name in sorted(set().union(*surface.values(), patched, reached)):
        assert hasattr(target, name), name


def test_drain_pin_skips_a_retained_row() -> None:
    """CBN-EPIC-450.5. A worker shares the repo's refs, so it could delete the branch."""
    row = "- [ ] **CBN-012** [medium] [unattended] | ok — plain."
    assert loop._marked_ids(_pin_read(row), frozenset({"CBN-012"})) == frozenset()
