"""Worktree bootstrap and readiness, against real temp git repos (CBN-EPIC-450.3)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from conftest import _commit, _git
from test_worktree import _head, _repo, _with_submodule, allow_file_submodules  # noqa: F401

from caobunga.bootstrap import (
    DEFAULT_TIMEOUT,
    BootstrapResult,
    refuse_unready,
    run_bootstrap,
)
from caobunga.errors import GitUnreadable, UserError
from caobunga.models import RegistryRecord, SuiteVerdict
from caobunga.store import Store
from caobunga.suite import run_suite
from caobunga.suite_verbs import TAIL_CHARS, run_verb
from caobunga.worktree import WorktreeContext, origin, start

TASK = "CBN-EPIC-9.3"


def _py(code: str) -> list[str]:
    """An argv that runs `code` with this interpreter, so no shell is involved."""
    return [sys.executable, "-c", code]


def _record(
    repo: Path, argv: list[str] | None = None, timeout: float | None = None
) -> RegistryRecord:
    return RegistryRecord(
        id="app", path=str(repo.resolve()), bootstrap=argv, bootstrap_timeout=timeout
    )


def _worktree(tmp_path: Path, *, ignore: str = "deps/\n") -> tuple[Path, WorktreeContext]:
    """A repo that ignores `deps/`, holds an installed ignored tree, and a fresh worktree."""
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text(ignore, encoding="utf-8")
    (repo / "justfile").write_text("test:\n    echo hi\n", encoding="utf-8")
    _commit(repo, "ignore deps")
    (repo / "deps").mkdir()
    (repo / "deps" / "installed.txt").write_text("main's own install\n", encoding="utf-8")
    return repo, start(repo, TASK, _head(repo))


# --- running ---


def test_runs_argv_at_the_execution_root_without_a_shell(tmp_path: Path) -> None:
    _repo_path, ctx = _worktree(tmp_path)
    code = (
        "import os, pathlib; pathlib.Path('deps').mkdir(); "
        "print('cwd', os.getcwd()); print('$HOME')"
    )
    result = run_bootstrap(ctx, _record(_repo_path, _py(code)))
    assert result is not None
    assert result.ok, result.render()
    assert result.cwd == ctx.execution_root
    assert f"cwd {ctx.execution_root}" in result.output
    # No shell: `$HOME` reaches the program as the literal string.
    assert "$HOME" in result.output
    assert "result: ok" in result.render()
    refuse_unready(ctx, result)


def test_runs_with_the_default_timeout_when_none_is_registered(tmp_path: Path) -> None:
    repo, ctx = _worktree(tmp_path)
    seen: list[float] = []

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        seen.append(timeout)
        return 0, ""

    run_bootstrap(ctx, _record(repo, ["just", "setup"]), run=run)
    run_bootstrap(ctx, _record(repo, ["just", "setup"], timeout=42.0), run=run)
    assert seen == [DEFAULT_TIMEOUT, 42.0]


def test_runs_nested_registration_at_its_subpath(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ctx = start(repo / "backend", TASK, _head(repo))
    result = run_bootstrap(ctx, _record(repo / "backend", _py("print(open('app.py').read())")))
    assert result is not None
    assert result.ok, result.render()
    assert result.cwd == ctx.worker_root / "backend"


@pytest.mark.usefixtures("allow_file_submodules")
def test_runs_after_submodules_are_checked_out(tmp_path: Path) -> None:
    repo, _core, _pinned = _with_submodule(tmp_path)
    ctx = start(repo, TASK, _head(repo))
    code = "import sys; sys.exit(open('.flaitron/core/SPEC.md').read() != 'pinned\\n')"
    result = run_bootstrap(ctx, _record(repo, _py(code)))
    assert result is not None
    assert result.ok, result.render()
    refuse_unready(ctx, result)


def test_cold_worktree_has_no_copied_ignored_tree_and_bootstrap_fills_its_own(
    tmp_path: Path,
) -> None:
    repo, ctx = _worktree(tmp_path)
    assert (repo / "deps" / "installed.txt").is_file()
    assert not (ctx.worker_root / "deps").exists()
    code = "import pathlib; p = pathlib.Path('deps'); p.mkdir(); (p / 'own.txt').write_text('x')"
    result = run_bootstrap(ctx, _record(repo, _py(code)))
    assert result is not None
    assert result.ok, result.render()
    refuse_unready(ctx, result)
    assert (ctx.worker_root / "deps" / "own.txt").is_file()
    assert not (ctx.worker_root / "deps" / "installed.txt").exists()
    # And main's own install is untouched by it.
    assert sorted(p.name for p in (repo / "deps").iterdir()) == ["installed.txt"]


def test_output_is_tailed_from_the_front(tmp_path: Path) -> None:
    repo, ctx = _worktree(tmp_path)
    result = run_bootstrap(ctx, _record(repo, ["x"]), run=lambda *_: (0, "a" * TAIL_CHARS + "END"))
    assert result is not None
    assert result.truncated
    assert result.output.endswith("END")
    assert len(result.output) == TAIL_CHARS
    assert "head dropped" in result.render()


# --- absent configuration ---


def test_absent_bootstrap_runs_nothing_and_a_clean_worktree_is_ready(tmp_path: Path) -> None:
    repo, ctx = _worktree(tmp_path)

    def run(*_: object) -> tuple[int, str]:
        raise AssertionError("nothing is registered, so nothing runs")

    assert run_bootstrap(ctx, _record(repo), run=run) is None
    refuse_unready(ctx, None)


def test_absent_bootstrap_still_refuses_dirt(tmp_path: Path) -> None:
    _repo_path, ctx = _worktree(tmp_path)
    (ctx.worker_root / "stray.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(UserError) as exc:
        refuse_unready(ctx, None)
    message = str(exc.value)
    assert "1 uncommitted change(s)" in message
    assert "with no bootstrap registered" in message
    assert "stray.txt" in message


# --- refusals ---


def _assert_retained(message: str, ctx: WorktreeContext) -> None:
    assert f"Retained, not removed: branch {ctx.branch} and worktree {ctx.worker_root}" in message
    assert ctx.worker_root.is_dir()
    refs = subprocess.run(  # noqa: S603
        ["git", "-C", str(ctx.landing_root), "branch", "--list", ctx.branch],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ctx.branch in refs


def test_refuse_nonzero_exit_names_the_retained_work_not_the_output(tmp_path: Path) -> None:
    repo, ctx = _worktree(tmp_path)
    # Built at run time, so the argv echoed in the refusal does not carry it.
    code = "import sys; print('SECRET-ISH' + ' OUTPUT'); sys.exit(3)"
    result = run_bootstrap(ctx, _record(repo, _py(code)))
    assert result is not None
    assert result.failure == "exited 3"
    assert "SECRET-ISH OUTPUT" in result.render()
    with pytest.raises(UserError) as exc:
        refuse_unready(ctx, result)
    message = str(exc.value)
    assert "exited 3" in message
    assert "not launching" in message
    assert "SECRET-ISH OUTPUT" not in message
    _assert_retained(message, ctx)


def test_refuse_on_timeout_kills_and_reports(tmp_path: Path) -> None:
    repo, ctx = _worktree(tmp_path)
    code = "import time; print('started', flush=True); time.sleep(30)"
    result = run_bootstrap(ctx, _record(repo, _py(code), timeout=0.5))
    assert result is not None
    assert result.timed_out
    assert result.duration < 15
    assert "started" in result.output
    assert result.failure is not None
    assert result.failure.startswith("timed out after")
    with pytest.raises(UserError) as exc:
        refuse_unready(ctx, result)
    _assert_retained(str(exc.value), ctx)


def test_refuse_when_the_command_cannot_start(tmp_path: Path) -> None:
    repo, ctx = _worktree(tmp_path)
    result = run_bootstrap(ctx, _record(repo, [str(tmp_path / "no-such-tool")]))
    assert result is not None
    assert result.error is not None
    assert result.exit_code is None
    with pytest.raises(UserError) as exc:
        refuse_unready(ctx, result)
    assert "could not start" in str(exc.value)
    _assert_retained(str(exc.value), ctx)


def test_refuse_dirt_a_bootstrap_leaves_outside_ignored_paths(tmp_path: Path) -> None:
    repo, ctx = _worktree(tmp_path)
    code = (
        "import pathlib\n"
        "for i in range(7): pathlib.Path(f'lock{i}.txt').write_text('x')\n"
        "pathlib.Path('backend/app.py').write_text('changed')"
    )
    result = run_bootstrap(ctx, _record(repo, _py(code)))
    assert result is not None
    assert result.ok
    with pytest.raises(UserError) as exc:
        refuse_unready(ctx, result)
    message = str(exc.value)
    assert "8 uncommitted change(s)" in message
    assert "after bootstrap" in message
    assert "(+3 more)" in message
    assert "a bootstrap may write only ignored paths" in message
    _assert_retained(message, ctx)


def test_unready_refuses_when_git_status_answers_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo_path, ctx = _worktree(tmp_path)
    monkeypatch.setattr("caobunga.bootstrap.git_porcelain", lambda _root: None)
    with pytest.raises(UserError) as exc:
        refuse_unready(ctx, None)
    assert "cannot read git status" in str(exc.value)


def test_unready_hung_git_is_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo_path, ctx = _worktree(tmp_path)

    def hang(*_: object, **__: object) -> object:
        raise subprocess.TimeoutExpired("git", 30)

    monkeypatch.setattr("caobunga.git.subprocess.run", hang)
    with pytest.raises(GitUnreadable):
        refuse_unready(ctx, None)


def test_result_failure_reads() -> None:
    base = {"argv": ("x",), "cwd": Path("/r"), "duration": 1.0, "output": ""}
    assert BootstrapResult(exit_code=0, **base).failure is None
    assert BootstrapResult(exit_code=2, **base).failure == "exited 2"
    assert BootstrapResult(exit_code=None, timed_out=True, **base).failure == "timed out after 1s"
    gone = BootstrapResult(exit_code=None, error="nope", **base)
    assert gone.failure == "could not start: nope"
    assert "(no output)" in gone.render()
    # Shown as the argv that ran, quoted so a paste runs the same thing.
    quoted = BootstrapResult(**{**base, "argv": ("sh", "-c", "just setup && npm ci")}, exit_code=0)
    assert "`sh -c 'just setup && npm ci'`" in quoted.render()


# --- the fresh-checkout baseline ---


def _just(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")


def _ok(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
    return 0, f"ran in {cwd}\n"


def test_suite_runner_accepts_a_registered_projects_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, ctx = _worktree(tmp_path)
    store = Store(tmp_path / "home")
    store.register(_record(repo))
    _just(monkeypatch)
    result = run_verb("run_tests", str(ctx.execution_root), store=store, run=_ok)
    assert result.refused is None
    assert result.exit_code == 0
    assert result.cwd == ctx.execution_root.resolve()


def test_suite_runner_accepts_a_nested_registrations_worktree_subpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (repo / "backend" / "justfile").write_text("test:\n    echo hi\n", encoding="utf-8")
    _commit(repo, "nested justfile")
    store = Store(tmp_path / "home")
    store.register(_record(repo / "backend"))
    ctx = start(repo / "backend", TASK, _head(repo))
    _just(monkeypatch)
    assert run_verb("run_tests", str(ctx.execution_root), store=store, run=_ok).refused is None
    # The worktree root is not where the registration points, so it stays refused.
    root = run_verb("run_tests", str(ctx.worker_root), store=store, run=_ok)
    assert root.refused is not None
    assert "not a registered caobunga project" in root.refused


def test_suite_runner_refuses_an_unregistered_projects_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo_path, ctx = _worktree(tmp_path)
    _just(monkeypatch)
    result = run_verb("run_tests", str(ctx.execution_root), store=Store(tmp_path / "home"), run=_ok)
    assert result.refused == f"not a registered caobunga project: {ctx.execution_root.resolve()}"


def test_suite_runner_refuses_a_linked_worktree_that_is_not_a_dispatch_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ctx = _worktree(tmp_path)
    store = Store(tmp_path / "home")
    store.register(_record(repo))
    _just(monkeypatch)
    shapes = (
        ("elsewhere", "side"),
        ("repo-worktrees/feature", "feature"),
        # At the reserved parent with the prefix, but not a task id.
        ("repo-worktrees/wt-feature", "wt-feature"),
        # Named like a dispatch worktree, but not at `<landing>-worktrees/`.
        ("elsewhere-worktrees/wt-CBN-EPIC-9.4", "wt-CBN-EPIC-9.4"),
    )
    for name, branch in shapes:
        other = tmp_path / name
        _git(repo, "worktree", "add", "-b", branch, str(other))
        result = run_verb("run_tests", str(other), store=store, run=_ok)
        assert result.refused is not None, name
        assert "not a registered caobunga project" in result.refused


def test_worktree_origin_is_none_when_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo_path, ctx = _worktree(tmp_path)

    def unreadable(root: Path) -> None:
        raise GitUnreadable(root)

    monkeypatch.setattr("caobunga.worktree.repo_identity", unreadable)
    assert origin(ctx.execution_root) is None


def test_red_baseline_on_the_worktree_root_is_measured_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CBN-420 gate's input: the suite runs on the worktree root and folds `red`.

    `loop._refuse_if_red_baseline` refuses on any non-green verdict (test_loop's
    CBN-420 tests); before this child a worktree root folded `refused` instead,
    since its path was never registered.
    """
    repo, ctx = _worktree(tmp_path)
    store = Store(tmp_path / "home")
    store.register(_record(repo))
    _just(monkeypatch)
    seen: list[Path] = []

    def red(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        seen.append(cwd)
        return (1, "1 failed\n") if argv[-1] == "test" else (0, "")

    measured = run_suite(ctx.execution_root, store, run=red)
    assert measured.suite.verdict is SuiteVerdict.RED
    assert all(verb.refused is None for verb in measured.suite.verbs)
    assert seen == [ctx.execution_root.resolve()] * 3
