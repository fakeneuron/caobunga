"""Collision-safe worktree start for isolated unattended dispatch (CBN-EPIC-450.2).

The start half of the pinned Flaitron `docs/WORKTREES.md` §"Procedure", which
`docs/EXTERNAL-AGENTS.md` step 10 requires of every operator-less dispatch:
check for a collision, `git worktree add -b wt-<ID>`, then initialize pinned
submodules. The run loop calls `start` for a record with `isolate` set
(CBN-EPIC-450.4); the end half, landing and removal, is `caobunga.landing`.

Every safety read is strict. A nonzero answer refuses (`UserError`, exit 2) and
a hung or missing `git` raises `GitUnreadable` (exit 5), so an unreadable check
never passes for a clean one. Nothing here deletes. A branch, directory, or
registered worktree left behind by an earlier start is a collision to inspect,
never something to reuse, and a start that fails at `worktree add` or after it
says exactly what it left behind.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from caobunga.errors import GitUnreadable, Unlaunchable, UserError
from caobunga.flaitron.grammar import TASK_ID_BODY
from caobunga.git import RepoIdentity, git_run, repo_identity, worktree_list

BRANCH_PREFIX = "wt-"
# `worktree add` checks out a whole tree and `submodule update --init` may
# clone over the network, so both get longer than `GIT_TIMEOUT`'s 30 s.
CHECKOUT_TIMEOUT = 300.0

_TASK_ID = re.compile(TASK_ID_BODY)
# A full object name (SHA-1 or SHA-256). Also keeps an option-shaped value
# from reaching `worktree add` as a flag.
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class WorktreeContext:
    """Where one isolated dispatch runs, kept apart from where it was registered.

    `registry_root` is the registered path, and only identifies the project.
    `landing_root` is the main checkout the branch lands into. `worker_root` is
    the linked worktree's own toplevel. `execution_root` is `registry_root`'s
    counterpart inside it, so an id registered at a nested path runs at the
    same subpath. `base_sha` is the commit the branch starts from, and the one
    a fast-forward landing must still find on `landing_root`.
    """

    registry_root: Path
    landing_root: Path
    worker_root: Path
    execution_root: Path
    base_sha: str
    branch: str


def branch_name(task_id: str) -> str:
    """`wt-<TASK-ID>`, refusing anything that is not a Flaitron task id.

    The id becomes both a ref name and a directory name, so only the grammar's
    own shape is allowed through.
    """
    if not _TASK_ID.fullmatch(task_id):
        raise UserError(f"not a task id: {task_id!r}; refusing to name a worktree after it")
    return f"{BRANCH_PREFIX}{task_id}"


def worktree_path(landing_root: Path, task_id: str) -> Path:
    """`<project>-worktrees/wt-<TASK-ID>/`, a sibling of the main checkout."""
    return _worktrees_dir(landing_root) / branch_name(task_id)


def _worktrees_dir(landing_root: Path) -> Path:
    return landing_root.parent / f"{landing_root.name}-worktrees"


def origin(path: Path) -> Path | None:
    """The main-checkout path `path` stands in for, when it is inside a `wt-<ID>` worktree.

    `path` must sit in a linked worktree at exactly `worktree_path(main, id)`;
    the result is the same subpath of the main checkout. Anything else, an
    unreadable git included, is `None`. This lets the suite runner treat a
    registered project's own dispatch worktree as registered
    (CBN-EPIC-450.3) without admitting an arbitrary linked worktree.
    """
    try:
        identity = repo_identity(path)
    except GitUnreadable:
        return None
    if identity is None or not identity.linked or identity.main is None:
        return None
    name = identity.toplevel.name
    if not name.startswith(BRANCH_PREFIX):
        return None
    task_id = name.removeprefix(BRANCH_PREFIX)
    if not _TASK_ID.fullmatch(task_id):
        return None
    expected = worktree_path(identity.main, task_id)
    if _fold(expected) != _fold(identity.toplevel):
        return None
    return identity.main / identity.prefix


def main_checkout(registry_root: Path) -> RepoIdentity:
    """`registry_root`'s identity, refusing anything that is not in a main checkout.

    Refuses a path outside a working tree (not a repository, or bare), a path
    inside a linked worktree (dispatch starts from the main checkout only, so
    one checkout lands every branch), and a submodule checkout, whose
    `<name>-worktrees/` sibling would sit inside the superproject's tree.
    """
    identity = repo_identity(registry_root)
    if identity is None:
        raise UserError(
            f"{registry_root} is not inside a git checkout (not a repository, or bare); "
            "cannot start a worktree"
        )
    if identity.linked:
        raise UserError(
            f"{registry_root} is inside a linked worktree; "
            "isolated dispatch starts only from the main checkout"
        )
    superproject = git_run(registry_root, "rev-parse", "--show-superproject-working-tree")
    if superproject is None:
        raise UserError(f"cannot read {registry_root}'s superproject; not starting a worktree")
    if superproject.strip():
        raise UserError(
            f"{registry_root} is a submodule of {superproject.strip()}; "
            "its worktrees would land inside the superproject's tree"
        )
    return identity


def collision(landing: Path, task_id: str) -> str | None:
    """Why `task_id` cannot start a worktree, or `None` when nothing is in the way.

    An existing `wt-<ID>` branch counts even with no worktree attached to it,
    because a branch-only orphan may carry the only copy of a run's work. So
    does anything at the target path, and a worktree git still has registered
    there after its directory was removed by hand.
    """
    branch = branch_name(task_id)
    if _branch_exists(landing, branch):
        return f"branch {branch} already exists"
    target = worktree_path(landing, task_id)
    if os.path.lexists(target):
        return f"{target} already exists"
    if _registered(landing, target):
        return f"{target} is still registered as a worktree (see `git worktree prune`)"
    return None


def retained_ids(registry_root: Path) -> frozenset[str]:
    """Every task id `collision` would refuse: work an earlier dispatch left behind.

    Read from git and the worktrees directory on each invocation (CBN-EPIC-450.5),
    never from a stored list, so a restart or an orphan from a crashed run is
    seen the same way as this invocation's own park. A `wt-<ID>` branch, a
    worktree git still registers at a `wt-<ID>` path, and a `wt-<ID>` directory
    each count. The reason it was left does not matter: the dispatcher denies
    the id until the operator has inspected it. Every read is strict, so an
    unreadable answer refuses instead of passing for "nothing retained".
    """
    landing = main_checkout(registry_root).toplevel
    names: list[str] = []
    refs = git_run(landing, "for-each-ref", "--format=%(refname)", f"refs/heads/{BRANCH_PREFIX}*")
    if refs is None:
        raise UserError(f"cannot read branches in {landing}; not dispatching")
    names.extend(ref.removeprefix("refs/heads/") for ref in refs.splitlines())
    entries = worktree_list(landing)
    if entries is None:
        raise UserError(f"cannot list worktrees in {landing}; not dispatching")
    for entry in entries:
        task_id = _named_id(entry.path.name)
        if task_id is not None and _fold(entry.path) == _fold(worktree_path(landing, task_id)):
            names.append(entry.path.name)
    folder = _worktrees_dir(landing)
    try:
        names.extend(os.listdir(folder) if folder.is_dir() else [])
    except OSError as exc:
        raise UserError(f"cannot list {folder}: {exc}; not dispatching") from exc
    return frozenset(task_id for task_id in map(_named_id, names) if task_id is not None)


def started_at(landing: Path, branch: str, base_sha: str) -> bool:
    """Whether `branch` is the one a dispatch started at `base_sha` (CBN-EPIC-450.6).

    `start` creates `wt-<ID>` at its base, so the oldest reflog entry is that
    base. A report uses it to tell whether a run record describes the branch
    there now or an earlier one of the same name, removed since. With no reflog
    to read (`core.logAllRefUpdates` off, or expired), it falls back to "the
    base is an ancestor of the branch", which keeps a real record's landing
    guidance at the cost of not catching a re-created branch. `False` when git
    refuses either read. A hang still raises `GitUnreadable`.
    """
    ref = f"refs/heads/{branch}"
    log = git_run(landing, "reflog", "show", "--format=%H", ref, "--")
    shas = log.split() if log else []
    if shas:
        return shas[-1] == base_sha
    return git_run(landing, "merge-base", "--is-ancestor", base_sha, ref) is not None


def _named_id(name: str) -> str | None:
    """The task id a `wt-<ID>` branch or directory name carries, else `None`."""
    task_id = name.removeprefix(BRANCH_PREFIX)
    if task_id == name or not _TASK_ID.fullmatch(task_id):
        return None
    return task_id


def start(registry_root: Path, task_id: str, base_sha: str) -> WorktreeContext:
    """Create `wt-<ID>` at `base_sha` in its own worktree, with pinned submodules.

    Refuses before creating anything when `base_sha` is not a full commit id,
    the registry path is not in a main checkout, or something is already in the
    way (`collision`). If `worktree add` or the submodule step then fails, the
    refusal names whatever is left. A leftover blocks this id until the
    operator inspects it, the same rule an orphan from an earlier start gets.
    """
    if not _SHA.fullmatch(base_sha):
        raise UserError(f"not a commit sha: {base_sha!r}; refusing to start a worktree at it")
    identity = main_checkout(registry_root)
    landing = identity.toplevel
    blocked = collision(landing, task_id)
    if blocked is not None:
        raise UserError(
            f"{task_id}: {blocked} — an earlier dispatch never ended; "
            "inspect it before redispatching, nothing was reused or removed"
        )
    branch = branch_name(task_id)
    target = worktree_path(landing, task_id)
    add = ("worktree", "add", "-b", branch, str(target), base_sha)
    _setup(landing, landing, branch, target, *add)
    _setup(target, landing, branch, target, "submodule", "update", "--init")
    execution = target / identity.prefix
    if not execution.is_dir():
        raise _refusal(
            f"{identity.prefix} is not tracked at {base_sha[:12]}", landing, branch, target
        )
    return WorktreeContext(
        registry_root=registry_root.resolve(),
        landing_root=landing,
        worker_root=target,
        execution_root=execution,
        base_sha=base_sha,
        branch=branch,
    )


def _branch_exists(landing: Path, branch: str) -> bool:
    refs = git_run(landing, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}")
    if refs is None:
        raise UserError(f"cannot read branches in {landing}; not starting a worktree")
    return bool(refs.strip())


def _registered(landing: Path, target: Path) -> bool:
    """Whether git lists a worktree at `target`, compared resolved and case-folded
    (the `_same_repo` rule), since a stale entry may carry another spelling."""
    entries = worktree_list(landing)
    if entries is None:
        raise UserError(f"cannot list worktrees in {landing}; not starting one")
    key = _fold(target)
    return any(_fold(entry.path) == key for entry in entries)


def _fold(path: Path) -> str:
    return str(path.resolve()).casefold()


def _setup(root: Path, landing: Path, branch: str, target: Path, *args: str) -> None:
    """Run one setup step; on any failure, refuse and name what was left behind.

    A refusal is exit 2. A hang is exit 5 (`Unlaunchable`), as everywhere else
    git cannot answer, but still names the leftovers, which a bare
    `GitUnreadable` would not.
    """
    step = f"`git {' '.join(args[:2])}`"
    try:
        ok = git_run(root, *args, timeout=CHECKOUT_TIMEOUT) is not None
    except GitUnreadable as exc:
        raise _refusal(f"{step} did not answer", landing, branch, target, hung=True) from exc
    if not ok:
        raise _refusal(f"{step} failed (git refused)", landing, branch, target)


def _refusal(
    what: str, landing: Path, branch: str, target: Path, *, hung: bool = False
) -> UserError:
    left = _left_behind(landing, branch, target)
    tail = (
        f"left behind and retained, not removed: {left} — inspect and clean up before redispatching"
        if left
        else "nothing was left behind"
    )
    message = f"{what} for {branch} at {target}; {tail}"
    return Unlaunchable(message) if hung else UserError(message)


def _left_behind(landing: Path, branch: str, target: Path) -> str:
    parts: list[str] = []
    for label, probe in (
        (f"branch {branch}", lambda: _branch_exists(landing, branch)),
        (f"worktree registration for {target}", lambda: _registered(landing, target)),
    ):
        try:
            if probe():
                parts.append(label)
        except (UserError, GitUnreadable):
            parts.append(f"{label} (unknown — git could not say)")
    if os.path.lexists(target):
        parts.append(f"directory {target}")
    return ", ".join(parts)
