"""Git plumbing reads shared by a close inspection: HEAD, dirt, and committed content.

Split out of `close.py` at CBN-238: these reads have no close-classification
logic of their own — they answer "what does the repo say", and `close.py`'s
`inspect_close` composes the answers into a verdict. `suite_verbs._porcelain`
shares `git_porcelain`'s subprocess call (through `git_dirt`) rather than
keeping a second one, so the two porcelain reads cannot drift on timeout or
`OSError` policy.

Every read goes through `_run` (CBN-261), so all of them share `GIT_TIMEOUT`.
Most take `_git`'s policy — `git_dirt`'s: a hung `git`, a missing binary, and
a nonzero exit all degrade to `None` — "git could not say" — rather than
raising or hanging a status surface. `git_head` and
`find_archived_tasknote_at_head` do not (CBN-269): their `None` is read as a
repo fact ("not a git repository", "no archived note"), so a hung or missing
`git` raises `GitUnreadable` instead of passing for one. Nor does
`git_porcelain`, the launch gate's form of `git_dirt` (CBN-317).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from caobunga.errors import GitUnreadable
from caobunga.flaitron import TASKNOTE_DIR_REL
from caobunga.models import ForeignCommit
from caobunga.text import clip

GIT_TIMEOUT = 30.0


def _run(root: Path, *args: str, timeout: float = GIT_TIMEOUT) -> str | None:
    """`git -C root <args>` stdout; `None` on a nonzero exit.

    `errors="replace"`: a committed file that is not UTF-8 is still content, not
    a `UnicodeDecodeError` out of `git_show` (CBN-318).

    Raises `GitUnreadable` on `OSError` or timeout — git could not answer at all.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), *args],  # noqa: S607
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitUnreadable(root) from exc
    if result.returncode != 0:
        return None
    return result.stdout


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT) -> str | None:
    """`git -C root <args>` stdout; `None` on a nonzero exit, `OSError`, or timeout."""
    try:
        return _run(root, *args, timeout=timeout)
    except GitUnreadable:
        return None


def git_head(root: Path) -> str | None:
    """`HEAD`'s sha; `None` when git answers no (not a repo, unborn HEAD).

    Raises `GitUnreadable` when git cannot answer at all (CBN-269).
    """
    stdout = _run(root, "rev-parse", "HEAD")
    if stdout is None:
        return None
    sha = stdout.strip()
    return sha or None


def git_toplevel(root: Path) -> Path | None:
    """The worktree root `root` sits in; `None` outside a repo or when git could not say.

    The run lock's repo identity (CBN-EPIC-313.6): two ids registered at nested
    paths of one checkout share it, while a linked worktree — its own index and
    `HEAD` — keeps its own. `_git`'s policy, so the caller falls back to the
    path it has rather than refusing a run on this read alone.
    """
    stdout = _git(root, "rev-parse", "--show-toplevel")
    if stdout is None or not stdout.strip():
        return None
    return Path(stdout.strip())


def reachable_from_head(root: Path, sha: str) -> bool | None:
    """Whether `sha` is `HEAD` or one of its ancestors; `None` when git could not say.

    `git merge-base <sha> HEAD` rather than `--is-ancestor` (CBN-275): that flag
    answers "no" with exit 1, which `_git` cannot tell from an error, and the
    caller acts on a real "no" but keeps what it has on "could not say".
    """
    stdout = _git(root, "merge-base", sha, "HEAD")
    if stdout is None:
        return None
    return stdout.strip() == sha


_FIELD_SEP = "\x1f"


def list_foreign_commits(
    root: Path, start_sha: str, head: str, *, exclude: str | None = None
) -> list[ForeignCommit] | None:
    """Commits in `start_sha..head` this run does not itself account for (CBN-172).

    `loop.py` already takes `start_sha` before launch and `close.py` already
    compares `HEAD` against it to find the run's own commit — but only that one:
    a commit that lands in the same window from anywhere else was invisible until
    a hand `git log` turned one up mid-diagnosis (Fleet HQ's NAT-247.4 sweep,
    landed inside a CBN-167 worker window with nobody watching for it). `exclude`
    is the commit this run already attributes to itself — the closure commit on
    a completed run, or the commit that moved `HEAD` on a park.

    Report-only, the same posture as `tree_after` and `run_echo.echo_unparsed`:
    state it, refuse nothing — a foreign commit is evidence for the operator's
    post-mortem, not a reason to discard the worker's work. `None` when the range
    is empty or git could not read it, the same "not measured" shape `new_rows`
    uses for an absent baseline.
    """
    if head == start_sha:
        return None
    stdout = _git(root, "log", f"--format=%H{_FIELD_SEP}%an{_FIELD_SEP}%s", f"{start_sha}..{head}")
    if stdout is None:
        return None
    commits: list[ForeignCommit] = []
    for line in stdout.splitlines():
        if not line:
            continue
        sha, _, rest = line.partition(_FIELD_SEP)
        author, _, subject = rest.partition(_FIELD_SEP)
        if sha == exclude:
            continue
        commits.append(ForeignCommit(sha=sha, author=clip(author), subject=clip(subject)))
    return commits or None


def commits_since(root: Path, since: datetime) -> list[str] | None:
    """`<short sha> <subject>` for HEAD's commits dated after `since`, newest first (CBN-376).

    By committer date, so a commit made after `since` counts even when it was
    authored earlier. `None` when git could not say.
    """
    stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    stdout = _git(root, "log", f"--since={stamp}", "--format=%h %s", "HEAD")
    if stdout is None:
        return None
    return [clip(line) for line in stdout.splitlines() if line]


def sole_path_patch(root: Path, sha: str, rel: str) -> list[str] | None:
    """`sha`'s zero-context patch lines for `rel`, when `rel` is the only path it changes.

    `None` for a commit that touches any other path, a merge (no changed paths
    to compare), or a read git could not answer. The caller classifies the
    patch (CBN-299); this only refuses to hand over a commit that did more than
    the one file.
    """
    paths = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    if paths is None or paths.split() != [rel]:
        return None
    patch = _git(root, "show", "--format=", "--unified=0", sha, "--", rel)
    return None if patch is None else patch.splitlines()


def git_dirt(root: Path, *, timeout: float = GIT_TIMEOUT) -> list[str] | None:
    """`git status --porcelain` lines for `root`; `None` when git could not report.

    The degrading form of `git_porcelain`, which issues this read and is the one
    the operator-presence gate (CBN-EPIC-17.3) calls (CBN-317). Untracked
    files count, because flaitron's own foreign-dirt gate runs this same bare
    command and counts them. The candidate's-own-tasknote exemption is **not**
    applied here: `presence.tree_state` derives it from the parsed lines, so the
    dispatcher and the status report cannot disagree about it (CBN-130). This
    call had an `exempt` pathspec parameter until that split; it has none now,
    because a second implementation of the exemption is what the split removed.

    `-uall` is load-bearing, not a verbosity preference, and stays load-bearing
    across that change for a mechanism-specific reason. Default porcelain
    collapses a wholly-untracked directory to one `?? dir/` line, which names a
    directory and therefore matches no open tasknote's path — so a target whose
    `.flaitron/tasknote/` happened to be untracked would classify the exempt note
    as foreign dirt and refuse the very CBN-109 resume the exemption exists to
    allow. It was the pathspec that could not match a collapsed directory before;
    it is the path matching now. Same failure, same flag.

    Deliberately **not** the skip-not-fail policy the producer parses follow
    (`docs/CONTRACT.md` §"Flaitron reader"). Those read a producer's file, where
    a wrong answer costs one candidate; this is a safety read, where a wrong
    "clean" costs a dispatch into a tree someone is standing in. So an
    unreadable answer is `None` here, and the gate — through `git_porcelain`,
    which raises for a hang — refuses on it.

    `timeout` (CBN-238) is shared with `suite_verbs._porcelain`'s own porcelain
    read, so the two cannot disagree on how long a hung `git` gets before this
    call degrades to `None` the same way a nonzero exit or a missing binary does.

    `--no-optional-locks` (CBN-261): a plain `git status` refreshes the index
    stat cache and so takes `index.lock` in the target repo. This read runs from
    the dashboard and the dispatcher against repos someone may be working in, so
    it must never be the reason their own `git commit` fails on a held lock.
    """
    try:
        return git_porcelain(root, timeout=timeout)
    except GitUnreadable:
        return None


def git_porcelain(root: Path, *, timeout: float = GIT_TIMEOUT) -> list[str] | None:
    """`git_dirt`'s read, raising `GitUnreadable` where it degrades (CBN-317).

    `None` still means git answered "no" (nonzero exit). The operator-presence
    gate reads through this one so a hung or missing `git` refuses exit 5 like
    the pre-launch `HEAD` read, not the exit-2 `cannot read git status`.

    The argv is `git_dirt`'s documented read: `-uall` is load-bearing for the
    candidate-tasknote exemption and `--no-optional-locks` keeps the target's
    `index.lock` free (CBN-261) — see `git_dirt` before changing either.
    """
    stdout = _run(root, "--no-optional-locks", "status", "--porcelain", "-uall", timeout=timeout)
    if stdout is None:
        return None
    return [line for line in stdout.splitlines() if line.strip()]


DIRT_SAMPLE = 5


def tree_after(root: Path, task_id: str) -> str:
    """One `tree: N file(s) left (…)` line for a run record's `summary` (CBN-171, CBN-184).

    Originally scoped to the `worker-exited` / `timeout` escalations: those exit
    codes leave a run record that says only what the process did, never what it
    *left*, so an exit 0 with six modified files and a scaffold-only tasknote
    read identically, on the record alone, to a true no-op. CBN-184 extends the
    same read to a `closed` outcome's summary: a worker's post-commit filing
    (CBN-138.2's `unattended-candidates:` write, e.g.) can leave the tree dirty
    *after* the closing commit lands, and that residue used to surface only as
    the *next* launch's dirty-tree refusal — a stale complaint about the
    previous run rather than a fact on this one's own record. This folds the
    same `git status --porcelain -uall` read and `DIRT_SAMPLE` clip
    `_refuse_if_dirty` echoed before CBN-287, plus whether the candidate's own
    tasknote is still open, into one report-only line. It does not change what
    the *next* invocation's dirty-tree refusal sees — that stays a fresh read.
    """
    lines = git_dirt(root)
    note_open = (root / TASKNOTE_DIR_REL / f"{task_id}.md").is_file()
    note_part = "tasknote open" if note_open else "no open tasknote"
    if lines is None:
        return f"tree: unreadable ({note_part})"
    if not lines:
        return f"tree: 0 file(s) left ({note_part})"
    shown = "; ".join(line.strip() for line in lines[:DIRT_SAMPLE])
    more = f" (+{len(lines) - DIRT_SAMPLE} more)" if len(lines) > DIRT_SAMPLE else ""
    return f"tree: {len(lines)} file(s) left ({shown}{more}; {note_part})"


def git_show(root: Path, rel_path: str, *, rev: str = "HEAD") -> str | None:
    """`rel_path`'s content as committed at `rev`; `None` when it isn't there.

    The HEAD-tree counterpart to `git_head` / `git_dirt` (CBN-158): those read
    the ref and the checkout's dirt, this reads a *file's committed content*
    without touching the checkout at all, so a worker's uncommitted edit to the
    same path — mid-flip, mid-move — never leaks into a read of "what closed".
    `rev` lets close attribution read an earlier commit in the run's window
    (CBN-279).
    """
    return _git(root, "show", f"{rev}:{rel_path}")


def list_commits_touching(
    root: Path, start_sha: str, head: str, *rel_paths: str
) -> list[str] | None:
    """Shas in `start_sha..head` that touch any of `rel_paths`, oldest first.

    `--topo-order` so a parent always precedes its child whatever the commit
    dates say. `None` when git could not say (CBN-279).
    """
    stdout = _git(
        root,
        "log",
        "--topo-order",
        "--reverse",
        "--format=%H",
        f"{start_sha}..{head}",
        "--",
        *rel_paths,
    )
    if stdout is None:
        return None
    return [line for line in stdout.splitlines() if line]


def find_archived_tasknote_at_head(root: Path, task_id: str) -> str | None:
    """Posix path of `task_id`'s archived tasknote in `HEAD`'s tree, or `None`.

    `git ls-tree` reads the committed tree the same way `git_show` does, so an
    archive move that is still in-progress on disk (old file deleted, new file
    not yet written) reports nothing until the commit that makes it real — the
    other half of the HEAD-only classification `git_show` establishes.

    Raises `GitUnreadable` when git cannot answer at all (CBN-269): a `None`
    here classifies a close as `no-tasknote`, which a timeout must not pass for.
    """
    archive = (TASKNOTE_DIR_REL / "archive").as_posix()
    stdout = _run(root, "ls-tree", "-r", "--name-only", "HEAD", "--", archive)
    if stdout is None:
        return None
    suffix = f"/{task_id}.md"
    hits = sorted(line for line in stdout.splitlines() if line.endswith(suffix))
    return hits[0] if hits else None
