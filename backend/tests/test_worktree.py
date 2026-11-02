"""Worktree start and the repo-family identity, against real temp git repos (CBN-EPIC-450.2)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import _commit, _git

from caobunga import worktree
from caobunga.errors import GitUnreadable, Unlaunchable, UserError
from caobunga.git import main_worktree, repo_identity, worktree_list
from caobunga.worktree import (
    WorktreeContext,
    branch_name,
    collision,
    retained_ids,
    start,
    worktree_path,
)

TASK = "CBN-EPIC-9.2"


def _head(repo: Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init")
    _commit(repo, "init")
    return repo


@pytest.fixture
def allow_file_submodules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local-path submodule clones are refused by default (git ≥ 2.38.1); a real
    adopter's `.flaitron/core` is an https URL. Env-level so `start`'s own git
    subprocesses inherit it."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


def _with_submodule(tmp_path: Path) -> tuple[Path, Path, str]:
    """A repo pinning `.flaitron/core` to a commit that is no longer the core's tip."""
    core = tmp_path / "core"
    core.mkdir()
    (core / "SPEC.md").write_text("pinned\n", encoding="utf-8")
    _git(core, "init")
    _commit(core, "pinned")
    pinned = _head(core)
    repo = _repo(tmp_path)
    _git(repo, "submodule", "add", str(core), ".flaitron/core")
    _commit(repo, "add core")
    (core / "SPEC.md").write_text("moved on\n", encoding="utf-8")
    _commit(core, "unpinned")
    return repo, core, pinned


# --- start: the happy path ---


@pytest.mark.usefixtures("allow_file_submodules")
def test_start_creates_branch_and_worktree_at_base_with_pinned_submodule(tmp_path: Path) -> None:
    repo, _core, pinned = _with_submodule(tmp_path)
    base = _head(repo)
    (repo / "later.txt").write_text("main moved on\n", encoding="utf-8")
    _commit(repo, "later")

    ctx = start(repo, TASK, base)

    assert ctx.worker_root == (tmp_path / "repo-worktrees" / f"wt-{TASK}").resolve()
    assert _head(ctx.worker_root) == base
    assert not (ctx.worker_root / "later.txt").exists()
    branch = subprocess.run(  # noqa: S603
        ["git", "-C", str(ctx.worker_root), "branch", "--show-current"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == f"wt-{TASK}" == ctx.branch
    core_checkout = ctx.worker_root / ".flaitron" / "core"
    assert (core_checkout / "SPEC.md").read_text(encoding="utf-8") == "pinned\n"
    assert _head(core_checkout) == pinned


def test_start_context_keeps_roots_distinct(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _head(repo)
    ctx = start(repo, TASK, base)
    assert ctx == WorktreeContext(
        registry_root=repo.resolve(),
        landing_root=repo.resolve(),
        worker_root=worktree_path(repo.resolve(), TASK),
        execution_root=worktree_path(repo.resolve(), TASK),
        base_sha=base,
        branch=f"wt-{TASK}",
    )
    assert ctx.worker_root != ctx.landing_root


def test_start_context_maps_a_nested_registration_to_the_same_subpath(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ctx = start(repo / "backend", TASK, _head(repo))
    assert ctx.registry_root == (repo / "backend").resolve()
    assert ctx.landing_root == repo.resolve()
    assert ctx.execution_root == ctx.worker_root / "backend"
    assert (ctx.execution_root / "app.py").is_file()


# --- start: refusals before anything is created ---


def test_refuses_a_registration_inside_a_linked_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "side", str(linked))
    with pytest.raises(UserError, match="inside a linked worktree") as exc:
        start(linked, TASK, _head(repo))
    assert exc.value.exit_code == 2
    assert not (tmp_path / "linked-worktrees").exists()
    assert not (tmp_path / "repo-worktrees").exists()


def test_refuses_an_existing_branch_only_orphan(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "branch", f"wt-{TASK}")
    assert collision(repo.resolve(), TASK) == f"branch wt-{TASK} already exists"
    with pytest.raises(UserError, match="already exists — an earlier dispatch never ended"):
        start(repo, TASK, _head(repo))
    assert not worktree_path(repo.resolve(), TASK).exists()


def test_refuses_an_existing_target_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = worktree_path(repo.resolve(), TASK)
    target.mkdir(parents=True)
    (target / "leftover.txt").write_text("someone's\n", encoding="utf-8")
    with pytest.raises(UserError, match="already exists"):
        start(repo, TASK, _head(repo))
    assert (target / "leftover.txt").is_file()
    refs = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "branch", "--list", f"wt-{TASK}"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert refs == ""


def test_refuses_a_second_start_of_the_same_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    start(repo, TASK, _head(repo))
    with pytest.raises(UserError, match=f"branch wt-{TASK} already exists"):
        start(repo, TASK, _head(repo))


def test_refuses_outside_a_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(UserError, match="not inside a git checkout"):
        start(plain, TASK, "0" * 40)


def test_refuses_a_bare_repository(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)  # noqa: S603, S607
    with pytest.raises(UserError, match="not inside a git checkout"):
        start(bare, TASK, "0" * 40)


@pytest.mark.parametrize("bad", ["../escape", "wt/x", "cbn-1", "CBN-1 ", ""])
def test_refuses_a_name_that_is_not_a_task_id(tmp_path: Path, bad: str) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(UserError, match="not a task id"):
        start(repo, bad, _head(repo))
    assert not (tmp_path / "repo-worktrees").exists()


def test_branch_name_accepts_epic_and_audit_ids() -> None:
    assert branch_name("CBN-EPIC-450.N") == "wt-CBN-EPIC-450.N"
    assert branch_name("CBN-443") == "wt-CBN-443"


def test_refuses_when_the_branch_read_answers_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    real = worktree.git_run

    def no_branches(root: Path, *args: str, timeout: float = 30.0) -> str | None:
        if args[0] == "for-each-ref":
            return None
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(worktree, "git_run", no_branches)
    with pytest.raises(UserError, match="cannot read branches"):
        start(repo, TASK, _head(repo))
    assert not worktree_path(repo.resolve(), TASK).exists()


def test_a_hung_git_raises_unreadable_before_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    base = _head(repo)

    def hang(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(GitUnreadable) as exc:
        start(repo, TASK, base)
    assert exc.value.exit_code == 5
    assert not (tmp_path / "repo-worktrees").exists()


# --- start: failures after `worktree add` keep what they made ---


@pytest.mark.usefixtures("allow_file_submodules")
def test_submodule_failure_retains_the_branch_and_directory(tmp_path: Path) -> None:
    repo, core, _pinned = _with_submodule(tmp_path)
    core.rename(tmp_path / "core-gone")
    with pytest.raises(UserError, match="`git submodule update` failed") as exc:
        start(repo, TASK, _head(repo))
    message = str(exc.value)
    target = worktree_path(repo.resolve(), TASK)
    assert (
        f"retained, not removed: branch wt-{TASK}, worktree registration for {target}, "
        f"directory {target}"
    ) in message
    assert target.is_dir()
    assert collision(repo.resolve(), TASK) == f"branch wt-{TASK} already exists"


def test_submodule_hang_retains_and_names_what_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    real = worktree.git_run

    def hang_on_submodule(root: Path, *args: str, timeout: float = 30.0) -> str | None:
        if args[0] == "submodule":
            raise GitUnreadable(root)
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(worktree, "git_run", hang_on_submodule)
    with pytest.raises(Unlaunchable, match="`git submodule update` did not answer") as exc:
        start(repo, TASK, _head(repo))
    assert exc.value.exit_code == 5
    assert "retained, not removed" in str(exc.value)
    assert worktree_path(repo.resolve(), TASK).is_dir()


def test_worktree_add_failure_refuses_and_names_the_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    refused = f"git worktree add` failed \\(git refused\\) for wt-{TASK}"
    with pytest.raises(UserError, match=refused) as exc:
        start(repo, TASK, "f" * 40)
    assert str(exc.value).endswith("nothing was left behind")


# --- repo-family identity ---


def test_main_worktree_is_shared_by_the_checkout_its_subpaths_and_linked_worktrees(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "side", str(linked))
    for member in (repo, repo / "backend", linked, linked / "backend"):
        found = main_worktree(member)
        assert found is not None
        assert found.resolve() == repo.resolve()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main_worktree(plain) is None


def test_main_worktree_degrades_to_none_when_git_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)

    def hang(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    monkeypatch.setattr(subprocess, "run", hang)
    assert main_worktree(repo) is None
    with pytest.raises(GitUnreadable):
        worktree_list(repo)


def test_worktree_list_marks_a_bare_main_entry(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)  # noqa: S603, S607
    entries = worktree_list(bare)
    assert entries is not None
    assert entries[0].bare
    assert entries[0].path.resolve() == bare.resolve()


# --- review hardening: case, odd checkouts, sha shape, stale registrations ---


def test_start_from_a_differently_cased_registration(tmp_path: Path) -> None:
    """A registry path in another letter case still lands in git's own spelling."""
    repo = _repo(tmp_path, "Repo")
    variant = tmp_path / "repo"
    if not variant.exists():
        pytest.skip("case-sensitive filesystem")
    ctx = start(variant / "BACKEND", TASK, _head(repo))
    assert ctx.landing_root.name == "Repo"
    assert ctx.execution_root == ctx.worker_root / "backend"
    assert (ctx.execution_root / "app.py").is_file()


@pytest.mark.usefixtures("allow_file_submodules")
def test_main_worktree_of_a_submodule_checkout_is_its_toplevel(tmp_path: Path) -> None:
    repo, _core, _pinned = _with_submodule(tmp_path)
    checkout = repo / ".flaitron" / "core"
    found = main_worktree(checkout)
    assert found is not None
    assert found.resolve() == checkout.resolve()


def test_main_worktree_of_a_separate_git_dir_checkout_is_its_toplevel(tmp_path: Path) -> None:
    work = tmp_path / "work"
    subprocess.run(  # noqa: S603
        ["git", "init", "--separate-git-dir", str(tmp_path / "sep.git"), str(work)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    (work / "f").write_text("x\n", encoding="utf-8")
    _commit(work, "init")
    found = main_worktree(work)
    assert found is not None
    assert found.resolve() == work.resolve()
    ctx = start(work, TASK, _head(work))
    assert ctx.worker_root.parent == (tmp_path / "work-worktrees").resolve()


@pytest.mark.parametrize("sha", ["--detach", "", "HEAD", "abc123", "F" * 40])
def test_refuses_a_base_that_is_not_a_full_sha(tmp_path: Path, sha: str) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(UserError, match="not a commit sha"):
        start(repo, TASK, sha)
    assert not (tmp_path / "repo-worktrees").exists()


def test_refuses_a_target_still_registered_after_its_directory_was_removed(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    target = worktree_path(repo.resolve(), TASK)
    _git(repo, "worktree", "add", "-b", "elsewhere", str(target))
    _git(repo, "worktree", "lock", str(target))  # survives an incidental prune
    subprocess.run(["rm", "-rf", str(target)], check=True)  # noqa: S603, S607
    assert collision(repo.resolve(), TASK) == (
        f"{target} is still registered as a worktree (see `git worktree prune`)"
    )
    with pytest.raises(UserError, match="still registered as a worktree"):
        start(repo, TASK, _head(repo))


def test_worktree_list_parses_every_entry_main_first(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "side", str(linked))
    entries = worktree_list(linked)
    assert entries is not None
    assert [entry.path.resolve() for entry in entries] == [repo.resolve(), linked.resolve()]
    assert not any(entry.bare for entry in entries)


# --- review round 2 ---


@pytest.mark.usefixtures("allow_file_submodules")
def test_refuses_a_submodule_checkout(tmp_path: Path) -> None:
    repo, _core, pinned = _with_submodule(tmp_path)
    checkout = repo / ".flaitron" / "core"
    with pytest.raises(UserError, match="is a submodule of"):
        start(checkout, TASK, pinned)
    assert not (repo / ".flaitron" / "core-worktrees").exists()


def test_refuses_a_nested_registration_untracked_at_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "scratch").mkdir()
    (repo / "scratch" / "ignored.txt").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(UserError, match="scratch/ is not tracked at") as exc:
        start(repo / "scratch", TASK, _head(repo))
    assert f"branch wt-{TASK}" in str(exc.value)


def test_a_stale_registration_in_another_case_is_still_a_collision(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "Repo")
    if not (tmp_path / "repo").exists():
        pytest.skip("case-sensitive filesystem")
    target = worktree_path(repo.resolve(), TASK)
    variant = tmp_path / "repo-worktrees" / f"wt-{TASK}"
    _git(repo, "worktree", "add", "-b", "elsewhere", str(variant))
    _git(repo, "worktree", "lock", str(variant))
    subprocess.run(["rm", "-rf", str(tmp_path / "repo-worktrees")], check=True)  # noqa: S603, S607
    assert collision(repo.resolve(), TASK) == (
        f"{target} is still registered as a worktree (see `git worktree prune`)"
    )


def test_identity_reads_an_empty_toplevel_as_no_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-2.25 git answered `--show-toplevel` in a bare repo with an empty line."""
    import caobunga.git as git_module

    monkeypatch.setattr(git_module, "_run", lambda *_a, **_k: ".\n.\n\n\n")
    assert repo_identity(tmp_path) is None
    assert main_worktree(tmp_path) is None


def test_a_linked_worktree_whose_list_fails_has_no_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "side", str(linked))
    import caobunga.git as git_module

    monkeypatch.setattr(git_module, "worktree_list", lambda _root: None)
    identity = repo_identity(linked)
    assert identity is not None
    assert identity.linked
    assert identity.main is None
    assert main_worktree(linked) is None
    with pytest.raises(UserError, match="inside a linked worktree"):
        start(linked, TASK, _head(repo))


# --- retained ids (CBN-EPIC-450.5) ---


def test_retained_ids_is_empty_for_a_fresh_checkout(tmp_path: Path) -> None:
    assert retained_ids(_repo(tmp_path)) == frozenset()


def test_retained_ids_names_every_kind_of_leftover(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    start(repo, TASK, _head(repo))
    _git(repo, "branch", "wt-CBN-2")
    worktree_path(repo, "CBN-3").mkdir(parents=True)
    # A registration whose directory and branch are gone: only git's list knows it.
    stale = start(repo, "CBN-4", _head(repo))
    _git(repo, "branch", "-m", "wt-CBN-4", "renamed")
    shutil.rmtree(stale.worker_root)
    # Not task ids, or not `wt-` names: ignored.
    _git(repo, "branch", "feature")
    _git(repo, "branch", "wt-not_an_id")
    (worktree_path(repo, "CBN-3").parent / "notes").mkdir()
    assert retained_ids(repo) == {TASK, "CBN-2", "CBN-3", "CBN-4"}
    for task_id in retained_ids(repo):
        assert collision(repo.resolve(), task_id) is not None


def test_retained_ids_reads_through_a_nested_registration(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "branch", "wt-CBN-2")
    assert retained_ids(repo / "backend") == {"CBN-2"}


def test_retained_ids_refuses_when_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    real = worktree.git_run

    def no_refs(root: Path, *args: str, timeout: float = 30.0) -> str | None:
        return None if args[:1] == ("for-each-ref",) else real(root, *args, timeout=timeout)

    monkeypatch.setattr(worktree, "git_run", no_refs)
    with pytest.raises(UserError, match="cannot read branches"):
        retained_ids(repo)
    monkeypatch.setattr(worktree, "git_run", real)
    monkeypatch.setattr(worktree, "worktree_list", lambda _root: None)
    with pytest.raises(UserError, match="cannot list worktrees"):
        retained_ids(repo)


def test_retained_ids_refuses_an_unlistable_worktrees_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    worktree_path(repo, "CBN-3").mkdir(parents=True)

    def denied(_path: object) -> list[str]:
        raise PermissionError("denied")

    monkeypatch.setattr(worktree.os, "listdir", denied)
    with pytest.raises(UserError, match="cannot list"):
        retained_ids(repo)
