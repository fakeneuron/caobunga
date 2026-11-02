"""Suite verb runner (CBN-174). Injected runner and registry; no live `just`."""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from caobunga.models import RegistryRecord
from caobunga.store import Store
from caobunga.suite_verbs import (
    TAIL_CHARS,
    TIMEOUTS,
    VERBS,
    probe_fitness,
    run_verb,
)


def _registered_repo(tmp_path: Path, *, justfile: bool = True) -> tuple[Store, Path]:
    store = Store(tmp_path / "home")
    repo = tmp_path / "Code" / "toolkit"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603, S607
    if justfile:
        (repo / "justfile").write_text("test:\n    echo hi\n", encoding="utf-8")
    store.register(RegistryRecord(id="toolkit", path=str(repo)))
    return store, repo


def _ok(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
    return 0, f"ran {' '.join(argv)} in {cwd.name} under {timeout:.0f}s\n"


def test_verbs_are_fixed_and_named() -> None:
    """The caller chooses whether, never what: three verbs, all `just`, no args."""
    assert VERBS == {
        "run_tests": ("just", "test"),
        "run_lint": ("just", "lint"),
        "run_typecheck": ("just", "typecheck"),
    }
    assert set(TIMEOUTS) == set(VERBS)


def test_run_tests_in_registered_repo_reports_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, repo = _registered_repo(tmp_path)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")
    seen: list[tuple[tuple[str, ...], Path, float]] = []

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        seen.append((tuple(argv), cwd, timeout))
        return 1, "FAILED tests/test_x.py::test_y\n1 failed, 3 passed\n"

    result = run_verb("run_tests", str(repo), store=store, run=run)
    assert seen == [(("just", "test"), repo.resolve(), TIMEOUTS["run_tests"])]
    assert result.exit_code == 1
    assert result.refused is None
    assert not result.timed_out
    text = result.render()
    assert text.startswith("run_tests: `just test` in ")
    assert "exit: 1" in text
    assert "1 failed, 3 passed" in text


def test_guard_wraps_the_spawn_but_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard opens after the refusal check and closes after the tree read (CBN-310)."""
    store, repo = _registered_repo(tmp_path)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")
    events: list[str] = []

    @contextlib.contextmanager
    def guard() -> Iterator[None]:
        events.append("enter")
        yield
        events.append("exit")

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        events.append("spawn")
        return 0, "ok\n"

    run_verb("run_tests", str(repo), store=store, run=run, guard=guard)
    assert events == ["enter", "spawn", "exit"]

    events.clear()
    refused = run_verb("run_tests", "/nonexistent/anywhere", store=store, run=run, guard=guard)
    assert refused.refused is not None
    assert events == []


@pytest.mark.parametrize(
    ("repo_path", "fragment"),
    [
        ("relative/path", "must be absolute"),
        ("/nonexistent/anywhere", "not a directory"),
    ],
)
def test_refuses_bad_paths_before_any_subprocess(
    tmp_path: Path, repo_path: str, fragment: str
) -> None:
    store = Store(tmp_path / "home")
    called = False

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        nonlocal called
        called = True
        return 0, ""

    result = run_verb("run_lint", repo_path, store=store, run=run)
    assert result.refused is not None and fragment in result.refused
    assert result.render() == f"run_lint: refused — {result.refused}"
    assert not called


def test_refuses_an_unregistered_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry is the whole bound: a real repo with a justfile still refuses."""
    store = Store(tmp_path / "home")
    stranger = tmp_path / "Code" / "stranger"
    stranger.mkdir(parents=True)
    (stranger / "justfile").write_text("test:\n    echo hi\n", encoding="utf-8")
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")
    result = run_verb("run_tests", str(stranger), store=store, run=_ok)
    assert result.refused == f"not a registered caobunga project: {stranger.resolve()}"


def test_accepts_a_case_variant_of_the_registered_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-131's rule, reused: `~/code/X` and a record written `~/Code/X` are one repo."""
    store, repo = _registered_repo(tmp_path)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")
    variant = str(repo).replace("/Code/", "/code/")
    result = run_verb("run_tests", variant, store=store, run=_ok)
    # A case-variant path only resolves to a directory on a case-insensitive
    # filesystem; elsewhere the refusal is the directory check, not the registry.
    if result.refused is not None:
        assert "not a directory" in result.refused
    else:
        assert result.exit_code == 0


def test_refuses_without_a_justfile_or_a_just_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, repo = _registered_repo(tmp_path, justfile=False)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")
    assert run_verb("run_lint", str(repo), store=store, run=_ok).refused == (
        f"no justfile at {repo.resolve()} — nothing to run"
    )
    (repo / "justfile").write_text("lint:\n    echo hi\n", encoding="utf-8")
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: None)
    assert run_verb("run_lint", str(repo), store=store, run=_ok).refused == (
        "`just` is not on PATH"
    )


def test_timeout_is_a_result_not_a_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, repo = _registered_repo(tmp_path)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        raise subprocess.TimeoutExpired(list(argv), timeout, output="partial output\n")

    result = run_verb("run_tests", str(repo), store=store, run=run, timeout=5.0)
    assert result.timed_out and result.exit_code is None
    text = result.render()
    assert "exit: none (timed out)" in text
    assert "partial output" in text


def test_output_is_tailed_from_the_front(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest's failure summary and ruff's findings trail, so the head is what goes."""
    store, repo = _registered_repo(tmp_path)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")
    body = "x" * (TAIL_CHARS * 2) + "\nFAILED at the end\n"

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        return 1, body

    result = run_verb("run_tests", str(repo), store=store, run=run)
    assert result.truncated
    assert len(result.output) == TAIL_CHARS
    assert result.output.endswith("FAILED at the end\n")
    assert f"(last {TAIL_CHARS} chars; head dropped)" in result.render()


def test_tree_delta_is_measured_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound 3: a suite that dirties the tree is a listed fact, never hidden."""
    store, repo = _registered_repo(tmp_path)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")

    def dirtying(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        (cwd / "stray.txt").write_text("oops", encoding="utf-8")
        return 0, "ok\n"

    clean = run_verb("run_lint", str(repo), store=store, run=_ok)
    assert not clean.tree_changed
    assert "tree: unchanged (git status --porcelain -uall)" in clean.render()
    dirty = run_verb("run_lint", str(repo), store=store, run=dirtying)
    assert dirty.tree_changed
    assert "tree: CHANGED — appeared: ?? stray.txt" in dirty.render()


def test_tree_unknown_when_git_status_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "home")
    repo = tmp_path / "Code" / "plain"
    repo.mkdir(parents=True)  # a directory, not a git repo
    (repo / "justfile").write_text("lint:\n    echo hi\n", encoding="utf-8")
    store.register(RegistryRecord(id="plain", path=str(repo)))
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    result = run_verb("run_lint", str(repo), store=store, run=_ok)
    assert result.tree_unknown and not result.tree_changed
    assert "tree: unknown (git status failed)" in result.render()


def test_missing_recipe_is_a_refusal_not_a_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A justfile that lacks the verb's recipe cannot answer it — `refused`, like
    no justfile at all — not `red` (CBN-211: a fleet repo's `CORE-12`, `check:` in place of
    `typecheck:`). Only just's own error, naming this verb's recipe, reclassifies;
    the same message about some *other* recipe is the verb failing, and stays a code.
    """
    store, repo = _registered_repo(tmp_path)
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")

    def missing(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        return 1, f"error: justfile does not contain recipe `{argv[-1]}`\n"

    result = run_verb("run_typecheck", str(repo), store=store, run=missing)
    assert result.refused == "no `typecheck` recipe in justfile — nothing to run"
    assert result.exit_code is None and not result.timed_out
    assert result.output.startswith("error: justfile does not contain recipe")
    assert result.render() == f"run_typecheck: refused — {result.refused}"

    def other(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        return 1, "error: Justfile does not contain recipe `_helper`\n"

    failed = run_verb("run_typecheck", str(repo), store=store, run=other)
    assert failed.refused is None and failed.exit_code == 1


def test_fitness_refuses_without_a_justfile(tmp_path: Path) -> None:
    """Report-only (CBN-215): a repo with no justfile can never fold `suite: green`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    result = probe_fitness(repo)
    assert result.fit is False
    assert result.refused == f"no justfile at {repo}"
    assert result.render() == f"suite-fitness: refused — no justfile at {repo}"


def test_fitness_refuses_without_just_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "justfile").write_text("test:\n    echo hi\n", encoding="utf-8")
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: None)
    result = probe_fitness(repo)
    assert result.fit is False
    assert result.refused == "`just` is not on PATH"


def test_fitness_refuses_on_a_summary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "justfile").write_text("test:\n    echo hi\n", encoding="utf-8")
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")

    def broken(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        return 1, "error: expected ':', got '='\n"

    result = probe_fitness(repo, run=broken)
    assert result.fit is False
    assert result.refused == "`just --summary` exited 1: error: expected ':', got '='"


def test_fitness_names_missing_recipes_no_check_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-211's `CORE-12` shape: a `check:` recipe never satisfies `typecheck`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "justfile").write_text("test:\n    echo hi\n", encoding="utf-8")
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")

    def summary(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        return 0, "test lint check\n"

    result = probe_fitness(repo, run=summary)
    assert result.fit is False
    assert result.missing == ("typecheck",)
    assert result.present == ("test", "lint")
    assert result.render() == (
        "suite-fitness: unreachable — missing recipe(s): typecheck (has: test, lint)"
    )


def test_fitness_is_fit_when_all_three_recipes_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "justfile").write_text("test:\n    echo hi\n", encoding="utf-8")
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")
    seen: list[tuple[str, ...]] = []

    def summary(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        seen.append(tuple(argv))
        return 0, "build test lint typecheck deploy\n"

    result = probe_fitness(repo, run=summary)
    assert result.fit is True
    assert result.missing == ()
    assert result.render() == "suite-fitness: fit — test/lint/typecheck recipes present"
    # Never runs the verbs themselves, only lists them.
    assert seen == [("just", "--summary")]
