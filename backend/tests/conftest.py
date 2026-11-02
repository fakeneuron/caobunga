"""Shared pytest fixtures and test helpers. See Fleet HQ's docs/CODING-STANDARDS.md §Testing."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from caobunga import pins as pins_mod
from caobunga import usage as usage_mod
from caobunga.errors import UserError, WorkerError
from caobunga.flaitron import FT_TASK_SKILL_DIR_REL, UNATTENDED_MODULE
from caobunga.loop import execute_run
from caobunga.models import ExecutionTier, RegistryRecord, Suite, SuiteVerb, utc_now
from caobunga.store import Store
from caobunga.suite import SuiteRun
from caobunga.text import normalize_terminal_line
from caobunga.usage import Leftover, ProviderUsage, RunFn, WhichFn
from caobunga.worker import (
    STATUS_ERROR,
    STATUS_EXITED,
    STATUS_PROCESSING,
    Session,
    resolve_cwd,
    safe_session_name,
)
from main import app

_REAL_PROBE_ALL = usage_mod.probe_all
_REAL_PINS_CHECK_ALL = pins_mod.check_all


@pytest.fixture(autouse=True)
def _block_live_probe_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test reaches live ``usage.probe_all()`` without injection.

    ``_run()`` injects ``usage=`` by convention; this makes hermeticity
    structural. Passthrough when ``which=`` or ``run=`` is supplied so
    ``test_usage`` can still exercise the real library. Patch both the
    usage module and picker's bound import (``from … import probe_all``).
    """

    def guarded_probe_all(
        *,
        which: WhichFn | None = None,
        run: RunFn | None = None,
        timeout: float = usage_mod.DEFAULT_TIMEOUT,
    ) -> list[ProviderUsage]:
        if which is None and run is None:
            msg = (
                "test reached live usage.probe_all(); "
                "inject usage= on pick/execute_run, or which=/run= on probe_all"
            )
            raise RuntimeError(msg)
        return _REAL_PROBE_ALL(which=which, run=run, timeout=timeout)

    monkeypatch.setattr(usage_mod, "probe_all", guarded_probe_all)
    monkeypatch.setattr("caobunga.picker.probe_all", guarded_probe_all)


@pytest.fixture(autouse=True)
def _stub_live_pins_check_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `status <project>` / `digest` tests off the live model catalogs (CBN-256).

    Uninjected ``pins.check_all()`` would shell out to ``grok models`` /
    ``cursor-agent models`` (network, 10s+) on any host that has them. It
    returns no reports instead; passthrough when ``which=`` or ``run=`` is
    supplied, mirroring ``_block_live_probe_all``. (``test_pins`` imports
    ``check_all`` by name, so it holds the real function regardless.)
    """

    def stubbed_check_all(
        *,
        which: pins_mod.WhichFn | None = None,
        run: pins_mod.RunFn | None = None,
        timeout: float = pins_mod.CATALOG_TIMEOUT,
    ) -> list[pins_mod.PinReport]:
        if which is None and run is None:
            return []
        return _REAL_PINS_CHECK_ALL(which=which, run=run, timeout=timeout)

    monkeypatch.setattr(pins_mod, "check_all", stubbed_check_all)


@pytest.fixture
def client() -> TestClient:
    """A TestClient bound to the FastAPI app — use it to exercise routes."""
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def caobunga_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the on-disk store from the real ~/.caobunga/."""
    home = tmp_path / "caobunga-home"
    home.mkdir()
    monkeypatch.setenv("CAOBUNGA_HOME", str(home))
    return home


# --- Run-loop test helpers (CBN-EPIC-383.3) ---------------------------------
# Moved from `test_loop.py` when it split: the plans, tmp-repo builders, and
# worker fakes more than one test module uses, plus the helpers those call
# (`_dirty`, `_tail`). Test modules import them by name
# (`from conftest import _git_repo`); file-local helpers stay with their file.

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
    (skill_dir / UNATTENDED_MODULE).write_text(
        "unattended posture\n4. **Lone-park commit.**\n", encoding="utf-8"
    )
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

    def activity(self, session: Session) -> tuple[int, str | None]:  # noqa: ARG002
        return (0, None)

    def shutdown(self, session: Session) -> None:
        self.deleted.append(session.session_name)

    def close(self) -> None:
        self.closed = True


def _closing_client(fake: FakeWorker, on_send: Callable[[str], None]) -> FakeWorker:
    """`fake` with `on_send` wired — the hook that closes the task (or flips state)."""
    fake.on_send = on_send
    return fake


def _not_green_suite(
    verdict: str, *, baseline_green: bool = True
) -> Callable[[Path, Store], SuiteRun]:
    """A suite runner whose `test` verb is `red` / `timeout` / `refused` (CBN-EPIC-202.4).

    Its first call — the pre-launch baseline (CBN-420) — is green unless
    `baseline_green` is false, so a post-close red still reaches the close.
    """
    if verdict == "red":
        test = SuiteVerb(verb="test", exit_code=1, duration=1.5, detail="1 failed")
    elif verdict == "timeout":
        test = SuiteVerb(verb="test", duration=600.0, timed_out=True)
    else:
        test = SuiteVerb(verb="test", duration=0.0, refused="no justfile")
    calls = 0

    def runner(repo: Path, store: Store) -> SuiteRun:
        nonlocal calls
        calls += 1
        if baseline_green and calls == 1:
            return _green_suite(repo, store)
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

    def activity(self, session: Session) -> tuple[int, str | None]:  # noqa: ARG002
        return (0, None)

    def shutdown(self, session: Session) -> None:
        self.events.append(f"shutdown:{session.provider}")

    def close(self) -> None:
        self.events.append("close")


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


def _tail(text: str | None) -> tuple[str, ...]:
    """A fake's `*_tail`: the non-empty lines of `text`, as `LocalWorker` keeps them."""
    return tuple(
        plain for line in (text or "").splitlines() if (plain := normalize_terminal_line(line))
    )


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


def _dirty(repo: Path, name: str = "scratch.txt") -> None:
    (repo / name).write_text("operator edit\n", encoding="utf-8")
