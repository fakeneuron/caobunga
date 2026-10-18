"""Gated / observe run loop. In-memory worker fake; tmp git repos."""

from __future__ import annotations

import ast
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

import caobunga
from caobunga import finish, launch, loop, prompt, report, run_echo, wait
from caobunga.close import CloseSnapshot
from caobunga.errors import (
    GitUnreadable,
    NoTaskError,
    ReportError,
    Unlaunchable,
    UserError,
    WorkerError,
)
from caobunga.flaitron import (
    FT_TASK_SKILL_DIR_REL,
    SELF_FT_TASK_SKILL_DIR_REL,
    UNATTENDED_MODULE,
    NextTask,
    OpenTasknote,
    PlanPriority,
    ProjectRead,
    parse_plan,
    phase_progress,
    read_project,
)
from caobunga.git import git_dirt, git_head, git_toplevel
from caobunga.invocation import (
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
    _parked_descoped,
)
from caobunga.loop import execute_run
from caobunga.models import (
    Escalation,
    ExecutionTier,
    NewRow,
    Outcome,
    RegistryRecord,
    RunRecord,
    Suite,
    SuiteVerb,
    compact_utc,
    utc_now,
)
from caobunga.prompt import (
    FOLLOWUP_FILE,
    FOREGROUND_ONLY,
    PARK_CHECK,
    PINNED_SKILL,
    worker_message,
)
from caobunga.store import Store
from caobunga.suite import SuiteRun
from caobunga.taskmap import EFFORT, TaskClass
from caobunga.usage import Leftover, ProviderUsage
from caobunga.wait import settle_worker, wait_for_close
from caobunga.worker import (
    STATUS_ERROR,
    STATUS_EXITED,
    STATUS_PROCESSING,
    Session,
    resolve_cwd,
    safe_session_name,
)

# The pinned-skill cue (CBN-291) as each layout renders it: a seeded
# `.flaitron/core` pin, or a core-less flaitron-self tree. A fixture with
# neither on disk gets no cue at all.
PINNED = PINNED_SKILL.format(skill=(FT_TASK_SKILL_DIR_REL / "SKILL.md").as_posix())
SELF_PINNED = PINNED_SKILL.format(skill=(SELF_FT_TASK_SKILL_DIR_REL / "SKILL.md").as_posix())
FILED = FOLLOWUP_FILE

OPEN_PLAN = """# sample

**Suggested next task:** **CBN-011** — reader.

## High

- [ ] **CBN-011** [medium] [unattended] | flaitron-reader — reader work.
- [ ] **CBN-012** [medium] [unattended] | cao-client — client work.

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

(none)
"""

CLOSED_PLAN = """# sample

**Suggested next task:** **CBN-012** — client.

## High

- [ ] **CBN-012** [medium] [unattended] | cao-client — client work.

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

- [x] **CBN-011** [medium] | flaitron-reader — Completed 2026-08-19.
"""

EXHAUSTED_PLAN = """# sample

## High

(none)

## Medium

(none)

## Low

(none)

## Future Opportunities

- [ ] **CBN-050** [medium] [unattended] | later-tiers — later.

## Completed

- [x] **CBN-011** [medium] | flaitron-reader — Completed 2026-08-19.
"""

TWO_CLOSED = """# sample

## High

(none)

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

- [x] **CBN-011** [medium] | flaitron-reader — Completed 2026-08-19.
- [x] **CBN-012** [medium] | cao-client — Completed 2026-08-19.
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, message: str, *, author: str = "test") -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        f"user.name={author}",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--no-gpg-sign",
        "-m",
        message,
    )


def _amend(repo: Path) -> None:
    """Amend HEAD with a new file, as a worker's post-closure fix-up does."""
    (repo / "amended.txt").write_text("amend\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "--amend", "--no-edit")


def _git_repo(
    tmp_path: Path,
    plan: str,
    notes: dict[str, str] | None = None,
    *,
    unattended: bool = True,
) -> Path:
    repo = tmp_path / "repo"
    tasknotes = repo / ".flaitron" / "tasknote"
    tasknotes.mkdir(parents=True)
    (repo / ".flaitron" / "PLAN.md").write_text(plan, encoding="utf-8")
    for task_id, body in (notes or {}).items():
        (tasknotes / f"{task_id}.md").write_text(body, encoding="utf-8")
    if unattended:
        _pin_supports_unattended(repo)
    _git(repo, "init")
    _commit(repo, "init")
    return repo


def _pin_supports_unattended(repo: Path) -> None:
    """Seed the pinned `/ft-task` shape `flaitron.supports_unattended` probes.

    The default for every fixture repo: both registered projects sit at a pin
    that recognizes the flag, so a capable target is the ordinary case and the
    degrade is what a test opts into (CBN-108).
    """
    skill_dir = repo / FT_TASK_SKILL_DIR_REL
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / UNATTENDED_MODULE).write_text("unattended posture\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "Trailing `--fast` / `-f` and `--unattended` flags are accepted.\n",
        encoding="utf-8",
    )


def _archive_note(
    repo: Path,
    task_id: str,
    *,
    descoped: bool = False,
    follow_ups: str = "",
) -> None:
    archive = repo / ".flaitron" / "tasknote" / "archive" / "cbn"
    archive.mkdir(parents=True, exist_ok=True)
    verdict = "De-scope" if descoped else "Proceed"
    (archive / f"{task_id}.md").write_text(
        (
            "---\ntitle: t\nstatus: completed\ncreated: 2026-08-19\n---\n\n"
            f"**Verdict:** {verdict}\n\n"
            f"{follow_ups}"
            "**Final Summary:**\n\nThe reader picks the next task.\n\n"
            "**Archived:** 2026-08-19\n"
        ),
        encoding="utf-8",
    )
    open_note = repo / ".flaitron" / "tasknote" / f"{task_id}.md"
    if open_note.exists():
        open_note.unlink()


def _close_worker(
    repo: Path,
    task_id: str = "CBN-011",
    plan: str = CLOSED_PLAN,
    *,
    descoped: bool = False,
    follow_ups: str = "",
) -> None:
    (repo / ".flaitron" / "PLAN.md").write_text(plan, encoding="utf-8")
    _archive_note(repo, task_id, descoped=descoped, follow_ups=follow_ups)
    _commit(repo, f"feat: {task_id} — close")


def _note(status: str, park_reason: str | None = None) -> str:
    park = f"park-reason: {park_reason}\n" if park_reason is not None else ""
    return f"---\ntitle: t\nstatus: {status}\n{park}created: 2026-08-19\n---\n\n# body\n"


def _register(
    store: Store,
    repo: Path,
    slug: str = "app",
    tier: ExecutionTier = ExecutionTier.GATED,
    provider: str | None = None,
    model: str | None = None,
    providers: list[str] | None = None,
) -> None:
    store.register(
        RegistryRecord(
            id=slug,
            path=str(repo.resolve()),
            tier=tier,
            provider=provider,
            model=model,
            providers=providers,
            registered_at=utc_now(),
            enabled=True,
        )
    )


def _usage(*providers: str) -> list[ProviderUsage]:
    names = providers or ("grok_cli", "claude_code", "codex", "cursor_cli")
    return [
        ProviderUsage(provider=name, installed=True, leftover=Leftover.UNKNOWN) for name in names
    ]


def _row(
    provider: str,
    *,
    installed: bool = True,
    leftover: Leftover = Leftover.UNKNOWN,
    remaining: float | None = None,
) -> ProviderUsage:
    return ProviderUsage(
        provider=provider,
        installed=installed,
        leftover=leftover,
        remaining=remaining,
    )


def _green_suite(_repo: Path, _store: Store) -> SuiteRun:
    """The injected suite runner: green, instant, no `just` (CBN-EPIC-202.3)."""
    return SuiteRun(
        suite=Suite.from_verbs(
            [
                SuiteVerb(verb="test", exit_code=0, duration=1.5, detail="9 passed"),
                SuiteVerb(verb="lint", exit_code=0, duration=0.2),
                SuiteVerb(verb="typecheck", exit_code=0, duration=0.4),
            ]
        ),
        log="verdict: green\n\ntest: fake\n",
    )


def _run(project_id: str, **kwargs):
    kwargs.setdefault("usage", _usage())
    kwargs.setdefault("run_suite", _green_suite)
    return execute_run(project_id, **kwargs)


class FakeWorker:
    """In-memory `worker.Worker`: records what the loop hands it, runs nothing.

    `state` is what `status()` reports next; a test (or its `on_send` hook)
    flips it to drive the close wait. `created` is the last `launch` kwargs.
    """

    def __init__(self) -> None:
        self.launches: list[dict[str, object]] = []
        self.created: dict[str, object] | None = None
        self.messages: list[str] = []
        self.deleted: list[str] = []
        self.state: str = "idle"
        self.on_send: Callable[[str], None] | None = None
        self.closed = False

    def __enter__(self) -> FakeWorker:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def launch(
        self,
        cwd: Path | str,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_name: str | None = None,
        log_path: Path | None = None,
    ) -> Session:
        root = resolve_cwd(cwd)
        name = safe_session_name(session_name or "auto")
        self.created = {
            "cwd": str(root),
            "provider": provider,
            "model": model,
            "effort": effort,
            "session_name": name,
            "log_path": log_path,
        }
        self.launches.append(self.created)
        return Session(
            session_name=name,
            terminal_id=f"t{len(self.launches)}",
            provider=provider,
        )

    def send(self, session: Session, message: str) -> None:  # noqa: ARG002
        if not message:
            raise UserError("message must not be empty")
        self.messages.append(message)
        if self.on_send is not None:
            self.on_send(message)

    def status(self, session: Session) -> str:  # noqa: ARG002
        return self.state

    def exit_code(self, session: Session) -> int | None:  # noqa: ARG002
        return None

    def stderr_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return ()

    def stdout_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return ()

    def shutdown(self, session: Session) -> None:
        self.deleted.append(session.session_name)

    def close(self) -> None:
        self.closed = True


def _closing_client(fake: FakeWorker, on_send: Callable[[str], None]) -> FakeWorker:
    """`fake` with `on_send` wired — the hook that closes the task (or flips state)."""
    fake.on_send = on_send
    return fake


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
    assert result.provider == "grok_cli"
    assert result.model == "grok-4.7"
    assert fake.created is not None
    assert fake.created["provider"] == "grok_cli"
    assert fake.created["model"] == "grok-4.7"
    loaded = store.last_run("app")
    assert loaded is not None
    assert loaded.task_id == "CBN-011"
    assert loaded.model == "grok-4.7"
    assert fake.messages == [
        f"/ft-task CBN-011 --unattended {PINNED} {FOREGROUND_ONLY} {PARK_CHECK} {FILED}"
    ]
    assert fake.deleted


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
    assert result.provider == "grok_cli"
    assert result.model == "grok-4.7"


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


def test_registry_pin_wins_over_leftover(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, provider="claude_code", model="opus")
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.provider == "claude_code"
    assert result.model == "opus"
    assert fake.created is not None
    assert fake.created["provider"] == "claude_code"
    assert fake.created["model"] == "opus"


def test_leftover_available_beats_v1_order(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    usage = [
        _row("grok_cli", leftover=Leftover.EXHAUSTED),
        _row("claude_code", leftover=Leftover.AVAILABLE, remaining=0.2),
        _row("codex"),
        _row("cursor_cli"),
    ]
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2, usage=usage)
    assert result is not None
    assert result.provider == "claude_code"
    assert result.model == "opus"


def test_registry_allow_list_steers_the_launch(tmp_path: Path, caobunga_home: Path) -> None:
    # Without the list, all-unknown leftover hands this to grok_cli (CBN-EPIC-9.4).
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2, usage=_usage())
    assert result is not None
    assert result.provider == "claude_code"
    assert result.model == "opus"
    assert fake.created is not None
    assert fake.created["provider"] == "claude_code"


def test_allow_list_still_yields_to_leftover(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    usage = [
        _row("claude_code"),
        _row("codex", leftover=Leftover.AVAILABLE, remaining=0.5),
    ]
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2, usage=usage)
    assert result is not None
    assert result.provider == "codex"


def test_no_eligible_member_of_allow_list_is_unlaunchable(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    fake = FakeWorker()
    usage = [
        _row("grok_cli"),  # eligible, but the operator did not declare it
        _row("claude_code", leftover=Leftover.EXHAUSTED),
        _row("codex", installed=False),
    ]
    with fake as worker, pytest.raises(UserError, match="no eligible worker in") as exc:
        _run("app", store=store, worker=worker, poll_interval=0, usage=usage)
    assert exc.value.exit_code == 5
    assert fake.created is None
    assert store.last_run("app") is None


def test_no_eligible_worker_is_unlaunchable(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    usage = [
        _row("grok_cli", installed=False),
        _row("claude_code", leftover=Leftover.EXHAUSTED),
        _row("codex", installed=False),
        _row("cursor_cli", leftover=Leftover.EXHAUSTED),
    ]
    with fake as worker, pytest.raises(UserError, match="no eligible worker") as exc:
        _run("app", store=store, worker=worker, poll_interval=0, usage=usage)
    assert exc.value.exit_code == 5
    assert fake.created is None
    assert store.last_run("app") is None


def test_uninstalled_pin_is_unlaunchable(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, provider="grok_cli")
    fake = FakeWorker()
    usage = [_row("grok_cli", installed=False)]
    with fake as worker, pytest.raises(UserError, match="not installed") as exc:
        _run("app", store=store, worker=worker, poll_interval=0, usage=usage)
    assert exc.value.exit_code == 5
    assert fake.created is None
    assert store.last_run("app") is None


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


def test_gated_writes_the_status_card(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-188.2: the last-run card lands under `$CAOBUNGA_HOME/status/`, not the target repo."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
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
        )
    assert result is not None
    assert not (repo / "caobunga-status.md").exists()
    status = store.status_file("app")
    assert status.is_file()
    body = status.read_text(encoding="utf-8")
    assert body.startswith("# Caobunga status\n")
    assert "- **task:** CBN-011\n" in body
    assert "- **outcome:** closed\n" in body
    assert result.summary in body
    assert "## 📝" not in body
    assert any(line.startswith("card: ") for line in lines)
    head_after = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_after == result.commit_sha


def test_gated_records_and_echoes_a_foreign_commit(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-172: a commit landing mid-window is recorded and echoed, not swallowed."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []

    def _close_with_foreign_commit(_msg: str) -> None:
        (repo / "scratch.txt").write_text("filed CBN-999\n", encoding="utf-8")
        _commit(repo, "chore: unrelated sweep filing", author="Someone Else")
        _close_worker(repo)

    with _closing_client(fake, _close_with_foreign_commit) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
        )
    assert result is not None
    assert result.foreign_commits is not None
    assert len(result.foreign_commits) == 1
    commit = result.foreign_commits[0]
    assert commit.subject == "chore: unrelated sweep filing"
    assert commit.author == "Someone Else"
    assert any(line == f"foreign: {commit.sha} {commit.author} {commit.subject}" for line in lines)


def test_gated_echoes_a_workers_own_filing_as_filed_not_foreign(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-299: the worker's `/ft-file-followup` commit is `new_rows`' account, not foreign."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []

    def _file_then_close(_msg: str) -> None:
        plan = repo / ".flaitron" / "PLAN.md"
        plan.write_text(
            OPEN_PLAN.replace(
                "## Medium\n\n(none)",
                "## Medium\n\n- [ ] **CBN-014** [light] | worker-filed — filed mid-run.",
            ),
            encoding="utf-8",
        )
        _commit(repo, "chore: file CBN-014 follow-up")
        _close_worker(repo, plan=FILED_PLAN)

    with _closing_client(fake, _file_then_close) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, echo=lines.append
        )
    assert result is not None
    assert result.foreign_commits is None
    assert result.new_rows == [NewRow(task_id="CBN-014", unattended=False)]
    assert any(line.startswith("filed: ") and "file CBN-014 follow-up" in line for line in lines)
    assert not any(line.startswith("foreign: ") for line in lines)


def test_observe_does_not_write_status_card(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.OBSERVE)
    fake = FakeWorker()
    with fake as worker:
        result = execute_run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
        )
    assert result is None
    assert not (repo / "caobunga-status.md").exists()
    assert not store.status_file("app").exists()


def test_status_card_failure_degrades_only_that_surface(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []

    def boom(_store: Store, _run: object) -> Path:
        raise WorkerError("status card write failed: boom")

    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        with pytest.raises(ReportError, match="status card write failed") as exc:
            _run(
                "app",
                store=store,
                worker=worker,
                poll_interval=0,
                timeout=2,
                publish_status=boom,
                echo=lines.append,
            )
    # CBN-EPIC-9.5 F4: a card failure costs that surface alone — the record stays
    # written and carried on the error, and the escalation stands.
    assert exc.value.exit_code == 4
    assert exc.value.record.task_id == "CBN-011"
    loaded = store.last_run("app")
    assert loaded is not None
    assert loaded.outcome is Outcome.CLOSED
    assert "card: failed: status card write failed: boom" in lines


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
    assert "drain: outcome — CBN-011 outcome blocked (destructive)" in lines


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


def _not_green_suite(verdict: str) -> Callable[[Path, Store], SuiteRun]:
    """A suite runner whose `test` verb is `red` / `timeout` / `refused` (CBN-EPIC-202.4)."""
    if verdict == "red":
        test = SuiteVerb(verb="test", exit_code=1, duration=1.5, detail="1 failed")
    elif verdict == "timeout":
        test = SuiteVerb(verb="test", duration=600.0, timed_out=True)
    else:
        test = SuiteVerb(verb="test", duration=0.0, refused="no justfile")

    def runner(_repo: Path, _store: Store) -> SuiteRun:
        return SuiteRun(
            suite=Suite.from_verbs(
                [
                    test,
                    SuiteVerb(verb="lint", exit_code=0, duration=0.2),
                    SuiteVerb(verb="typecheck", exit_code=0, duration=0.4),
                ]
            ),
            log=f"verdict: {verdict}\n\ntest: fake\n",
        )

    return runner


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


class _IdleWorker:
    """Worker that stays `processing`; `wait_for_close` returns only on a close."""

    def status(self, session: Session) -> str:  # noqa: ARG002
        return STATUS_PROCESSING

    def exit_code(self, session: Session) -> int | None:  # noqa: ARG002
        return None

    def stderr_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return ()

    def stdout_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return ()


def test_wait_for_close_survives_transient_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-168: a poll whose read hits a vanished path answers "not yet", not exit 1.

    Models the 2026-09-12 CBN-159 run: `inspect_close` raised `FileNotFoundError`
    on the worker's archive move and the exception escaped the loop, so no run
    record was written and the crash's cleanup killed a worker that had finished.
    """
    calls = 0
    closed = CloseSnapshot(outcome=Outcome.CLOSED, commit_sha="abc", summary="done")

    def flaky_inspect(*args: object, **kwargs: object) -> CloseSnapshot | None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError(tmp_path / ".flaitron" / "tasknote" / "X-1.md")
        return closed

    monkeypatch.setattr("caobunga.wait.inspect_close", flaky_inspect)
    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        _IdleWorker(),  # type: ignore[arg-type]
        session,
        tmp_path,
        "X-1",
        "start",
        timeout=10.0,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert snap is closed
    assert calls == 2


def test_wait_for_close_survives_transient_git_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-269: one hung `git` read mid-run answers "not yet", not an exit-2 abort."""
    calls = 0
    closed = CloseSnapshot(outcome=Outcome.CLOSED, commit_sha="abc", summary="done")

    def flaky_inspect(*args: object, **kwargs: object) -> CloseSnapshot | None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GitUnreadable(tmp_path)
        return closed

    monkeypatch.setattr("caobunga.wait.inspect_close", flaky_inspect)
    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        _IdleWorker(),  # type: ignore[arg-type]
        session,
        tmp_path,
        "X-1",
        "start",
        timeout=10.0,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert snap is closed
    assert calls == 2


class _StatusWorker(_IdleWorker):
    """Worker pinned to one status, for the `wait_for_close` status-ladder pins."""

    def __init__(self, state: str) -> None:
        self.state = state

    def status(self, session: Session) -> str:  # noqa: ARG002
        return self.state


def test_wait_for_close_exited_keeps_a_last_action_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-shot worker whose close lands on its last action returns it, not WORKER_EXITED.

    Not a duplicate of `test_worker_exit_after_close_still_lands_closed`: that one
    closes before the first poll and never reads `status()`; this one reaches the
    EXITED re-poll branch.
    """
    calls = 0
    closed = CloseSnapshot(outcome=Outcome.CLOSED, commit_sha="abc", summary="done")

    def late_inspect(*args: object, **kwargs: object) -> CloseSnapshot | None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return closed if calls == 2 else None

    monkeypatch.setattr("caobunga.wait.inspect_close", late_inspect)
    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        _StatusWorker(STATUS_EXITED),  # type: ignore[arg-type]
        session,
        tmp_path,
        "X-1",
        "start",
        timeout=10.0,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert snap is closed
    assert calls == 2


def test_wait_for_close_stops_the_observer_on_a_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An escalation out of the loop still stops and joins the watchdog observer."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    observers: list[object] = []
    real_start_watch = wait._start_watch

    def capturing(root: Path, wake: threading.Event) -> object:
        observer = real_start_watch(root, wake)
        observers.append(observer)
        return observer

    monkeypatch.setattr(wait, "_start_watch", capturing)
    session = Session(session_name="s", terminal_id="t", provider=None)
    with pytest.raises(WorkerError) as exc:
        wait_for_close(
            _StatusWorker(STATUS_ERROR),  # type: ignore[arg-type]
            session,
            repo,
            "CBN-011",
            start,
            timeout=10.0,
            poll_interval=5.0,
            sleep=lambda _: None,
        )
    assert exc.value.escalation is Escalation.WORKER_ERROR
    (observer,) = observers
    assert observer is not None
    assert not observer.is_alive()  # type: ignore[attr-defined]


_PHASE_NAMES = {1: "Discovery", 2: "Execution", 3: "Testing & Linting", 4: "Closure"}


def _phase_note(*phases: list[bool], emoji: bool = False) -> str:
    """A minimal open tasknote with the given per-phase checkbox states."""
    parts = ["---\nstatus: in-progress\n---\n\n"]
    marks = {1: "📝", 2: "🛠️", 3: "🧪", 4: "🚀"}
    for number, boxes in enumerate(phases, start=1):
        prefix = f"{marks[number]} " if emoji else ""
        parts.append(f"## {prefix}Phase {number}: {_PHASE_NAMES[number]}\n\n")
        for done in boxes:
            tick = "x" if done else " "
            parts.append(f"- [{tick}] step\n")
        parts.append("\n")
    return "".join(parts)


def test_phase_progress_reaches_phase_1_by_heading() -> None:
    assert phase_progress(_phase_note([False], [False])) == "1 discovery"
    assert phase_progress(_phase_note([False], [False], emoji=True)) == "1 discovery"


def test_phase_progress_advances_when_previous_phase_is_complete() -> None:
    assert phase_progress(_phase_note([True, True], [False])) == "2 execution"
    assert phase_progress(_phase_note([True], [True, False], [False])) == "2 execution"
    assert phase_progress(_phase_note([True], [True], [False])) == "3 testing"
    assert phase_progress(_phase_note([True], [True], [True], [True])) == "4 closure"


def test_phase_progress_skips_ahead_on_a_later_tick() -> None:
    assert phase_progress(_phase_note([False], [False], [True])) == "3 testing"


def test_phase_progress_ignores_a_fenced_phase_heading() -> None:
    """CBN-344: a quoted `## Phase 4:` inside a fence is body, not a phase."""
    quoted = "```markdown\n## 🚀 Phase 4: Closure\n\n- [x] done\n```\n\n"
    note = _phase_note([False]) + quoted
    assert phase_progress(note) == "1 discovery"


def test_phase_progress_ignores_fenced_checkbox_lines() -> None:
    """CBN-346: a quoted `- [ ]` / `- [x]` inside a fence is not a phase checkbox."""
    head = "---\nstatus: in-progress\n---\n\n"
    unticked_quote = "```markdown\n- [ ] quoted\n```\n\n"
    ticked_quote = "```markdown\n- [x] quoted\n```\n\n"
    done_phase_1 = f"{head}## 📝 Phase 1: Discovery\n\n- [x] step\n\n{unticked_quote}"
    assert phase_progress(f"{done_phase_1}## 🛠️ Phase 2: Execution\n\n- [ ] step\n") == (
        "2 execution"
    )
    open_phase_1 = f"{head}## 📝 Phase 1: Discovery\n\n- [ ] step\n\n"
    assert phase_progress(f"{open_phase_1}## 🛠️ Phase 2: Execution\n\n{ticked_quote}") == (
        "1 discovery"
    )


def test_phase_progress_micro_tasknote_is_notes() -> None:
    body = "---\nstatus: in-progress\n---\n\n## ⚡ Notes\n\n**Implementation:** doing it\n"
    assert phase_progress(body) == "notes"
    assert phase_progress("---\nstatus: in-progress\n---\n\n# body\n") is None


def test_wait_for_close_echoes_phase_progress(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _phase_note([False], [False])})
    start = git_head(repo)
    assert start is not None
    lines: list[str] = []
    ticks = {"n": 0}

    def advance(_dt: float) -> None:
        ticks["n"] += 1
        path = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        if ticks["n"] == 1:
            path.write_text(_phase_note([True], [False]), encoding="utf-8")
        elif ticks["n"] == 2:
            path.write_text(_phase_note([True], [True]), encoding="utf-8")
        else:
            _close_worker(repo)

    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        _IdleWorker(),  # type: ignore[arg-type]
        session,
        repo,
        "CBN-011",
        start,
        timeout=10.0,
        poll_interval=0,
        sleep=advance,
        echo=lines.append,
    )
    assert snap.outcome is Outcome.CLOSED
    assert [line for line in lines if line.startswith("phase:")] == [
        "phase: 1 discovery",
        "phase: 2 execution",
    ]


def test_wait_for_close_skips_a_missing_note_then_echoes(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    lines: list[str] = []
    ticks = {"n": 0}

    def advance(_dt: float) -> None:
        ticks["n"] += 1
        path = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        if ticks["n"] == 1:
            path.write_text(_phase_note([False]), encoding="utf-8")
        else:
            _close_worker(repo)

    session = Session(session_name="s", terminal_id="t", provider=None)
    snap = wait_for_close(
        _IdleWorker(),  # type: ignore[arg-type]
        session,
        repo,
        "CBN-011",
        start,
        timeout=10.0,
        poll_interval=0,
        sleep=advance,
        echo=lines.append,
    )
    assert snap.outcome is Outcome.CLOSED
    assert [line for line in lines if line.startswith("phase:")] == ["phase: 1 discovery"]


def test_instant_close_emits_no_phase_echo(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _phase_note([False], [False])})
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
    assert not any(line.startswith("phase:") for line in lines)


class _BusyWorker:
    """Worker that keeps reporting `processing` for a while after the close lands.

    Models the real CBN-EPIC-9.4 failure: `close.inspect_close` fires on the
    worker's commit, but the worker subprocess is still doing its own post-closure
    git work against the same index.

    `events` is the session-lifecycle log (CBN-EPIC-244.4): `launch:<provider>`,
    `shutdown:<provider>`, `close`, in call order. Keyed by provider because a
    re-pick reuses the session name.
    """

    def __init__(self, on_send: Callable[[], object], busy_polls: int) -> None:
        # Return value is discarded, so a `write_text` lambda is as good as `None`.
        self._on_send = on_send
        self._busy_polls = busy_polls
        self.polls = 0
        self.settled = False
        self.model: str | None = None
        self.effort: str | None = None
        self.log_path: Path | None = None
        self.events: list[str] = []

    def launch(
        self,
        cwd: Path | str,  # noqa: ARG002
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_name: str | None = None,
        log_path: Path | None = None,
    ) -> Session:
        self.model = model
        self.effort = effort
        self.log_path = log_path
        self.events.append(f"launch:{provider}")
        return Session(
            session_name=session_name or "s",
            terminal_id="t",
            provider=provider,
        )

    def send(self, session: Session, message: str) -> None:  # noqa: ARG002
        self._on_send()

    def status(self, session: Session) -> str:  # noqa: ARG002
        self.polls += 1
        if self.polls <= self._busy_polls:
            return STATUS_PROCESSING
        self.settled = True
        return STATUS_EXITED

    def exit_code(self, session: Session) -> int | None:  # noqa: ARG002
        return None

    def stderr_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return ()

    def stdout_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return ()

    def shutdown(self, session: Session) -> None:
        self.events.append(f"shutdown:{session.provider}")

    def close(self) -> None:
        self.events.append("close")


def test_worker_settles_before_caobunga_touches_the_repo(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-EPIC-9.5 F1. No git work in the target repo while the worker is alive."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _BusyWorker(lambda: _close_worker(repo), busy_polls=3)
    busy_at_publish: list[bool] = []

    def publish(_store: Store, _run: object) -> Path:
        busy_at_publish.append(not worker.settled)
        return store.status_file("app")

    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        sleep=lambda _s: None,
        publish_status=publish,
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert worker.polls > 3
    assert busy_at_publish == [False]


class _SettleEditWorker(_BusyWorker):
    """Rewrites the repo on its first settle poll, while it still reports busy (CBN-275)."""

    def __init__(self, on_send: Callable[[], object], on_settle: Callable[[], object]) -> None:
        super().__init__(on_send, busy_polls=1)
        self._on_settle = on_settle

    def status(self, session: Session) -> str:
        if self.polls == 0:
            self._on_settle()
        return super().status(session)


def _settle_edit_run(
    tmp_path: Path, home: Path, edit: Callable[[Path], object]
) -> tuple[Path, str | None, RunRecord | None]:
    """Run one gated close whose worker calls `edit(repo)` mid-settle; return the detected sha."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(home)
    _register(store, repo)
    detected: list[str | None] = []

    def on_settle() -> None:
        detected.append(git_head(repo))
        edit(repo)

    worker = _SettleEditWorker(lambda: _close_worker(repo), on_settle)
    result = _run(
        "app", store=store, worker=worker, poll_interval=0, timeout=2, sleep=lambda _s: None
    )
    return repo, detected[0], result


def test_settle_rereads_a_close_the_worker_amended(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-275. A fleet repo's FE-038: an amend after detection left `commit_sha` off-branch."""

    def amend(repo: Path) -> None:
        note = repo / ".flaitron" / "tasknote" / "archive" / "cbn" / "CBN-011.md"
        text = note.read_text(encoding="utf-8")
        note.write_text(text.replace("The reader picks", "The amend picks"), encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "--amend", "--no-edit")

    repo, detected, result = _settle_edit_run(tmp_path, caobunga_home, amend)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.commit_sha == git_head(repo)
    assert result.commit_sha != detected
    assert result.summary.startswith("The amend picks the next task.")
    assert result.foreign_commits is None


def test_settle_keeps_the_close_under_a_stacked_commit(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-275. A later commit leaves the close on-branch — it is not re-attributed."""

    def stack(repo: Path) -> None:
        (repo / "after.txt").write_text("after\n", encoding="utf-8")
        _commit(repo, "chore: after the close")

    repo, detected, result = _settle_edit_run(tmp_path, caobunga_home, stack)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.commit_sha == detected
    assert git_head(repo) != detected


def _effort_seen(tmp_path: Path, home: Path, plan_model: str) -> tuple[str | None, str | None]:
    """Run one gated task whose PLAN line carries `[<plan_model>]`; report launch kwargs."""
    plan = OPEN_PLAN.replace("**CBN-011** [medium]", f"**CBN-011** [{plan_model}]")
    repo = _git_repo(tmp_path, plan)
    store = Store(home)
    _register(store, repo, slug=plan_model, provider="grok_cli")
    worker = _BusyWorker(lambda: _close_worker(repo), busy_polls=0)
    result = _run(plan_model, store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    return worker.model, worker.effort


def test_loop_passes_the_class_effort_to_launch(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-EPIC-15.3. The rung reaches the worker seam, from the PLAN token and provider."""
    model, effort = _effort_seen(tmp_path, caobunga_home, "light")
    assert effort == "high"
    assert model == "grok-4.7"


def test_grok_light_shares_the_medium_rung_on_the_flat_id(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """End to end on grok: one model id, light and medium share `high`, heavy is `xhigh`.

    CBN-332 separated all three. CBN-360 takes the upper end of the light range,
    so light and medium dispatch the same rung.
    """
    seen = [_effort_seen(tmp_path / c, caobunga_home, c) for c in ("light", "medium", "heavy")]
    assert {model for model, _ in seen} == {"grok-4.7"}
    assert [effort for _, effort in seen] == ["high", "high", "xhigh"]


def test_settle_worker_gives_up_after_the_grace(tmp_path: Path) -> None:
    """CBN-EPIC-9.5 F1. A grace, not a gate — a wedged worker must not add an abort."""
    worker = _BusyWorker(lambda: None, busy_polls=10**6)
    session = worker.launch(tmp_path)
    settle_worker(worker, session, poll_interval=0.001, sleep=lambda _s: None, grace=0.05)
    assert worker.polls > 0
    assert not worker.settled


def test_settle_worker_returns_when_status_raises(tmp_path: Path) -> None:
    """A worker that cannot be polled is not a reason to abort a successful close."""

    class _Unpollable(_BusyWorker):
        def status(self, session: Session) -> str:  # noqa: ARG002
            raise WorkerError("terminal is gone")

    worker = _Unpollable(lambda: None, busy_polls=0)
    session = worker.launch(tmp_path)
    settle_worker(worker, session, poll_interval=0, sleep=lambda _s: None, grace=5)


# --- run signal fields (CBN-EPIC-12.2) -----------------------------------


def test_started_at_precedes_closed_at(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.started_at is not None
    assert result.started_at <= result.closed_at
    assert result.escalation_reason is None


def test_worker_parked_records_escalation(tmp_path: Path, caobunga_home: Path) -> None:
    """The worker flipped the note to blocked mid-run — distinct from note-blocked."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked"), encoding="utf-8")

    with _closing_client(fake, park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.BLOCKED
    assert result.escalation_reason is Escalation.WORKER_PARKED


def test_park_reason_is_absent_when_the_note_carries_none(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Legacy parks and pre-`--unattended` notes keep the bare summary."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked"), encoding="utf-8")

    with _closing_client(fake, park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.park_reason is None
    assert result.summary == "tasknote parked (status: blocked)"
    assert "park_reason" not in store.run_file(result).read_text(encoding="utf-8")


def test_worker_parked_records_park_reason(tmp_path: Path, caobunga_home: Path) -> None:
    """The mid-run site reads the reason the worker just wrote."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked", "drift — Discovery found a different root cause"), "utf-8")

    with _closing_client(fake, park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_PARKED
    assert result.park_reason == "drift"
    assert result.summary == (
        "tasknote parked (status: blocked): Discovery found a different root cause"
    )


def test_park_reason_without_blocked_status_is_worker_exited(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-327: a `park-reason:` on an `in-progress` note is not a park.

    A fleet repo's CORE-243 shape (CBN-274's second Codex retry). The worker is
    now asked to check this before exiting, but close detection reads `status:`
    alone, so a worker that ignores the cue still fails rather than reading as
    parked.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def half_park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("in-progress", "input-needed — bare pyright is red"), "utf-8")
        fake.state = STATUS_EXITED

    with _closing_client(fake, half_park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=600)
    assert PARK_CHECK in fake.messages[0]
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.park_reason is None
    # CBN-328: the mismatch is named in the summary, still never read as a park.
    assert "park-reason present but status in-progress, not blocked" in result.summary


def test_worker_exited_without_park_reason_names_no_mismatch(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-328: an `in-progress` note with no `park-reason:` gets no mismatch clause."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def plain_exit(_message: str) -> None:
        fake.state = STATUS_EXITED

    with _closing_client(fake, plain_exit) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=600)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert "park-reason" not in result.summary


def test_worker_parked_survives_a_colon_in_the_park_prose(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-186: a fleet repo's APP-74 shape — `: ` in the prose breaks the note's YAML.

    Before the reader's line-wise fallback the parked note dropped out of the open
    set, `inspect_close` never reached its parked branch, and the run recorded
    `failed / worker-exited` with the worker's unrelated stderr as the cause.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(
            _note("blocked", "visual-confirm — Phase 3 👁️ CONFIRM: header renders"), "utf-8"
        )

    with _closing_client(fake, park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.BLOCKED
    assert result.escalation_reason is Escalation.WORKER_PARKED
    assert result.park_reason == "visual-confirm"
    assert result.summary == "tasknote parked (status: blocked): Phase 3 👁️ CONFIRM: header renders"


def test_launch_failure_records_escalation(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise WorkerError("worker refused the session")

    fake.launch = boom  # type: ignore[method-assign]
    with fake as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_LAUNCH
    assert result.provider is None


def test_timeout_records_escalation(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=0.05)
    assert result is not None
    assert result.escalation_reason is Escalation.TIMEOUT


def test_worker_exited_records_escalation(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def flip(_message: str) -> None:
        fake.state = STATUS_EXITED

    with _closing_client(fake, flip) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=600)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_EXITED


def test_run_record_log_path_is_set_when_the_worker_wrote_one(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-162: the tee path is a run-record field, but only once a file landed."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _BusyWorker(lambda: None, busy_polls=0)

    def close_and_log() -> None:
        assert worker.log_path is not None
        worker.log_path.parent.mkdir(parents=True, exist_ok=True)
        worker.log_path.write_text("[stdout] hi\n")
        _close_worker(repo)

    worker._on_send = close_and_log
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.log_path == str(worker.log_path)


def test_run_record_log_path_is_absent_without_a_file(tmp_path: Path, caobunga_home: Path) -> None:
    """An adapter that ignores `log_path` leaves no dangling pointer."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _BusyWorker(lambda: _close_worker(repo), busy_polls=0)
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.log_path is None


class _ExitedWorker(_BusyWorker):
    """Exits without closing, carrying an exit code and captured stderr/stdout lines."""

    def __init__(
        self,
        code: int,
        stderr_line: str,
        stdout_line: str = "",
        *,
        on_send: Callable[[], object] = lambda: None,
    ) -> None:
        super().__init__(on_send, busy_polls=0)
        self._code = code
        self._stderr_line = stderr_line
        self._stdout_line = stdout_line

    def exit_code(self, session: Session) -> int | None:  # noqa: ARG002
        return self._code

    def stderr_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return _tail(self._stderr_line)

    def stdout_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return _tail(self._stdout_line)


def test_worker_exited_includes_exit_code_and_stderr_line(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-162: a usage-limit refusal, a bad arg, and a crash used to look identical."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(1, "Error: usage limit reached")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert "(exit code 1; stderr: Error: usage limit reached)" in (result.summary or "")


def test_worker_exited_includes_the_last_stdout_line_on_a_clean_exit(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-200: exit 0 usually has no stderr, so the diagnostic is the last stdout line."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(0, "", "Waiting for `just check` to finish.")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert "stdout: Waiting for `just check` to finish." in (result.summary or "")
    assert "stderr:" not in (result.summary or "")


class _StuckWorker(_BusyWorker):
    """Never leaves `processing`, but has already captured a stderr/stdout line."""

    def __init__(self, stderr_line: str, stdout_line: str = "") -> None:
        super().__init__(lambda: None, busy_polls=10_000)
        self._stderr_line = stderr_line
        self._stdout_line = stdout_line

    def stderr_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return _tail(self._stderr_line)

    def stdout_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return _tail(self._stdout_line)


def test_timeout_includes_the_last_stderr_line_with_no_exit_code(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """A wedged, still-running worker has no exit code, but its stderr may still help."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _StuckWorker("waiting on a rate limit")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=0.05, grace=0)
    assert result is not None
    assert result.escalation_reason is Escalation.TIMEOUT
    assert "stderr: waiting on a rate limit" in (result.summary or "")
    assert "exit code" not in (result.summary or "")


def test_timeout_includes_both_stderr_and_stdout_lines_when_both_exist(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _StuckWorker("rate limited", "retrying in 30s")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=0.05, grace=0)
    assert result is not None
    assert result.escalation_reason is Escalation.TIMEOUT
    summary = result.summary or ""
    assert "stderr: rate limited" in summary
    assert "stdout: retrying in 30s" in summary


def test_exit_summary_skips_a_trailing_json_brace(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-283: a multi-line JSON error summarized as `stderr: }` (a fleet repo's FE-104)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(1, 'Internal error: {\n  "message": "bad request",\n}\n')
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    summary = result.summary or ""
    assert 'stderr: "message": "bad request",' in summary
    assert "stderr: }" not in summary


def test_exit_summary_prefers_message_over_trailing_json_field(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-285: a `"type": "billing"` field after the message is not the quoted line."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(1, '{\n  "message": "bad request",\n  "type": "invalid"\n}\n')
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert 'stderr: "message": "bad request",' in (result.summary or "")
    assert '"type"' not in (result.summary or "")


def test_exit_summary_prefers_usage_limit_line_and_strips_ansi(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-285: the refusal line beats a newer `"message"` line; a colour reset is no line."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stderr = '\x1b[31mError: usage limit reached\x1b[0m\n{"message": "later"}\n\x1b[0m\n'
    worker = _ExitedWorker(1, stderr)
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    summary = result.summary or ""
    assert "stderr: Error: usage limit reached)" in summary
    assert "\x1b" not in summary


def test_exit_summary_prefers_a_cause_bearing_stdout_line(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-292: a trailing `Follow-up:` aside is not the quoted line (a fleet repo's TEST-004)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stdout = (
        "I stopped before scaffolding anything: TEST-004 hasn't been started.\n"
        "- **Task state:** TEST-004 is open in `.flaitron/PLAN.md`.\n"
        "- **Fix:** run `/ft-update` to bump the `.flaitron/core` pin.\n"
        "- **Checks:** 0 errors, 42 passed, 0 failed.\n"
        "- **Follow-up:** I didn't file one, since the cause is a version mismatch.\n"
    )
    worker = _ExitedWorker(0, "Error: stopped\nlast stderr line\n", stdout)
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    summary = result.summary or ""
    assert "stdout: - **Fix:** run `/ft-update`" in summary
    assert "Follow-up" not in summary
    assert "stderr: last stderr line;" in summary


def _stdout_fragment(tmp_path: Path, caobunga_home: Path, stdout: str) -> str:
    """The `stdout:` fragment of a clean-exit summary for a worker that printed `stdout`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    result = _run(
        "app", store=store, worker=_ExitedWorker(0, "", stdout), poll_interval=0, timeout=2
    )
    assert result is not None
    return (result.summary or "").partition("stdout: ")[2]


def test_exit_summary_skips_a_trailing_aside_that_carries_a_marker(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-300: `Follow-up:` / `Note:` / `Next steps:` are commentary, even saying "can't"."""
    stdout = (
        "I stopped before scaffolding anything.\n"
        "- **Fix:** run `/ft-update` to bump the `.flaitron/core` pin.\n"
        "- **Note:** I can\u2019t verify the pin from here.\n"
        "\u2022 Next steps: it's blocked until then.\n"
        "1. Follow-up: I can't file one.\n"
    )
    assert _stdout_fragment(tmp_path, caobunga_home, stdout).startswith("- **Fix:** run")


def test_exit_summary_fix_marker_needs_a_word_start(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-300: `prefix:` is not `fix:`."""
    stdout = "- **Fix:** run `/ft-update`.\n- Config prefix: v2\n"
    assert _stdout_fragment(tmp_path, caobunga_home, stdout).startswith("- **Fix:** run")


def test_exit_summary_cause_outranks_a_newer_stdout_message_line(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-300: a streamed `"message"` JSON line after the cause is not the quoted line."""
    stdout = '- **Fix:** run `/ft-update`.\n{"message": "session closed"}\n'
    assert _stdout_fragment(tmp_path, caobunga_home, stdout).startswith("- **Fix:** run")


def test_punctuation_only_tail_adds_no_fragment(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-283: a tail with nothing informative is omitted, not quoted as `}`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(1, "}\n]\n")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert "(exit code 1)" in (result.summary or "")
    assert "stderr:" not in (result.summary or "")


def test_worker_exited_folds_tree_snapshot_with_dirt_and_open_tasknote(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-171: an exit-0 with an unclosed diff used to read identically to a no-op."""
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _note("in-progress")})
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(0, "")
    worker._on_send = lambda: (repo / "stray.txt").write_text("left behind\n", encoding="utf-8")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    summary = result.summary or ""
    assert "tree: 1 file(s) left (" in summary
    assert "stray.txt" in summary
    assert "tasknote open" in summary


def test_timeout_folds_tree_snapshot_when_clean_and_no_open_tasknote(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _StuckWorker("")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=0.05, grace=0)
    assert result is not None
    assert result.escalation_reason is Escalation.TIMEOUT
    summary = result.summary or ""
    assert "tree: 0 file(s) left (no open tasknote)" in summary


def test_worker_error_records_escalation(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def flip(_message: str) -> None:
        fake.state = STATUS_ERROR

    with _closing_client(fake, flip) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert "exit code" not in (result.summary or "")
    assert "tree:" not in (result.summary or "")


class _ErroringWorker(_BusyWorker):
    """Reaches `error` on the first poll and reports a concrete exit code.

    Models `LocalWorker` after a non-zero subprocess exit — CBN-110's F2.
    """

    def __init__(self, code: int) -> None:
        super().__init__(lambda: None, busy_polls=0)
        self._code = code

    def status(self, session: Session) -> str:  # noqa: ARG002
        self.polls += 1
        return STATUS_ERROR

    def exit_code(self, session: Session) -> int | None:  # noqa: ARG002
        return self._code


def test_worker_error_includes_exit_code_when_the_adapter_has_one(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-110. A recoverable cause reaches `summary` without a transcript."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ErroringWorker(17)
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert "(exit code 17)" in (result.summary or "")


def _tail(text: str | None) -> tuple[str, ...]:
    """A fake's `*_tail`: the non-empty lines of `text`, as `LocalWorker` keeps them."""
    return tuple(line for line in (text or "").splitlines() if line.strip())


class _UsageLimitThenCloseWorker(_BusyWorker):
    """First launch (or every launch, if `always`) is a usage-limit refusal.

    Later launches close via `on_close`. Models CBN-213's re-pick: a clean-tree
    worker-error whose last line is a usage/session-limit refusal.
    """

    def __init__(
        self,
        on_close: Callable[[], object],
        *,
        stderr: str = "Error: usage limit reached",
        stdout: str = "",
        code: int = 1,
        dirty: Callable[[], object] | None = None,
        always: bool = False,
        shutdown_fails: bool = False,
    ) -> None:
        super().__init__(on_close, busy_polls=0)
        self._stderr = stderr
        self._stdout = stdout
        self._code = code
        self._dirty = dirty
        self._always = always
        self._shutdown_fails = shutdown_fails
        self.launch_count = 0
        self.providers: list[str | None] = []
        self.efforts: list[str | None] = []

    def launch(
        self,
        cwd: Path | str,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_name: str | None = None,
        log_path: Path | None = None,
    ) -> Session:
        self.launch_count += 1
        self.polls = 0
        self.providers.append(provider)
        self.efforts.append(effort)
        return super().launch(
            cwd,
            provider=provider,
            model=model,
            effort=effort,
            session_name=session_name,
            log_path=log_path,
        )

    @property
    def _limiting(self) -> bool:
        return self._always or self.launch_count <= 1

    def send(self, session: Session, message: str) -> None:  # noqa: ARG002
        if self._limiting:
            if self._dirty is not None:
                self._dirty()
            return
        self._on_send()

    def status(self, session: Session) -> str:
        if self._limiting:
            self.polls += 1
            return STATUS_ERROR if self._code != 0 else STATUS_EXITED
        return super().status(session)

    def exit_code(self, session: Session) -> int | None:  # noqa: ARG002
        return self._code if self._limiting else 0

    def stderr_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return _tail(self._stderr if self._limiting else None)

    def stdout_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return _tail(self._stdout if self._limiting else None)

    def shutdown(self, session: Session) -> None:
        super().shutdown(session)
        if self._shutdown_fails:
            raise WorkerError("shutdown failed")


def test_usage_limit_repick_launches_next_allow_list_member(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-213: clean-tree usage-limit on A → B runs, one closed record."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    lines: list[str] = []
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo))
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
        echo=lines.append,
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.provider == "codex"
    assert worker.launch_count == 2
    assert worker.providers == ["claude_code", "codex"]
    assert len(store.list_runs("app")) == 1
    assert "usage-limit: claude_code exhausted this invocation; re-picking" in lines
    assert result.exhausted_providers == ["claude_code"]
    assert store.list_runs("app")[0].exhausted_providers == ["claude_code"]


def test_usage_limit_repick_resolves_effort_for_the_new_provider(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-332: the rung is the re-picked provider's column, not the first pick's."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo))
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert worker.providers == ["claude_code", "codex"]
    first, second = (EFFORT[p][TaskClass.MEDIUM] for p in ("claude_code", "codex"))
    assert first != second
    assert worker.efforts == [first, second]


def test_usage_limit_repick_also_pivots_on_worker_exited(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Exit 0 with a usage-limit last line is still a picker signal (CBN-162's example)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo),
        stderr="",
        stdout="Error: usage limit reached",
        code=0,
    )
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.provider == "codex"
    assert worker.launch_count == 2


def test_usage_limit_repick_reads_past_a_multiline_json_refusal(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-282: Grok's 402 is multi-line JSON whose last line is a bare `}`.

    Verbatim shape from a fleet repo's FE-104 (2026-10-06). No allow-list, so the
    re-pick falls to the next installed `V1_PROVIDERS` member — claude_code.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    block = (
        "Internal error: {\n"
        '  "message": "API error (status 402 Payment Required): '
        'Grok Build usage balance exhausted",\n'
        '  "http_status": 402\n'
        "}\n"
    )
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo), stderr=block + "Error: " + block
    )
    assert _tail(block)[-1] == "}"  # the line the pre-CBN-282 matcher saw
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("grok_cli", "claude_code"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert worker.providers == ["grok_cli", "claude_code"]
    assert result.provider == "claude_code"


def test_usage_limit_match_never_spans_two_lines(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-282 review: a marker split across a line break is not a refusal — escalate."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo), stderr="exceeded usage\nlimit=3"
    )
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert worker.launch_count == 1


def test_usage_limit_repick_skips_hard_provider_pin(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, provider="claude_code")
    lines: list[str] = []
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), always=True)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
        echo=lines.append,
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert worker.launch_count == 1
    assert not any(line.startswith("usage-limit:") for line in lines)
    assert "stderr: Error: usage limit reached" in (result.summary or "")


def test_usage_limit_repick_skips_dirty_tree(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo),
        dirty=lambda: (repo / "stray.txt").write_text("left behind\n", encoding="utf-8"),
    )
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.launch_count == 1
    assert result.provider == "claude_code"


def test_usage_limit_repick_skips_a_moved_head(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-260. The worker committed, then hit its limit: clean tree, HEAD moved.

    Porcelain alone read that as "left nothing" and relaunched the task on top
    of the worker's own commit.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    start = git_head(repo)

    def commit_then_refuse() -> None:
        (repo / "work.txt").write_text("half done\n", encoding="utf-8")
        _commit(repo, "wip: CBN-011")

    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), dirty=commit_then_refuse)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert git_dirt(repo) == []
    assert git_head(repo) != start
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.launch_count == 1
    assert result.provider == "claude_code"


def test_usage_limit_repick_skips_an_unreadable_head(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-269. An unreadable HEAD is not an unmoved one: escalate, never pivot."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    reads: list[Path] = []

    def head_then_unreadable(root: Path) -> str | None:
        reads.append(root)
        if len(reads) == 1:
            return git_head(root)
        raise GitUnreadable(root)

    # Read 1 is `_run_gated`'s start sha (loop), read 2 is `_should_repick`'s (launch).
    monkeypatch.setattr("caobunga.loop.git_head", head_then_unreadable)
    monkeypatch.setattr("caobunga.launch.git_head", head_then_unreadable)
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo))
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert len(reads) == 2
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.launch_count == 1


def test_usage_limit_repick_skips_non_matching_output(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(
        lambda: _close_worker(repo),
        stderr="waiting on a rate limit",
    )
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.launch_count == 1
    assert "rate limit" in (result.summary or "")


def test_usage_limit_repick_escalates_when_every_member_refuses(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    lines: list[str] = []
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), always=True)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
        echo=lines.append,
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert worker.launch_count == 2
    assert worker.providers == ["claude_code", "codex"]
    assert result.provider == "codex"
    assert len(store.list_runs("app")) == 1
    assert lines.count("usage-limit: claude_code exhausted this invocation; re-picking") == 1
    assert lines.count("usage-limit: codex exhausted this invocation; re-picking") == 1


class _UnlaunchableAfterWorker(_UsageLimitThenCloseWorker):
    """Launch `unlaunchable_from`-th onward raises `Unlaunchable`, as
    `LocalWorker._binary` does for a binary gone since `which` (CBN-316)."""

    def __init__(self, on_close: Callable[[], object], *, unlaunchable_from: int) -> None:
        super().__init__(on_close)
        self._unlaunchable_from = unlaunchable_from

    def launch(self, cwd: Path | str, **kwargs: object) -> Session:
        if self.launch_count + 1 >= self._unlaunchable_from:
            self.launch_count += 1
            raise Unlaunchable(f"provider {kwargs['provider']!r} is not installed")
        return super().launch(cwd, **kwargs)  # type: ignore[arg-type]


def test_usage_limit_repick_launch_unlaunchable_fails_with_the_refusal(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-316: the re-pick cannot launch — the worker that ran still gets its record."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    lines: list[str] = []
    worker = _UnlaunchableAfterWorker(lambda: _close_worker(repo), unlaunchable_from=2)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
        echo=lines.append,
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert "usage limit reached" in (result.summary or "")
    assert result.provider == "claude_code"
    assert result.exhausted_providers == ["claude_code"]
    assert len(store.list_runs("app")) == 1
    assert "usage-limit: re-pick codex cannot launch: provider 'codex' is not installed" in lines
    assert worker.events == ["launch:claude_code", "shutdown:claude_code"]


class _SendRefusesOnRepickWorker(_UsageLimitThenCloseWorker):
    """The re-pick's `launch` reserves a session, then `send` raises `UserError`."""

    def send(self, session: Session, message: str) -> None:
        if self.launch_count >= 2:
            raise UserError("message must not be empty")
        super().send(session, message)


def test_usage_limit_repick_send_usererror_retires_the_unrun_session(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-316 review: a reserved-but-unsent re-pick session is retired, not attributed."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _SendRefusesOnRepickWorker(lambda: _close_worker(repo))
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.provider == "claude_code"
    assert result.exhausted_providers == ["claude_code"]
    assert result.suite is None
    assert worker.events == [
        "launch:claude_code",
        "shutdown:claude_code",
        "launch:codex",
        "shutdown:codex",
    ]


def test_first_launch_unlaunchable_still_refuses_without_record(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-316: with no worker run yet there is nothing to record — exit 5 refusal."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UnlaunchableAfterWorker(lambda: _close_worker(repo), unlaunchable_from=1)
    with pytest.raises(Unlaunchable) as exc:
        _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            usage=_usage("claude_code", "codex"),
        )
    assert exc.value.exit_code == 5
    assert store.last_run("app") is None


# Session lifecycle (CBN-EPIC-244.4): characterization pins ahead of `.5`
# splitting `_run_gated`. Each launched session is shut down exactly once, a
# refused one before the next launch; `close()` only on a client the loop built.


def _failing_card(_store: Store, _run: object) -> Path:
    raise WorkerError("status card write failed: boom")


def test_lifecycle_repick_shuts_refused_session_before_next_launch(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo))
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    # Order, not count: the refused session goes before the next launch, and
    # the injected client is never closed by the loop.
    assert worker.events == [
        "launch:claude_code",
        "shutdown:claude_code",
        "launch:codex",
        "shutdown:codex",
    ]


def test_lifecycle_failed_run_shuts_its_session_once(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, provider="claude_code")
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), always=True)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.events == ["launch:claude_code", "shutdown:claude_code"]


def test_lifecycle_all_refuse_shuts_each_session_once(tmp_path: Path, caobunga_home: Path) -> None:
    """Every session is shut down on the re-pick path; `finally` finds none left."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), always=True)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert worker.events == [
        "launch:claude_code",
        "shutdown:claude_code",
        "launch:codex",
        "shutdown:codex",
    ]


def test_lifecycle_finish_raising_still_shuts_the_session(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code"])
    worker = _BusyWorker(lambda: _close_worker(repo), busy_polls=0)
    with pytest.raises(ReportError, match="status card write failed"):
        _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            usage=_usage("claude_code"),
            publish_status=_failing_card,
        )
    assert worker.events == ["launch:claude_code", "shutdown:claude_code"]


def test_lifecycle_shutdown_error_is_swallowed_on_both_paths(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """The re-pick shutdown and the `finally` shutdown both raise; the run still closes."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code", "codex"])
    worker = _UsageLimitThenCloseWorker(lambda: _close_worker(repo), shutdown_fails=True)
    result = _run(
        "app",
        store=store,
        worker=worker,
        poll_interval=0,
        timeout=2,
        usage=_usage("claude_code", "codex"),
    )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.provider == "codex"
    assert worker.events == [
        "launch:claude_code",
        "shutdown:claude_code",
        "launch:codex",
        "shutdown:codex",
    ]


@pytest.mark.parametrize("publish_status", [None, _failing_card])
def test_lifecycle_closes_a_client_the_loop_built(
    tmp_path: Path,
    caobunga_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    publish_status: Callable[[Store, object], Path] | None,
) -> None:
    """`worker=None` → `select_worker()`: closed after the shutdown, even when `finish` raises.

    Patched on `caobunga.local`, which `select_worker` imports late — not on
    `caobunga.loop`, so it adds nothing to `LOOP_PATCHED`.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, providers=["claude_code"])
    built = _BusyWorker(lambda: _close_worker(repo), busy_polls=0)
    monkeypatch.delenv("CAOBUNGA_WORKER", raising=False)
    monkeypatch.setattr("caobunga.local.LocalWorker", lambda: built)
    try:
        _run(
            "app",
            store=store,
            poll_interval=0,
            timeout=2,
            usage=_usage("claude_code"),
            publish_status=publish_status,
        )
    except ReportError:
        assert publish_status is _failing_card
    else:
        assert publish_status is None
    assert built.events == ["launch:claude_code", "shutdown:claude_code", "close"]


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
    with store.run_lock(holder, git_toplevel(Path(held.path)) or held.path):
        with pytest.raises(UserError, match=f"already active for {refused}'s repo") as exc:
            _run(refused, store=store, worker=fake, poll_interval=0)
        assert exc.value.exit_code == 2
    assert store.last_run(refused) is None
    assert fake.created is None


def test_git_toplevel_names_the_checkout_root(tmp_path: Path) -> None:
    """Root and subdirectory resolve to one toplevel; a non-repo is `None` (CBN-EPIC-313.6)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    nested = repo / "backend"
    nested.mkdir()
    assert git_toplevel(repo) == repo.resolve()
    assert git_toplevel(nested) == repo.resolve()
    outside = tmp_path / "plain"
    outside.mkdir()
    assert git_toplevel(outside) is None


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


def _dirty(repo: Path, name: str = "scratch.txt") -> None:
    (repo / name).write_text("operator edit\n", encoding="utf-8")


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
            "app", store=store, worker=worker, usage=_usage(), poll_interval=0, timeout=2
        )
    assert receipt.stop_reason == "head-lost", receipt.detail
    assert receipt.legacy_exit_code == 2
    assert any("partial work" in step for step in receipt.recovery)
    assert not any("CLI installation" in step for step in receipt.recovery)


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
    fake = FakeWorker()
    with fake as worker:
        with pytest.raises(UserError) as exc:
            _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    message = str(exc.value)
    assert exc.value.exit_code == 2
    assert "flaitron filing artifacts a dispatched worker writes" in message
    assert f"git -C {repo} add .flaitron" in message
    # Named, not exempted: the count and the porcelain line are still reported.
    assert "1 uncommitted change(s)" in message
    assert ".flaitron/PLAN.md" in message
    # Printed, never run — the tree is exactly as dirty as before (§Fleet boundary).
    assert fake.created is None
    assert store.last_run("app") is None
    assert git_dirt(repo) != []


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


def test_run_record_carries_the_worker_s_proposed_follow_ups(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-121: the one link between extraction and every reader that renders it."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    section = "### Follow-ups proposed\n\n- **CBN-999** | guard — the discovery.\n\n"

    def close(_message: str) -> None:
        _close_worker(repo, follow_ups=section)

    with _closing_client(fake, close) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.follow_ups == ["**CBN-999** | guard — the discovery."]
    # And it survives the round trip through disk, not only the in-memory record.
    persisted = store.last_run("app")
    assert persisted is not None
    assert persisted.follow_ups == ["**CBN-999** | guard — the discovery."]


def test_run_record_omits_follow_ups_when_the_worker_proposed_none(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(repo)

    with _closing_client(fake, close) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.follow_ups is None


# A row the worker files into `## Medium` mid-run — unmarked, as every
# worker-filed row is: caobunga never seeds and flaitron never writes the marker.
FILED_PLAN = CLOSED_PLAN.replace(
    "## Medium\n\n(none)",
    "## Medium\n\n- [ ] **CBN-014** [light] | worker-filed — filed mid-run.",
)


def test_run_record_carries_the_rows_the_worker_filed(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-138.3: the launch baseline reaches `inspect_close` and the diff reaches disk."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(repo, plan=FILED_PLAN)

    with _closing_client(fake, close) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.new_rows == [NewRow(task_id="CBN-014", unattended=False)]
    persisted = store.last_run("app")
    assert persisted is not None
    assert persisted.new_rows == [NewRow(task_id="CBN-014", unattended=False)]


def test_run_record_omits_new_rows_when_the_worker_filed_none(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def close(_message: str) -> None:
        _close_worker(repo)

    with _closing_client(fake, close) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.new_rows is None


def test_closed_run_carries_the_suite_and_saves_its_log(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-EPIC-202.3: the suite rides the single write at v2, the log pairs by stamp,
    and one `suite:` line is echoed. `gated` is the default tier here — every
    launching tier runs it; only the continue is `proceed-on-green`'s."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []
    seen: list[tuple[Path, Store]] = []

    def runner(root: Path, st: Store) -> SuiteRun:
        seen.append((root, st))
        return _green_suite(root, st)

    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            run_suite=runner,
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert seen == [(repo.resolve(), store)]
    assert result.schema_version == 2
    assert result.suite is not None
    assert result.suite.verdict.value == "green"
    assert [verb.verb for verb in result.suite.verbs] == ["test", "lint", "typecheck"]
    on_disk = store.last_run("app")
    assert on_disk is not None
    assert on_disk.suite == result.suite
    log = store.suite_file("app", compact_utc(result.closed_at), "CBN-011")
    assert log.read_text(encoding="utf-8") == "verdict: green\n\ntest: fake\n"
    assert log.stem == store.run_file(result).stem
    assert "suite: green — test 0 1.5s · lint 0 0.2s · typecheck 0 0.4s" in lines


def test_post_settle_tree_line_is_read_after_the_worker_stops(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-352: a rotation the worker writes after close detection (here during the
    suite) is on the record, and the session is shut down once, before the write."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    written_at_shutdown: list[bool] = []
    shutdown = fake.shutdown

    def recording_shutdown(session: Session) -> None:
        written_at_shutdown.append(store.last_run("app") is not None)
        shutdown(session)

    fake.shutdown = recording_shutdown  # type: ignore[method-assign]

    def runner(root: Path, st: Store) -> SuiteRun:
        (repo / ".flaitron" / "PLAN-ARCHIVE.md").write_text("rotated\n", encoding="utf-8")
        return _green_suite(root, st)

    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, run_suite=runner
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    summary = result.summary or ""
    assert "tree: 1 file(s) left (?? .flaitron/PLAN-ARCHIVE.md; no open tasknote)" in summary
    assert summary.count(" — tree: ") == 1
    assert written_at_shutdown == [False]
    assert len(fake.deleted) == 1


def _post_suite_run(
    tmp_path: Path, caobunga_home: Path, during_suite: Callable[[Path], None]
) -> tuple[RunRecord, Path]:
    """Close CBN-011, run `during_suite` on the repo inside the suite, return the record."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def runner(root: Path, st: Store) -> SuiteRun:
        during_suite(repo)
        return _green_suite(root, st)

    with _closing_client(FakeWorker(), lambda _m: _close_worker(repo)) as worker:
        result = _run(
            "app", store=store, worker=worker, poll_interval=0, timeout=2, run_suite=runner
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.suite is not None
    assert result.suite.verdict == "green"
    return result, repo


def test_post_suite_stacked_commit_is_foreign(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-356: a commit stacked on the close during the suite (a fleet repo's rotation)
    is on the record as foreign; the close keeps its attribution."""
    closes: list[str | None] = []

    def rotate(repo: Path) -> None:
        # A real rotation edits PLAN too, so the close walk sees this commit.
        closes.append(git_head(repo))
        plan = repo / ".flaitron" / "PLAN.md"
        plan.write_text(plan.read_text(encoding="utf-8") + "\n<!-- rotated -->\n", encoding="utf-8")
        (repo / ".flaitron" / "PLAN-ARCHIVE.md").write_text("rotated\n", encoding="utf-8")
        _commit(repo, "chore: rotate Completed")

    result, _repo = _post_suite_run(tmp_path, caobunga_home, rotate)
    assert result.commit_sha == closes[0]
    assert result.foreign_commits is not None
    assert [c.subject for c in result.foreign_commits] == ["chore: rotate Completed"]


def test_post_suite_amend_follows_the_close(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-356: an amend during the suite leaves the record's `commit_sha` on-branch."""
    result, repo = _post_suite_run(tmp_path, caobunga_home, _amend)
    assert result.commit_sha == git_head(repo)
    assert result.foreign_commits is None


def test_post_settle_refresh_keeps_the_mismatch_and_skips_free_text(tmp_path: Path) -> None:
    """CBN-352: only a folded tail is swapped — the CBN-328 mismatch ahead of it
    stays, and a summary that never folded is left alone even if it reads alike."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / "stray.txt").write_text("x\n", encoding="utf-8")
    exited = CloseSnapshot(
        outcome=Outcome.FAILED,
        escalation=Escalation.WORKER_EXITED,
        summary="exit 0 — park-reason present but status in-progress, not blocked"
        " — tree: 0 file(s) left (no open tasknote)",
    )
    fresh = finish.refresh_tree(exited, repo, "CBN-011")
    assert fresh.summary == (
        "exit 0 — park-reason present but status in-progress, not blocked"
        " — tree: 1 file(s) left (?? stray.txt; no open tasknote)"
    )
    parked = CloseSnapshot(
        outcome=Outcome.BLOCKED,
        escalation=Escalation.WORKER_PARKED,
        summary="blocked on X — tree: needs rework",
    )
    assert finish.refresh_tree(parked, repo, "CBN-011") is parked


def test_a_queued_suite_echoes_the_wait_live(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default suite is handed an `on_wait` that echoes before it blocks (CBN-310)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    lines: list[str] = []

    def queued(root: Path, st: Store, *, on_wait: Callable[[], None] | None = None) -> SuiteRun:
        assert on_wait is not None
        on_wait()
        return _green_suite(root, st)

    monkeypatch.setattr("caobunga.finish.run_fleet_suite", queued)
    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            run_suite=None,
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert lines.index("suite: waiting for suite.lock") < next(
        i for i, line in enumerate(lines) if line.startswith("suite: green")
    )


def test_parked_run_measures_no_suite(tmp_path: Path, caobunga_home: Path) -> None:
    """A park leaves no closed tree: the runner is never called, no field, no log."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    calls: list[Path] = []

    def runner(root: Path, st: Store) -> SuiteRun:
        calls.append(root)
        return _green_suite(root, st)

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked"), encoding="utf-8")

    lines: list[str] = []
    with _closing_client(fake, park) as worker:
        result = _run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            echo=lines.append,
            run_suite=runner,
        )
    assert result is not None
    assert result.outcome is Outcome.BLOCKED
    assert calls == []
    assert result.suite is None
    assert not store.suites_dir("app").exists()
    assert not any(line.startswith("suite:") for line in lines)


def test_default_runner_refuses_on_a_repo_without_just(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No injection → the real runner, which is a `refused` result on a bare tmp repo:
    a valid v2 record, no subprocess, and a stop `proceed-on-green` will read (D5)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: None)
    fake = FakeWorker()
    with _closing_client(fake, lambda _m: _close_worker(repo)) as worker:
        result = execute_run(
            "app",
            store=store,
            worker=worker,
            poll_interval=0,
            timeout=2,
            usage=_usage(),
        )
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.suite is not None
    assert result.suite.verdict.value == "refused"
    assert len(result.suite.verbs) == 3
    log = store.suite_file("app", compact_utc(result.closed_at), "CBN-011")
    assert "refused" in log.read_text(encoding="utf-8")


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

#: Names each importer takes `from caobunga.loop`, exact in both directions,
#: keyed by path relative to `backend/`.
LOOP_SURFACE = {
    # A leaf takes nothing back from `loop` (CBN-EPIC-363.3).
    "caobunga/launch.py": frozenset(),
    "caobunga/finish.py": frozenset(),
    "caobunga/run_echo.py": frozenset(),
    "caobunga/cli.py": frozenset({"execute_run"}),
    "tests/test_loop.py": frozenset({"execute_run"}),
}

#: Names each importer takes `from caobunga.prompt`, exact in both directions,
#: keyed by path relative to `backend/` (CBN-EPIC-232.3).
PROMPT_SURFACE = {
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
}

#: Names each importer takes `from caobunga.wait`, exact in both directions,
#: keyed by path relative to `backend/` (CBN-EPIC-232.5).
WAIT_SURFACE = {
    "caobunga/launch.py": frozenset({"is_usage_limit_line", "wait_for_close"}),
    "caobunga/finish.py": frozenset({"settle_worker"}),
    "caobunga/loop.py": frozenset({"DEFAULT_POLL", "SETTLE_GRACE"}),
    "tests/test_loop.py": frozenset({"settle_worker", "wait_for_close"}),
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
}

#: Names each importer takes `from caobunga.finish`, exact in both directions,
#: keyed by path relative to `backend/` (CBN-EPIC-363.4).
FINISH_SURFACE = {
    "caobunga/loop.py": frozenset({"finish", "refresh_tree", "settle_and_measure"}),
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
}

#: Module attributes `tests/test_loop.py` reaches through `monkeypatch.setattr`
#: rather than an import. A move must re-point the patch, not just the name.
#: `LOOP_PATCHED` emptied at `.6` when `send_hermes` left with the review;
#: `REPORT_PATCHED` emptied at CBN-258 when `send_hermes` was retired outright.
#: `settle_worker` added at CBN-EPIC-363.2 by the kwarg pass-through pin.
#: `LAUNCH_PATCHED` added at CBN-EPIC-363.3: the re-pick `git_head` read moved.
#: `FINISH_PATCHED` added at CBN-EPIC-363.4: `settle_worker` left `LOOP_PATCHED`
#: with the settle, and the queued-suite `run_fleet_suite` patch is pinned too.
#: `RUN_ECHO_PATCHED` added empty at CBN-EPIC-363.5: nothing patches the text.
LOOP_PATCHED: frozenset[str] = frozenset()
WAIT_PATCHED = frozenset({"inspect_close", "_start_watch"})
REPORT_PATCHED: frozenset[str] = frozenset()
LAUNCH_PATCHED = frozenset({"git_head"})
FINISH_PATCHED = frozenset({"run_fleet_suite", "settle_worker"})
RUN_ECHO_PATCHED: frozenset[str] = frozenset()


def _module_imports(importer: str, module: str) -> set[str]:
    """Names `<backend>/<importer>` imports from `module`, per its AST."""
    backend = Path(caobunga.__file__).parent.parent
    source = (backend / importer).read_text(encoding="utf-8")
    return {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def test_loop_surface_every_importer_takes_exactly_its_pinned_names() -> None:
    """An importer editing its own list is the half a module-side pin misses."""
    assert {
        importer: _module_imports(importer, "caobunga.loop") for importer in LOOP_SURFACE
    } == LOOP_SURFACE
    assert {
        importer: _module_imports(importer, "caobunga.prompt") for importer in PROMPT_SURFACE
    } == PROMPT_SURFACE
    assert {
        importer: _module_imports(importer, "caobunga.wait") for importer in WAIT_SURFACE
    } == WAIT_SURFACE
    assert {
        importer: _module_imports(importer, "caobunga.report") for importer in REPORT_SURFACE
    } == REPORT_SURFACE
    assert {
        importer: _module_imports(importer, "caobunga.launch") for importer in LAUNCH_SURFACE
    } == LAUNCH_SURFACE
    assert {
        importer: _module_imports(importer, "caobunga.finish") for importer in FINISH_SURFACE
    } == FINISH_SURFACE
    assert {
        importer: _module_imports(importer, "caobunga.run_echo") for importer in RUN_ECHO_SURFACE
    } == RUN_ECHO_SURFACE


def test_loop_surface_every_pinned_name_resolves_on_the_module() -> None:
    reached = set().union(*LOOP_SURFACE.values()) | LOOP_PATCHED
    for name in sorted(reached):
        assert hasattr(loop, name), name
    prompt_reached = set().union(*PROMPT_SURFACE.values())
    for name in sorted(prompt_reached):
        assert hasattr(prompt, name), name
    wait_reached = set().union(*WAIT_SURFACE.values()) | WAIT_PATCHED
    for name in sorted(wait_reached):
        assert hasattr(wait, name), name
    report_reached = set().union(*REPORT_SURFACE.values()) | REPORT_PATCHED
    for name in sorted(report_reached):
        assert hasattr(report, name), name
    launch_reached = set().union(*LAUNCH_SURFACE.values()) | LAUNCH_PATCHED
    for name in sorted(launch_reached):
        assert hasattr(launch, name), name
    finish_reached = set().union(*FINISH_SURFACE.values()) | FINISH_PATCHED
    for name in sorted(finish_reached):
        assert hasattr(finish, name), name
    run_echo_reached = set().union(*RUN_ECHO_SURFACE.values()) | RUN_ECHO_PATCHED
    for name in sorted(run_echo_reached):
        assert hasattr(run_echo, name), name


def test_start_watch_returns_none_with_nothing_to_watch(tmp_path: Path) -> None:
    """No `.flaitron` / `.git` → no observer, and `wait_for_close` falls back to `sleep`."""
    assert wait._start_watch(tmp_path, threading.Event()) is None


def test_wait_for_close_wakes_on_watchdog_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`poll_interval > 0` characterization: the observer, the wake, and the stop.

    Every other `wait_for_close` caller in this file passes `poll_interval=0`,
    which never starts the observer (CBN-013 chose "close on `send` so the first
    inspect hits"). Here the close lands from another thread while the loop is
    blocked in `wake.wait`; the elapsed bound is the assertion — without a
    working wake the loop sits out the whole `poll_interval` and only then polls.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    observers: list[object] = []
    real_start_watch = wait._start_watch

    def capturing(root: Path, wake: threading.Event) -> object:
        observer = real_start_watch(root, wake)
        observers.append(observer)
        return observer

    monkeypatch.setattr(wait, "_start_watch", capturing)
    slept: list[float] = []
    poll_interval = 8.0
    closer = threading.Timer(0.3, _close_worker, args=(repo,))
    session = Session(session_name="s", terminal_id="t", provider=None)
    began = time.monotonic()
    closer.start()
    try:
        snap = wait_for_close(
            _IdleWorker(),  # type: ignore[arg-type]
            session,
            repo,
            "CBN-011",
            start,
            timeout=poll_interval,
            poll_interval=poll_interval,
            sleep=slept.append,
        )
    finally:
        closer.join()
    elapsed = time.monotonic() - began
    assert snap.outcome is Outcome.CLOSED
    assert elapsed < poll_interval / 2, elapsed
    assert slept == []
    (observer,) = observers
    assert observer is not None
    assert not observer.is_alive()  # type: ignore[attr-defined]


# CBN-272: invocation receipts are independent of the last task's outcome.
def _invoke(project_id: str, store: Store, **kwargs):
    result = loop.execute_invocation(
        project_id,
        store=store,
        usage=_usage(),
        run_suite=kwargs.pop("run_suite", _green_suite),
        poll_interval=0,
        timeout=kwargs.pop("timeout", 2),
        **kwargs,
    )
    if result.tier is not ExecutionTier.OBSERVE:
        assert store.load_invocation(result.receipt_id) == result
        assert result.finished_at is not None
    return result


@pytest.mark.parametrize("tier", [ExecutionTier.GATED, ExecutionTier.PROCEED_ON_GREEN])
@pytest.mark.parametrize("verdict", ["green", "red", "timeout", "refused"])
def test_invocation_retains_closure_when_suite_stops(
    tmp_path: Path,
    caobunga_home: Path,
    tier: ExecutionTier,
    verdict: str,
) -> None:
    repo = _git_repo(
        tmp_path,
        OPEN_PLAN.replace(
            "- [ ] **CBN-012** [medium] [unattended] | cao-client — client work.", ""
        ),
    )
    store = Store(caobunga_home)
    _register(store, repo, tier=tier)
    worker = _closing_client(FakeWorker(), lambda _msg: _close_worker(repo, plan=EXHAUSTED_PLAN))
    result = _invoke(
        "app",
        store,
        worker=worker,
        run_suite=_green_suite if verdict == "green" else _not_green_suite(verdict),
    )
    run = store.last_run("app")
    assert run is not None
    assert result.run_records == [str(path) for path in store.runs_dir("app").glob("*.yaml")]
    assert run.invocation_id == result.invocation_id
    assert result.last_commit_sha == run.commit_sha == git_head(repo)
    assert result.legacy_exit_code == 0  # direct CLI compatibility
    assert result.successful is (verdict == "green")
    assert result.approved_scope_finished is (verdict == "green")
    assert result.stop_reason == (
        "suite" if verdict != "green" else "gated" if tier is ExecutionTier.GATED else "complete"
    )
    assert result.suite_log is not None and Path(result.suite_log).is_file()
    if verdict != "green":
        assert "test" in result.detail
        assert any("historical run record" in step for step in result.recovery)


@pytest.mark.parametrize("mode", ["empty", "unmarked", "blocked", "parked", "malformed"])
def test_invocation_empty_and_denied_scope(
    tmp_path: Path,
    caobunga_home: Path,
    mode: str,
) -> None:
    plan = OPEN_PLAN
    notes = None
    if mode == "empty":
        plan = TWO_CLOSED
    elif mode == "unmarked":
        plan = plan.replace(" [unattended]", "")
    elif mode == "blocked":
        plan = plan.replace("— reader work.", "— reader work. Blocked by [[X-001]].")
        plan = plan.replace("— client work.", "— client work. Blocked by [[X-001]].")
    elif mode == "parked":
        notes = {t: _note("blocked", "input-needed — access") for t in ("CBN-011", "CBN-012")}
    else:
        plan = "## High\n- [ ] **CBN-011** [!unattended] — malformed\n"
    repo = _git_repo(tmp_path, plan, notes)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    worker = FakeWorker()
    result = _invoke("app", store, worker=worker)
    assert not worker.launches
    assert not result.run_records
    assert result.legacy_exit_code == 3
    assert result.successful is (mode in {"empty", "unmarked"})
    assert result.approved_scope_finished is result.successful
    if mode == "parked":
        assert all("parked" in denial.reason for denial in result.denials)
    if mode == "malformed":
        assert result.unparsed_lines == [2]
        assert any("syntax" in step for step in result.recovery)


@pytest.mark.parametrize(
    "failure", ["dirty", "active", "provider", "disabled", "missing-plan", "no-git"]
)
def test_invocation_refusals_are_durable_without_run_records(
    tmp_path: Path,
    caobunga_home: Path,
    failure: str,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = FakeWorker()
    if failure == "dirty":
        (repo / "partial.py").write_text("partial\n")
    elif failure == "disabled":
        record = store.get("app").model_copy(update={"enabled": False})
        store._write_yaml(store.project_file("app"), record)
    elif failure == "missing-plan":
        (repo / ".flaitron/PLAN.md").unlink()
    elif failure == "no-git":
        repo = tmp_path / "plain"
        (repo / ".flaitron").mkdir(parents=True)
        (repo / ".flaitron/PLAN.md").write_text(OPEN_PLAN)
        _register(store, repo, slug="plain")
    if failure == "active":
        with store.run_lock("app", repo):
            result = _invoke("app", store, worker=worker)
    elif failure == "provider":
        result = loop.execute_invocation("app", store=store, worker=worker, usage=[])
        assert store.load_invocation(result.receipt_id) == result
    else:
        result = _invoke("plain" if failure == "no-git" else "app", store, worker=worker)
    assert not result.successful
    # No pickable provider is the machine's state, exit 5 (CBN-EPIC-313.5).
    assert result.legacy_exit_code == (5 if failure == "provider" else 2)
    assert not result.run_records
    assert not worker.launches
    assert result.recovery
    if failure == "dirty":
        assert result.stop_reason == "dirty-tree"
        assert result.tree and any("partial.py" in line for line in result.tree)
    if failure == "active":
        assert result.stop_reason == "active-run"
    if failure == "disabled":
        assert result.stop_reason == "disabled"


def test_invocation_invalid_timeout_env_stops_at_timeout_config(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_timeout()` raises after the `timeout-config` stamp, so the receipt names it."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    monkeypatch.setenv(loop.ENV_TIMEOUT, "soon")
    worker = FakeWorker()
    result = _invoke("app", store, worker=worker, timeout=None)
    assert result.stop_reason == "timeout-config"
    assert result.legacy_exit_code == 2
    assert not worker.launches


@pytest.mark.parametrize("adapter", ["run", "invocation"])
def test_public_kwargs_reach_their_consumers(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    """Both public adapters forward every knob to the seam that consumes it (CBN-EPIC-363.2)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    settles: list[dict[str, object]] = []
    suites: list[Path] = []
    published: list[str] = []
    ran: list[str] = []
    lines: list[str] = []
    monkeypatch.setattr("caobunga.finish.settle_worker", lambda *_a, **kw: settles.append(kw))

    def nap(_seconds: float) -> None:
        return None

    def suite(root: Path, suite_store: Store) -> SuiteRun:
        suites.append(root)
        return _green_suite(root, suite_store)

    def publish(_store: Store, run: RunRecord) -> Path:
        published.append(run.task_id)
        return store.status_file("app")

    entry = loop.execute_run if adapter == "run" else loop.execute_invocation
    with _closing_client(FakeWorker(), lambda _msg: _close_worker(repo)) as worker:
        entry(
            "app",
            store=store,
            worker=worker,
            poll_interval=0.001,
            timeout=2,
            sleep=nap,
            usage=_usage(),
            echo=lines.append,
            publish_status=publish,
            on_run=lambda run: ran.append(run.task_id),
            run_suite=suite,
            grace=0.25,
        )
    assert settles == [{"poll_interval": 0.001, "sleep": nap, "grace": 0.25}]
    assert suites == [repo]
    assert published == ["CBN-011"]
    assert ran == ["CBN-011"]
    # `suite:` is `finish`'s echo, so the caller's echo reached `RunDeps`, not just the receipt.
    assert any(line.startswith("suite:") for line in lines)


@pytest.mark.parametrize("project", ["missing", "../escape"])
def test_invocation_unknown_slug_is_accounted_safely(caobunga_home: Path, project: str) -> None:
    store = Store(caobunga_home)
    result = _invoke(project, store, worker=FakeWorker())
    assert result.project == project
    assert not result.successful
    assert result.stop_reason == "registry"
    assert result.legacy_exit_code == 2
    assert store.invocation_file(result.receipt_id).parent == caobunga_home / "invocations"


def test_invocation_echo_names_the_outcome_cause(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-287: `outcome` alone names no cause; the echoed line carries the receipt's detail."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    lines: list[str] = []
    result = _invoke(
        "app", store, worker=_ExitedWorker(1, "", "Stopped before closure"), echo=lines.append
    )
    assert result.stop_reason == "outcome"
    assert f"invocation: incomplete — outcome: {result.detail}; successful=false" in lines


def test_invocation_gated_stop_keeps_the_summary_without_a_commit(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-302: with no closure commit the summary is the only cause the operator gets."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    result = _invoke("app", store, worker=_ExitedWorker(1, "", "Stopped before closure"))
    (run,) = store.list_runs("app")
    assert run.commit_sha is None
    assert run.summary
    assert result.detail.endswith(f" — {run.summary}")


def test_invocation_outcome_stop_omits_the_summary_when_a_commit_closed_it(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-302: the `run:` dump already prints the summary; the receipt need not repeat it.

    A drain, because a gated committed green close finishes its scope (CBN-301).
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    lines: list[str] = []

    def close_without_tasknote(_msg: str) -> None:
        (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
        _commit(repo, "feat: CBN-011 — close")

    worker = _closing_client(FakeWorker(), close_without_tasknote)
    result = _invoke("app", store, worker=worker, echo=lines.append)
    (run,) = store.list_runs("app")
    assert run.outcome is Outcome.NO_TASKNOTE
    assert run.commit_sha is not None
    assert run.summary
    assert result.stop_reason == "outcome"
    assert run.summary not in result.detail
    assert f"invocation: incomplete — outcome: {result.detail}; successful=false" in lines
    # The committed close is task evidence even though the drain stopped on it (CBN-301).
    assert "CBN-011" not in result.remaining_tasks


@pytest.mark.parametrize("descoped", [False, True])
@pytest.mark.parametrize(
    ("verdict", "detail"),
    [("red", "test exit 1"), ("timeout", "test timeout"), ("refused", "test refused: no justfile")],
)
def test_drain_stops_suite_on_a_non_green_committed_closure_outcome(
    tmp_path: Path, caobunga_home: Path, descoped: bool, verdict: str, detail: str
) -> None:
    """CBN-304: a non-green de-scoped / no-tasknote close stops `suite`, not `outcome`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    lines: list[str] = []

    def close(_msg: str) -> None:
        if descoped:
            _close_worker(repo, "CBN-011", CLOSED_PLAN, descoped=True)
        else:
            (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
            _commit(repo, "feat: CBN-011 — close")

    result = _invoke(
        "app",
        store,
        worker=_closing_client(FakeWorker(), close),
        echo=lines.append,
        run_suite=_not_green_suite(verdict),
    )
    (run,) = store.list_runs("app")
    assert run.outcome is (Outcome.DE_SCOPED if descoped else Outcome.NO_TASKNOTE)
    assert result.stop_reason == "suite"
    assert f"drain: suite — CBN-011 suite {verdict} — {detail}" in lines
    assert any("Fix the failing" in step for step in result.recovery)


@pytest.mark.parametrize("descoped", [False, True])
@pytest.mark.parametrize("verdict", ["green", "red"])
def test_invocation_gated_committed_close_finishes_the_one_task_scope(
    tmp_path: Path, caobunga_home: Path, descoped: bool, verdict: str
) -> None:
    """CBN-301: a gated no-tasknote / de-scoped close with a green suite is a complete receipt."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def close(_msg: str) -> None:
        if descoped:
            _close_worker(repo, "CBN-011", CLOSED_PLAN, descoped=True)
        else:
            (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
            _commit(repo, "feat: CBN-011 — close")

    result = _invoke(
        "app",
        store,
        worker=_closing_client(FakeWorker(), close),
        run_suite=_green_suite if verdict == "green" else _not_green_suite(verdict),
    )
    (run,) = store.list_runs("app")
    assert run.outcome is (Outcome.DE_SCOPED if descoped else Outcome.NO_TASKNOTE)
    assert result.last_commit_sha == run.commit_sha is not None
    if verdict == "green":
        assert result.status == "complete"
        assert result.successful and result.approved_scope_finished
        assert result.stop_reason == "gated"
        assert result.remaining_tasks == []
        assert result.recovery == []
    else:
        assert result.status == "incomplete"
        assert not result.successful
        assert result.remaining_tasks == ["CBN-011"]
        # The suite stop, not `outcome` (CBN-304): the recovery names the suite fix.
        assert result.stop_reason == "suite"
        assert "test exit 1" in result.detail
        assert run.summary not in result.detail
        assert any("Fix the failing" in step for step in result.recovery)


def test_invocation_echo_tally_covers_every_task_the_drain_ran(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-295: a close then a park must not read as only the park."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    lines: list[str] = []

    def park() -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-012.md"
        note.write_text(_note("blocked", "destructive — sql"), encoding="utf-8")

    steps = iter([lambda: _close_worker(repo), park])
    worker = _closing_client(FakeWorker(), lambda _msg: next(steps)())
    _invoke("app", store, worker=worker, echo=lines.append)
    closed, parked = store.list_runs("app")
    assert closed.commit_sha is not None
    tally = f"ran 2: CBN-011 closed {closed.commit_sha[:8]}, CBN-012 blocked (destructive)"
    assert any(line.startswith("invocation: incomplete") and tally in line for line in lines)
    assert parked.task_id == "CBN-012"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_invocation_process_exit_without_committed_closure(
    tmp_path: Path,
    caobunga_home: Path,
    exit_code: int,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(
        exit_code,
        "",
        "Stopped before closure",
        on_send=lambda: (repo / "partial.py").write_text("partial\n"),
    )
    result = _invoke("app", store, worker=worker)
    assert not result.successful
    assert result.last_commit_sha is None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.legacy_exit_code == 4
    assert f"exit code {exit_code}" in result.detail
    assert any("Committed closure evidence is missing" in step for step in result.recovery)
    assert result.shutdown == "returned"


def test_invocation_worker_exited_on_clean_unmoved_tree_says_nothing_ran(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    """CBN-293: a clean exit that touched nothing has no partial work to resume."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(0, "", "Declined: out of scope", on_send=lambda: _write_log(worker))
    result = _invoke("app", store, worker=worker)
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.tree == []
    assert result.tasknote_path is None
    assert worker.log_path is not None
    assert any(
        "Nothing ran for CBN-011" in step and str(worker.log_path) in step
        for step in result.recovery
    )
    assert not any("Committed closure evidence is missing" in step for step in result.recovery)
    assert not any("resume partial work" in step for step in result.recovery)


def test_invocation_worker_exited_nothing_ran_without_a_log_names_the_summary(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    result = _invoke("app", store, worker=_ExitedWorker(0, "", "Declined: out of scope"))
    assert result.worker_log is None
    assert any(
        "Nothing ran for CBN-011" in step and "the escalation summary" in step
        for step in result.recovery
    )


def _write_log(worker: _BusyWorker) -> None:
    assert worker.log_path is not None
    worker.log_path.parent.mkdir(parents=True, exist_ok=True)
    worker.log_path.write_text("[stdout] declined\n")


def test_invocation_worker_error_on_clean_unmoved_tree_says_nothing_ran(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    """CBN-284: a provider refusal before any work is not 'resume partial work'."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = FakeWorker()
    worker.on_send = lambda _msg: setattr(worker, "state", STATUS_ERROR)
    result = _invoke("app", store, worker=worker)
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert result.tree == []
    assert any("Nothing ran for CBN-011" in step for step in result.recovery)
    assert not any("Committed closure evidence is missing" in step for step in result.recovery)


def _invocation_failure_worker(repo: Path, failure: str):
    worker = FakeWorker()
    kwargs = {}
    if failure == "launch":

        def boom(*_args, **_kwargs) -> Session:
            raise WorkerError("model access denied")

        worker.launch = boom
    elif failure == "park":

        def park(_msg):
            (repo / ".flaitron/tasknote/CBN-011.md").write_text(
                _note("blocked", "input-needed — grant access")
            )

        worker.on_send = park
    elif failure == "timeout":
        kwargs["timeout"] = 0.01
    elif failure in {"interrupt", "unexpected"}:

        def interrupt(_msg):
            (repo / "partial.py").write_text("partial\n")
            if failure == "interrupt":
                raise KeyboardInterrupt
            raise RuntimeError("unexpected callback failure")

        worker.on_send = interrupt
    else:
        worker.on_send = lambda _msg: _close_worker(repo)

        def broken_card(_store, _run):
            raise UserError("card disk error")

        kwargs["publish_status"] = broken_card
    return worker, kwargs


@pytest.mark.parametrize(
    "failure", ["launch", "park", "timeout", "interrupt", "unexpected", "report"]
)
def test_invocation_failure_recovery_and_evidence(
    tmp_path: Path,
    caobunga_home: Path,
    failure: str,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker, kwargs = _invocation_failure_worker(repo, failure)
    result = _invoke("app", store, worker=worker, **kwargs)
    assert not result.successful
    assert result.task_id == "CBN-011"
    assert result.recovery
    assert result.provider == "grok_cli" and result.model
    if failure == "launch":
        assert "model access denied" in result.detail
        assert any("model access" in step for step in result.recovery)
    elif failure == "park":
        assert result.park_reason == "input-needed — grant access"
        assert result.tasknote_status == "blocked"
        assert any("/ft-task CBN-011" in step for step in result.recovery)
    elif failure == "timeout":
        assert result.timeout_seconds == 0.01
        assert result.escalation_reason is Escalation.TIMEOUT
        assert result.shutdown == "returned"
        assert any("0.01s" in step for step in result.recovery)
    elif failure == "report":
        assert result.last_commit_sha == git_head(repo)
        assert len(result.run_records) == 1
        assert result.stop_reason == "report"
    else:
        assert result.tree and "?? partial.py" in result.tree
        assert result.shutdown == "returned"
        assert result.stop_reason == (
            "interrupted" if failure == "interrupt" else "orchestrator-error"
        )
        assert result.legacy_exit_code == (130 if failure == "interrupt" else 4)


def _press_ctrl_c() -> None:
    os.kill(os.getpid(), signal.SIGINT)


def _pressing_shutdown(worker: FakeWorker, presses: int) -> None:
    """`worker.shutdown` takes `presses` ^C before it returns (CBN-375)."""
    shutdown = worker.shutdown

    def pressed(session: Session) -> None:
        for _ in range(presses):
            _press_ctrl_c()
        shutdown(session)

    worker.shutdown = pressed


def _pressing_finish(monkeypatch: pytest.MonkeyPatch, presses: int) -> None:
    """`trace.finish`'s snapshot takes `presses` ^C before it reads (CBN-375)."""
    snapshot = InvocationTrace.snapshot

    def pressed(self: InvocationTrace) -> None:
        for _ in range(presses):
            _press_ctrl_c()
        snapshot(self)

    monkeypatch.setattr(InvocationTrace, "snapshot", pressed)


@pytest.mark.parametrize("second", ["shutdown", "finish"])
def test_repeat_interrupt_during_cleanup_still_writes_the_receipt(
    tmp_path: Path,
    caobunga_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    second: str,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: _press_ctrl_c())
    if second == "shutdown":
        _pressing_shutdown(worker, 1)
    else:
        _pressing_finish(monkeypatch, 1)
    before = signal.getsignal(signal.SIGINT)
    result = _invoke("app", store, worker=worker)
    assert result.status is InvocationStatus.INTERRUPTED
    assert result.stop_reason == "interrupted"
    assert result.legacy_exit_code == 130
    assert result.shutdown == "returned" and worker.deleted
    assert signal.getsignal(signal.SIGINT) is before
    err = capfd.readouterr().err
    assert loop.INTERRUPT_NOTICE in err
    assert "interrupt received again" in err


def test_third_interrupt_forces_the_exit(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: _press_ctrl_c())
    _pressing_shutdown(worker, 2)
    result = _invoke("app", store, worker=worker)
    # The third press aborted the shutdown, and the receipt still says so.
    assert worker.deleted == []
    assert result.shutdown == "failed"
    assert result.status is InvocationStatus.INCOMPLETE
    assert result.stop_reason == "interrupted"
    assert result.legacy_exit_code == 130


def test_third_interrupt_during_finish_escapes(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: _press_ctrl_c())
    _pressing_finish(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        loop.execute_invocation("app", store=store, worker=worker, usage=_usage(), timeout=2)


def test_first_interrupt_after_the_run_is_absorbed(
    tmp_path: Path,
    caobunga_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Once the run has ended, only the receipt write is left: a first press is
    absorbed, so the receipt the notice promises is written."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: _close_worker(repo))
    _pressing_finish(monkeypatch, 1)
    result = _invoke("app", store, worker=worker)
    assert result.successful
    assert loop.INTERRUPT_NOTICE in capfd.readouterr().err


def test_interrupt_guard_keeps_an_inherited_sig_ign() -> None:
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with loop._interrupt_guard():
            assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, previous)


def test_invocation_drain_retains_every_task_reference(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    closes = iter([lambda: _close_worker(repo), lambda: _close_worker(repo, "CBN-012", TWO_CLOSED)])
    worker = _closing_client(FakeWorker(), lambda _msg: next(closes)())
    lines: list[str] = []
    result = _invoke("app", store, worker=worker, echo=lines.append)
    assert result.successful and result.approved_scope_finished
    first, second = store.list_runs("app")
    assert first.commit_sha and second.commit_sha
    tally = f"ran 2: CBN-011 closed {first.commit_sha[:8]}, CBN-012 closed {second.commit_sha[:8]}"
    assert any(line.startswith("invocation: complete") and tally in line for line in lines)
    assert result.authorized_tasks == ["CBN-011", "CBN-012"]
    assert result.remaining_tasks == []
    assert result.run_records == [
        str(path) for path in sorted(store.runs_dir("app").glob("*.yaml"))
    ]
    assert len(result.run_records) == 2
    assert len(worker.launches) == 2


def test_invocation_unmarked_tail_does_not_fail_completed_authorized_scope(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    plan = OPEN_PLAN.replace("**CBN-012** [medium] [unattended]", "**CBN-012** [medium]")
    after = CLOSED_PLAN.replace("**CBN-012** [medium] [unattended]", "**CBN-012** [medium]")
    repo = _git_repo(tmp_path, plan)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    result = _invoke(
        "app",
        store,
        worker=_closing_client(FakeWorker(), lambda _msg: _close_worker(repo, plan=after)),
    )
    assert result.successful and result.approved_scope_finished
    assert result.authorized_tasks == ["CBN-011"]
    assert result.denials[0].task_id == "CBN-012"
    assert result.stop_reason == "incomplete"  # legacy whole-queue echo vocabulary


def test_invocation_refuses_unpinned_work_and_preserves_initial_scope(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    plan = OPEN_PLAN.replace("**CBN-012** [medium] [unattended]", "**CBN-012** [medium]")
    repo = _git_repo(tmp_path, plan)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    worker = _closing_client(FakeWorker(), lambda _msg: _close_worker(repo))
    result = _invoke("app", store, worker=worker)
    assert result.stop_reason == "unpinned"
    assert result.task_id == "CBN-012"
    assert not result.successful
    assert result.authorized_tasks == ["CBN-011"]
    assert len(worker.launches) == 1
    assert any("who changed" in step for step in result.recovery)


def test_invocation_observe_remains_read_only(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.OBSERVE)
    worker = FakeWorker()
    result = _invoke("app", store, worker=worker)
    assert result.status.value == "observed"
    assert not worker.launches
    assert not (caobunga_home / "invocations").exists()
    assert not store.list_runs("app")


def _dead_pid() -> int:
    """A pid that was a real process and is now reaped."""
    proc = subprocess.Popen(["true"])  # noqa: S607
    proc.wait()
    return proc.pid


def _stale_receipt(
    store: Store, repo: Path, *, pid: int | None, project: str = "app", path: str | None = None
) -> InvocationResult:
    """A `running` checkpoint as an uncatchable death leaves it (CBN-376).

    Started a day ahead of the fixture's `init` commit, so only a commit dated
    after that counts as having landed since.
    """
    started = utc_now() + timedelta(days=1)
    result = InvocationResult(
        invocation_id=compact_utc(started),
        receipt_id=f"{compact_utc(started)}-{os.urandom(16).hex()}",
        project=project,
        started_at=started,
        pid=pid,
        stop_reason="worker-start",
        path=path if path is not None else str(repo.resolve()),
        tier=ExecutionTier.GATED,
        task_id="CBN-011",
        authorized_tasks=["CBN-011"],
        remaining_tasks=["CBN-011"],
    )
    store.write_invocation(result)
    return result


def _commit_dated(repo: Path, message: str, when: str) -> None:
    _git(repo, "add", "-A")
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when},
    )


def test_stale_running_receipt_is_reconciled_to_abandoned(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-376: a dead run's receipt is rewritten and reported; the repo is only read."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stale = _stale_receipt(store, repo, pid=_dead_pid())
    (repo / "landed.py").write_text("landed\n")
    _commit_dated(repo, "feat: landed after the crash", (utc_now() + timedelta(days=2)).isoformat())
    (repo / ".flaitron/tasknote/CBN-011.md").write_text(
        "---\nstatus: in-progress\n---\n# CBN-011\n", encoding="utf-8"
    )
    (repo / "partial.py").write_text("partial\n")
    head, tree = git_head(repo), git_dirt(repo)
    lines: list[str] = []
    worker = FakeWorker()

    result = _invoke("app", store, worker=worker, echo=lines.append)

    # The dirty-tree refusal still stops this run, after the reconcile.
    assert result.stop_reason == "dirty-tree"
    assert not worker.launches
    assert (git_head(repo), git_dirt(repo)) == (head, tree)
    rewritten = store.load_invocation(stale.receipt_id)
    assert rewritten.status is InvocationStatus.ABANDONED
    assert rewritten.stop_reason == "process-gone"
    assert rewritten.finished_at is not None
    assert rewritten.legacy_exit_code is None
    assert not rewritten.successful and not rewritten.approved_scope_finished
    assert "last stage worker-start" in rewritten.detail
    assert "since started_at: 1 commit(s)" in rewritten.detail
    assert rewritten.head_sha == head
    assert rewritten.tree and any("partial.py" in line for line in rewritten.tree)
    assert rewritten.tasknote_status == "in-progress"
    assert any("may still be alive" in step for step in rewritten.recovery)
    stale_lines = [line for line in lines if line.startswith("stale-receipt")]
    assert stale_lines[0].startswith(f"stale-receipt: {stale.receipt_id} running since ")
    assert stale_lines[0].endswith("gone, last stage worker-start) → abandoned")
    assert "1 commit(s)" in stale_lines[1] and "feat: landed after the crash" in stale_lines[1]
    assert "partial.py" in stale_lines[2] and "(in-progress)" in stale_lines[2]
    assert stale_lines[3] == f"stale-receipt-record: {store.invocation_file(stale.receipt_id)}"
    # The new run's own receipt carries the pid the next reconcile reads.
    assert result.pid == os.getpid()


def test_stale_legacy_receipt_without_pid_reconciles_on_the_lock(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """A pre-CBN-376 receipt has no pid; holding the lock for its own path is the evidence."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stale = _stale_receipt(store, repo, pid=None)
    (repo / "partial.py").write_text("partial\n")
    lines: list[str] = []
    _invoke("app", store, worker=FakeWorker(), echo=lines.append)
    rewritten = store.load_invocation(stale.receipt_id)
    assert rewritten.status is InvocationStatus.ABANDONED
    assert "pid unrecorded" in rewritten.detail
    assert "0 commit(s) — nothing" in "\n".join(lines)
    assert "no open tasknote" in "\n".join(lines)


def test_stale_receipt_of_a_moved_slug_reads_its_own_repo(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """A dead pid is dead wherever the slug points now; evidence comes from the dead run's repo."""
    old = _git_repo(tmp_path / "old", OPEN_PLAN)
    new = _git_repo(tmp_path / "new", OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, new)
    stale = _stale_receipt(store, old, pid=_dead_pid())
    (new / "unrelated.py").write_text("unrelated\n")
    _commit_dated(new, "feat: unrelated", (utc_now() + timedelta(days=2)).isoformat())
    (new / "partial.py").write_text("partial\n")
    _invoke("app", store, worker=FakeWorker())
    rewritten = store.load_invocation(stale.receipt_id)
    assert rewritten.status is InvocationStatus.ABANDONED
    assert "since started_at: 0 commit(s)" in rewritten.detail
    assert rewritten.head_sha == git_head(old)


def test_stale_receipt_rewrite_failure_does_not_stop_the_run(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stale = _stale_receipt(store, repo, pid=_dead_pid())
    write = store.write_invocation

    def failing(result: InvocationResult) -> Path:
        if result.receipt_id == stale.receipt_id:
            raise OSError("disk full")
        return write(result)

    monkeypatch.setattr(store, "write_invocation", failing)
    (repo / "partial.py").write_text("partial\n")
    lines: list[str] = []
    result = _invoke("app", store, worker=FakeWorker(), echo=lines.append)
    assert result.stop_reason == "dirty-tree"
    assert f"stale-receipt: {stale.receipt_id} not rewritten: disk full" in lines
    assert store.load_invocation(stale.receipt_id) == stale


@pytest.mark.parametrize(
    "case", ["alive", "other-slug", "other-path", "terminal", "observe", "misnamed"]
)
def test_stale_reconcile_leaves_unproven_receipts_alone(
    tmp_path: Path, caobunga_home: Path, case: str
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    tier = ExecutionTier.OBSERVE if case == "observe" else ExecutionTier.GATED
    _register(store, repo, tier=tier)
    if case == "alive":
        stale = _stale_receipt(store, repo, pid=os.getppid())
    elif case == "other-slug":
        stale = _stale_receipt(store, repo, pid=_dead_pid(), project="other")
    elif case == "other-path":
        stale = _stale_receipt(store, repo, pid=None, path=str(tmp_path / "elsewhere"))
    else:
        stale = _stale_receipt(store, repo, pid=_dead_pid())
    if case == "terminal":
        stale = stale.model_copy(
            update={
                "status": InvocationStatus.REFUSED,
                "finished_at": utc_now(),
                "legacy_exit_code": 2,
            }
        )
        store.write_invocation(stale)
    misnamed = caobunga_home / "invocations" / "20261018T000000000Z-ab.yaml"
    if case == "misnamed":
        store.invocation_file(stale.receipt_id).rename(misnamed)
    (repo / "partial.py").write_text("partial\n")
    lines: list[str] = []
    _invoke("app", store, worker=FakeWorker(), echo=lines.append)
    if case == "misnamed":
        assert not store.invocation_file(stale.receipt_id).exists()
        assert store.load_invocation("20261018T000000000Z-ab") == stale
    else:
        assert store.load_invocation(stale.receipt_id) == stale
    assert not [line for line in lines if line.startswith("stale-receipt")]


def test_abandoned_receipt_has_a_finish_time_and_no_legacy_exit() -> None:
    trace_result = InvocationResult(
        invocation_id="20261018T000000000Z",
        receipt_id="20261018T000000000Z-ab",
        project="app",
        started_at=utc_now(),
    )
    data = trace_result.model_dump()
    with pytest.raises(ValueError, match="abandoned"):
        InvocationResult.model_validate({**data, "status": "abandoned"})
    with pytest.raises(ValueError, match="abandoned"):
        InvocationResult.model_validate(
            {**data, "status": "abandoned", "finished_at": utc_now(), "legacy_exit_code": 0}
        )
    InvocationResult.model_validate({**data, "status": "abandoned", "finished_at": utc_now()})


@pytest.mark.parametrize(
    ("tier", "stage"),
    [(ExecutionTier.OBSERVE, "observe"), (ExecutionTier.GATED, "project-read")],
)
def test_read_oserror_is_a_named_stop(
    tmp_path: Path,
    caobunga_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tier: ExecutionTier,
    stage: str,
) -> None:
    """CBN-326. The loop's direct reads are not under `wait.poll`: an `OSError` is a named stop."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=tier)

    def unreadable(_root: Path, **_kwargs: object) -> None:
        raise PermissionError("PLAN.md")

    monkeypatch.setattr("caobunga.loop.read_project", unreadable)
    result = _invoke("app", store, worker=FakeWorker())
    assert result.stop_reason == stage
    assert result.legacy_exit_code == 2
    assert "cannot read the Flaitron project" in result.detail
    with pytest.raises(UserError, match="cannot read the Flaitron project"):
        _run("app", store=store, worker=FakeWorker(), poll_interval=0, timeout=2)


def test_read_oserror_between_drained_tasks_keeps_the_run_record(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo, tier=ExecutionTier.PROCEED_ON_GREEN)
    reads: list[Path] = []

    def read_then_unreadable(root: Path, **kwargs: bool) -> ProjectRead:
        reads.append(root)
        if len(reads) > 1:
            raise PermissionError("PLAN.md")
        return read_project(root, **kwargs)

    monkeypatch.setattr("caobunga.loop.read_project", read_then_unreadable)
    worker = _closing_client(FakeWorker(), lambda _msg: _close_worker(repo))
    result = _invoke("app", store, worker=worker)
    assert result.status.value == "incomplete"
    assert result.stop_reason == "project-read"
    assert result.legacy_exit_code == 2
    assert len(result.run_records) == 1
    assert len(worker.launches) == 1


@pytest.mark.parametrize("repick", [False, True])
def test_invocation_shutdown_failure_survives_later_success(
    tmp_path: Path,
    caobunga_home: Path,
    repick: bool,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(
        store, repo, tier=ExecutionTier.PROCEED_ON_GREEN, providers=["claude_code", "grok_cli"]
    )
    closes = iter([lambda: _close_worker(repo), lambda: _close_worker(repo, "CBN-012", TWO_CLOSED)])
    if repick:
        worker = _UsageLimitThenCloseWorker(lambda: next(closes)())
    else:
        worker = _closing_client(FakeWorker(), lambda _msg: next(closes)())
    shutdown = worker.shutdown
    failed_sessions = []

    def fail_first(session: Session) -> None:
        shutdown(session)
        if not failed_sessions:
            failed_sessions.append(session.session_name)
            raise WorkerError("first shutdown failed")

    worker.shutdown = fail_first
    result = _invoke("app", store, worker=worker)
    assert len(result.run_records) == 2  # legacy dispatch continues as before
    assert result.remaining_tasks == []
    assert not result.successful
    assert result.shutdown == "failed"
    assert any(failed_sessions[0] in step for step in result.recovery)
    assert result.legacy_exit_code == 0


def test_invocation_uncommitted_plan_archive_is_not_closure(
    tmp_path: Path,
    caobunga_home: Path,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = FakeWorker()

    def paper_close(_msg: str) -> None:
        (repo / ".flaitron/PLAN.md").write_text(CLOSED_PLAN)
        _archive_note(repo, "CBN-011")
        worker.state = STATUS_EXITED

    worker.on_send = paper_close
    before = git_head(repo)
    result = _invoke("app", store, worker=worker)
    assert not result.successful
    assert result.head_sha == before
    assert result.last_commit_sha is None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.tree
    assert any("Committed closure evidence is missing" in step for step in result.recovery)


def test_invocation_park_commit_is_not_a_closure_sha(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    before = git_head(repo)

    def commit_park(_msg: str) -> None:
        (repo / ".flaitron/tasknote/CBN-011.md").write_text(
            _note("blocked", "input-needed — access")
        )
        _commit(repo, "chore: preserve parked work")

    worker = _closing_client(FakeWorker(), commit_park)
    result = _invoke("app", store, worker=worker)
    run = store.last_run("app")
    assert run is not None and run.outcome is Outcome.BLOCKED
    assert run.commit_sha == result.head_sha != before
    assert result.last_commit_sha is None
    assert not result.successful
    assert any("/ft-task CBN-011" in step for step in result.recovery)


@pytest.mark.parametrize(
    ("verdict", "confirm_and_close"), [("De-scope", True), ("Re-scope", False)]
)
def test_invocation_descope_park_recovers_by_confirm_and_close(
    tmp_path: Path, caobunga_home: Path, verdict: str, confirm_and_close: bool
) -> None:
    """CBN-294: a De-scope park has nothing to resume; any other park still resumes."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = FakeWorker()

    def park(_msg: str) -> None:
        note = _note("blocked", "drift — already delivered elsewhere")
        (repo / ".flaitron/tasknote/CBN-011.md").write_text(f"{note}\n**Verdict:** {verdict}\n")

    worker.on_send = park
    result = _invoke("app", store, worker=worker)
    assert result.park_reason == "drift — already delivered elsewhere"
    steps = " ".join(result.recovery)
    assert ("confirm and close the row" in steps) is confirm_and_close
    assert ("resume CBN-011 attended with /ft-task CBN-011" in steps) is not confirm_and_close


@pytest.mark.parametrize("unreadable", ["none", "missing", "not-utf8"])
def test_parked_descoped_is_false_without_a_readable_note(tmp_path: Path, unreadable: str) -> None:
    """CBN-294: no readable tasknote falls back to the generic resume text."""
    run = RunRecord(
        project="app",
        task_id="CBN-011",
        outcome=Outcome.BLOCKED,
        closed_at=utc_now(),
        summary="parked",
    )
    path = None if unreadable == "none" else str(tmp_path / "CBN-011.md")
    if unreadable == "not-utf8":
        assert path is not None
        Path(path).write_bytes(b"\xff\xfe**Verdict:** De-scope")
    assert _parked_descoped(run, path) is False


def test_invocation_worker_exited_with_moved_head_keeps_closure_missing_text(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-293: a commit means work happened, even when the tree is clean."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)

    def commit_unrelated() -> None:
        (repo / "other.py").write_text("x\n")
        _commit(repo, "chore: unrelated")

    result = _invoke("app", store, worker=_ExitedWorker(0, "", "", on_send=commit_unrelated))
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.tree == []
    assert any("Committed closure evidence is missing" in step for step in result.recovery)
    assert not any("Nothing ran" in step for step in result.recovery)


def test_invocation_worker_exited_with_open_tasknote_keeps_closure_missing_text(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-293: an open tasknote is partial work to resume, even on a clean unmoved tree."""
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _note("in-progress")})
    store = Store(caobunga_home)
    _register(store, repo)
    result = _invoke("app", store, worker=_ExitedWorker(0, "", ""))
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.tree == []
    assert result.tasknote_path is not None
    assert any("Committed closure evidence is missing" in step for step in result.recovery)
    assert not any("Nothing ran" in step for step in result.recovery)
