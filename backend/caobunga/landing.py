"""Land a verified isolated closure by fast-forward, then remove its worktree (CBN-EPIC-450.4).

The end half of the pinned Flaitron `docs/WORKTREES.md` §"Procedure": once a
`wt-<ID>` worktree (`worktree.start`) holds a closure the loop verified — a
`closed` outcome whose suite passed at the final HEAD — that HEAD fast-forwards
into the main checkout, and only then are the worktree and branch removed.

Nothing here merges, rebases, retries, or discards. `land` lands only when the
main checkout is clean, on the branch it had, and still at the commit the
branch started from, and
otherwise returns why not, leaving both checkouts as they were. `cleanup`
removes only a clean worktree whose HEAD main already contains, and deletes the
branch with git's safe `branch -d`. Each returns a reason rather than raising,
so the caller still writes its record and names what it retained. A hung or
missing `git` raises `GitUnreadable` from `land` only before the merge; a hang
during or after it is returned as a reason saying main may have moved.
"""

from __future__ import annotations

from pathlib import Path

from caobunga.errors import GitUnreadable
from caobunga.git import dirt_sample, git_head, git_porcelain, git_run
from caobunga.worktree import CHECKOUT_TIMEOUT, WorktreeContext


def checked_out(root: Path) -> str | None:
    """The branch ref `root` has checked out (`refs/heads/…`); `None` when detached."""
    ref = git_run(root, "symbolic-ref", "-q", "HEAD")
    return (ref.strip() or None) if ref is not None else None


def land(context: WorktreeContext, sha: str, *, ref: str | None) -> str | None:
    """Fast-forward `context.landing_root` to `sha`; `None` once main sits there.

    `sha` is the worktree HEAD the suite measured, and `ref` the branch main
    had checked out when the worktree started (`checked_out`): a fast-forward
    advances whatever is checked out, so a main that switched branches, or
    was detached to begin with, is left alone. Every check reads strictly,
    and any "no" is returned as the reason main was left alone.
    """
    landing = context.landing_root
    blocked = _blocked(context, sha, ref)
    if blocked is not None:
        return blocked
    if git_run(landing, "merge-base", "--is-ancestor", context.base_sha, sha) is None:
        return f"{sha[:12]} does not descend from the base {context.base_sha[:12]}"
    try:
        merged = git_run(landing, "merge", "--ff-only", sha, timeout=CHECKOUT_TIMEOUT)
        after = git_head(landing) if merged is not None else None
    except GitUnreadable as exc:
        return (
            f"git did not answer during the fast-forward ({exc}); main may already be at "
            f"{sha[:12]} — check its HEAD before landing by hand"
        )
    if merged is None:
        return "`git merge --ff-only` refused"
    if after != sha:
        # The merge said yes and main is somewhere else: someone moved it in between.
        where = after[:12] if after else "an unreadable HEAD"
        return f"the main checkout is at {where} after the merge"
    return None


def _blocked(context: WorktreeContext, sha: str, ref: str | None) -> str | None:
    """Why `land` must leave main alone before it merges, or `None`."""
    worker = context.worker_root
    landing = context.landing_root
    if ref is None:
        return "the main checkout was not on a branch when the worktree started"
    now = checked_out(landing)
    if now != ref:
        return f"the main checkout switched from {ref} to {now or 'a detached HEAD'}"
    if git_head(worker) != sha:
        return f"the worktree HEAD is no longer {sha[:12]}"
    dirt = _worktree_dirt(worker)
    if dirt is None:
        return f"cannot read git status in {worker}"
    if dirt:
        return f"the worktree has {len(dirt)} uncommitted change(s): {dirt_sample(dirt)}"
    dirt = git_porcelain(landing)
    if dirt is None:
        return f"cannot read git status in {landing}"
    if dirt:
        return f"the main checkout has {len(dirt)} uncommitted change(s): {dirt_sample(dirt)}"
    head = git_head(landing)
    if head != context.base_sha:
        moved = head[:12] if head is not None else "an unreadable HEAD"
        return f"the main checkout moved from {context.base_sha[:12]} to {moved}"
    return None


def _worktree_dirt(worker: Path) -> list[str] | None:
    """`git_porcelain`'s read, but blind to no submodule `ignore` setting.

    `cleanup` force-removes on a clean answer, so a submodule configured
    `ignore = dirty` must still show its uncommitted edits here.
    """
    out = git_run(
        worker,
        "--no-optional-locks",
        "status",
        "--porcelain",
        "-uall",
        "--ignore-submodules=none",
    )
    return None if out is None else [line for line in out.splitlines() if line.strip()]


def sync_submodules(context: WorktreeContext) -> str | None:
    """Check main's submodules out at the gitlinks it just landed; `None` when done.

    A fast-forward moves the superproject only, so a closure that bumped a pin
    (`.flaitron/core`, say) would leave main reading dirty at the old commit.
    No `--init`: a submodule main left uninitialized stays that way.
    """
    try:
        done = git_run(context.landing_root, "submodule", "update", timeout=CHECKOUT_TIMEOUT)
    except GitUnreadable as exc:
        return f"git did not answer: {exc}"
    return None if done is not None else "`git submodule update` refused"


def cleanup(context: WorktreeContext, sha: str) -> str | None:
    """Remove a landed worktree and its branch; `None` when both are gone.

    Only a porcelain-clean worktree at `sha`, with `sha` already in main's
    history, is removed. `--force` is what `git worktree remove` needs for a
    worktree with initialized submodules, and is safe once the tree is known
    clean (what it then drops is ignored output such as dependency trees).
    """
    worker = context.worker_root
    landing = context.landing_root
    try:
        if git_head(worker) != sha:
            return f"the worktree HEAD is no longer {sha[:12]}"
        dirt = _worktree_dirt(worker)
        if dirt is None or dirt:
            return f"the worktree is not clean ({len(dirt) if dirt else 'unreadable'})"
        if git_run(landing, "merge-base", "--is-ancestor", sha, "HEAD") is None:
            return f"{sha[:12]} is not in the main checkout's history"
        removed = git_run(
            landing, "worktree", "remove", "--force", str(worker), timeout=CHECKOUT_TIMEOUT
        )
        if removed is None:
            return "`git worktree remove` refused"
        if git_run(landing, "branch", "-d", context.branch) is None:
            return f"`git branch -d {context.branch}` refused"
    except GitUnreadable as exc:
        return f"git did not answer: {exc}"
    return None
