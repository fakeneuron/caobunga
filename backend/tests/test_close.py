"""Flaitron close inspector (PLAN + archive + git HEAD)."""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_loop import (
    CLOSED_PLAN,
    OPEN_PLAN,
    _amend,
    _archive_note,
    _commit,
    _git,
    _git_repo,
    _note,
)
from test_store import green_suite

from caobunga.close import (
    CloseSnapshot,
    diff_new_rows,
    extract_follow_ups,
    extract_summary,
    inspect_close,
    is_descoped,
    read_parked_follow_ups,
    reinspect_settled,
)
from caobunga.errors import GitUnreadable, HeadLost
from caobunga.flaitron import OpenTasknote, dispatchable, external_review_box, parse_plan
from caobunga.git import (
    GIT_TIMEOUT,
    find_archived_tasknote_at_head,
    git_dirt,
    git_head,
    git_show,
    list_foreign_commits,
    reachable_from_head,
    tree_after,
)
from caobunga.models import ExternalReview, Outcome

# `CBN-012` is the only open row and it carries no `[unattended]` marker — the
# CBN-EPIC-18.4 F4 shape, where the suggested next is exactly what the drain
# refuses. `CBN-013` is the marked row a fall-through can land on.
UNMARKED_NEXT_PLAN = """# sample

**Suggested next task:** **CBN-012** — client.

## High

- [ ] **CBN-012** [medium] | cao-client — client work.

## Medium

(none)

## Low

(none)

## Future Opportunities

(none)

## Completed

- [x] **CBN-011** [medium] | flaitron-reader — Completed 2026-08-29.
"""

MARKED_FALLTHROUGH_PLAN = UNMARKED_NEXT_PLAN.replace(
    "## Medium\n\n(none)",
    "## Medium\n\n- [ ] **CBN-013** [medium] [unattended] | seeded — seeded work.",
)

# The header names a row that is already checked — the CBN-151 shape, where
# the closing worker's line went stale and only the attended path repeated it.
STALE_NEXT_PLAN = UNMARKED_NEXT_PLAN.replace(
    "**Suggested next task:** **CBN-012** — client.",
    "**Suggested next task:** **CBN-010** — long done.",
).replace(
    "## Completed\n",
    "## Completed\n\n- [x] **CBN-010** [light] | done — Completed 2026-08-01.",
)


def test_extract_summary_first_paragraph() -> None:
    text = "**Final Summary:**\n\nThe lede sentence.\n\nMore detail.\n\n**Archived:** 2026-08-19\n"
    assert extract_summary(text) == "The lede sentence."


def test_extract_summary_skips_leading_doc_drift_paragraph() -> None:
    text = (
        "**Final Summary:**\n\nDoc-drift sweep: README no change.\n\n"
        "npm audit now clean.\n\n**Archived:** 2026-10-04\n"
    )
    assert extract_summary(text) == "npm audit now clean."


def test_extract_summary_doc_drift_only_keeps_it() -> None:
    text = "**Final Summary:**\n\n**Doc-drift:** none.\n\n**Archived:** 2026-10-04\n"
    assert extract_summary(text) == "**Doc-drift:** none."


def test_extract_summary_truncates_on_word_boundary() -> None:
    lede = " ".join(f"word{i}" for i in range(200))
    text = f"**Final Summary:**\n\n{lede}\n\n**Archived:** 2026-08-19\n"
    summary = extract_summary(text)
    assert len(summary) <= 501
    assert summary.endswith("…")
    assert not summary[:-1].endswith(" ")
    assert lede.startswith(summary[:-1])


def test_extract_summary_missing_archived_stamp() -> None:
    # CBN-145's shape: the archived note ends right after Final Summary with no
    # trailing **Archived:** line at all (CBN-152).
    text = "**Final Summary:**\n\nMoved the helper into grammar.py.\n"
    assert extract_summary(text) == "Moved the helper into grammar.py."


def test_extract_summary_no_final_summary_heading() -> None:
    assert extract_summary("## 📝 Phase 1: Discovery\n\nno closure yet.\n") == ""


def test_is_descoped_from_verdict() -> None:
    assert is_descoped("**Verdict:** De-scope\n**Rationale:** nope")
    assert not is_descoped("**Verdict:** Proceed")


def test_inspect_waits_until_head_moves(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    assert inspect_close(repo, "CBN-011", sha) is None
    _archive_note(repo, "CBN-011")
    assert inspect_close(repo, "CBN-011", sha) is None
    _commit(repo, "close CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.CLOSED
    assert snap.commit_sha != sha
    assert snap.next_task == "CBN-012"
    assert snap.tasknote_path == ".flaitron/tasknote/archive/cbn/CBN-011.md"
    assert "reader" in snap.summary.lower()


def test_inspect_close_ignores_mid_task_commit_before_the_real_close(tmp_path: Path) -> None:
    """CBN-158: a mid-task `/ft-file-followup` commit must not look like a close.

    The SCRAPE-61.3 shape: a worker's mid-task follow-up filing moves `HEAD` off
    `start_sha` before the worker's own close lands. At that instant the checkout
    may already show the PLAN row flipped (uncommitted) with no archived note
    written yet — the mid-move instant `inspect_close` used to read straight off
    disk. Classifying from `HEAD` instead means the row still reads as open until
    the close's own atomic commit lands.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / "scratch.txt").write_text("filed CBN-999\n", encoding="utf-8")
    _commit(repo, "file CBN-999 follow-up")
    assert git_head(repo) != sha
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    assert inspect_close(repo, "CBN-011", sha) is None
    _archive_note(repo, "CBN-011")
    _commit(repo, "close CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.CLOSED


def test_inspect_close_reports_a_commit_that_landed_mid_window(tmp_path: Path) -> None:
    """CBN-172: the NAT-247.4 shape — a foreign write lands inside a run's window.

    A commit unrelated to this task's own close (here: another repo's filing,
    the same "someone else committed into the tree" shape the ticket names)
    lands between `start_sha` and the eventual close. It must surface on the
    snapshot rather than staying invisible until a hand `git log`.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / "scratch.txt").write_text("filed CBN-999\n", encoding="utf-8")
    _commit(repo, "chore: unrelated sweep filing", author="Someone Else")
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _archive_note(repo, "CBN-011")
    _commit(repo, "close CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.CLOSED
    assert snap.foreign_commits is not None
    assert len(snap.foreign_commits) == 1
    foreign = snap.foreign_commits[0]
    assert foreign.subject == "chore: unrelated sweep filing"
    assert foreign.author == "Someone Else"
    assert foreign.sha != snap.commit_sha


def test_closed_summary_folds_post_commit_tree_residue(tmp_path: Path) -> None:
    """CBN-184: residue left *after* the closing commit used to surface only as
    the next launch's dirty-tree refusal, never on this run's own record.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _archive_note(repo, "CBN-011")
    _commit(repo, "close CBN-011")
    (repo / "stray.txt").write_text("post-commit residue\n", encoding="utf-8")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.CLOSED
    assert "tree: 1 file(s) left (" in snap.summary
    assert "stray.txt" in snap.summary
    assert "no open tasknote" in snap.summary


def test_tree_after_reports_clean_tree_with_open_tasknote(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _note("in-progress")})
    line = tree_after(repo, "CBN-011")
    assert line == "tree: 0 file(s) left (tasknote open)"


def test_inspect_close_reports_none_when_nothing_else_landed(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _archive_note(repo, "CBN-011")
    _commit(repo, "close CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.foreign_commits is None


def test_inspect_no_tasknote(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _commit(repo, "close without note")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.NO_TASKNOTE
    assert snap.tasknote_path is None
    assert snap.external_review is None


def test_inspect_waits_while_a_completed_row_still_has_an_open_note(tmp_path: Path) -> None:
    """The PLAN flip landed without the archive move: the close is still mid-flight."""
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _note("in-progress")})
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _commit(repo, "flip CBN-011 ahead of its archive move")
    assert inspect_close(repo, "CBN-011", sha) is None
    # The open note was the only thing holding it: archiving it closes the run.
    (repo / ".flaitron" / "tasknote" / "CBN-011.md").unlink()
    _archive_note(repo, "CBN-011")
    _commit(repo, "archive CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.CLOSED


def test_inspect_descoped(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _archive_note(repo, "CBN-011", descoped=True)
    _commit(repo, "de-scope CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.DE_SCOPED


def test_git_dirt_clean_repo_reports_nothing(tmp_path: Path) -> None:
    assert git_dirt(_git_repo(tmp_path, OPEN_PLAN)) == []


def test_git_dirt_lists_modified_and_untracked(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    (repo / "scratch.txt").write_text("wip\n", encoding="utf-8")
    dirt = git_dirt(repo)
    assert dirt is not None
    assert any(line.endswith(".flaitron/PLAN.md") for line in dirt)
    assert any(line.startswith("??") and line.endswith("scratch.txt") for line in dirt)


def test_git_dirt_reports_an_untracked_note_individually(tmp_path: Path) -> None:
    """Pins `-uall`, the flag the candidate's-own-tasknote exemption rides on.

    This fixture's `.flaitron/tasknote/` is itself untracked (git stores no empty
    directories). Without `-uall` porcelain collapses the whole directory to one
    `?? .flaitron/tasknote/` line, which names a directory and matches no open
    tasknote's path — so `presence.tree_state` would classify the exempt note as
    foreign dirt and refuse every CBN-109 resume (CBN-EPIC-17.3).

    The exemption itself is `presence.py`'s (CBN-130) and is pinned there; this
    is the raw read the exemption is derived from.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    note = ".flaitron/tasknote/CBN-011.md"
    (repo / note).write_text("---\nstatus: in-progress\n---\n", encoding="utf-8")
    assert git_dirt(repo) == [f"?? {note}"]


def test_git_dirt_outside_a_repo_is_none_not_clean(tmp_path: Path) -> None:
    """Bias-to-refuse: an unreadable answer is not "clean" (CBN-EPIC-17.3).

    The opposite of the skip-not-fail policy the producer parses use — there a
    wrong answer costs one candidate, here it costs a dispatch into a live tree.
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_dirt(plain) is None


def test_git_dirt_never_takes_the_targets_index_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins `--no-optional-locks` on the porcelain read (CBN-261).

    The flag's effect — skipping the index-refresh write — is not observable from
    porcelain output, so the argv is the only place to pin it.
    """
    seen: list[list[str]] = []
    real_run = subprocess.run

    def spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    assert git_dirt(_git_repo(tmp_path, OPEN_PLAN)) == []
    status = [argv for argv in seen if "status" in argv]
    assert status
    assert all(argv.index("--no-optional-locks") < argv.index("status") for argv in status)


_GIT_FAILURES = [OSError("no git"), subprocess.TimeoutExpired(cmd="git", timeout=GIT_TIMEOUT)]


def _fail_git(monkeypatch: pytest.MonkeyPatch, error: Exception) -> list[float | None]:
    seen: list[float | None] = []

    def boom(_argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs.get("timeout"))
        raise error

    monkeypatch.setattr(subprocess, "run", boom)
    return seen


@pytest.mark.parametrize("error", _GIT_FAILURES)
@pytest.mark.parametrize(
    "read",
    [
        lambda root: git_dirt(root),
        lambda root: git_show(root, "PLAN.md"),
        lambda root: list_foreign_commits(root, "a", "b"),
    ],
)
def test_every_git_read_degrades_to_none_on_a_hung_or_missing_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    read: Callable[[Path], object],
) -> None:
    """One helper, one policy (CBN-261): these reads never raise or hang a status surface."""
    seen = _fail_git(monkeypatch, error)
    assert read(tmp_path) is None
    assert seen == [GIT_TIMEOUT]


@pytest.mark.parametrize("error", _GIT_FAILURES)
@pytest.mark.parametrize(
    "read",
    [
        lambda root: git_head(root),
        lambda root: find_archived_tasknote_at_head(root, "CBN-011"),
    ],
)
def test_repo_fact_git_reads_raise_unreadable_on_a_hung_or_missing_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    read: Callable[[Path], object],
) -> None:
    """CBN-269: a `None` here reads as a repo fact, so "git could not say" raises instead."""
    seen = _fail_git(monkeypatch, error)
    with pytest.raises(GitUnreadable, match="git unreadable"):
        read(tmp_path)
    assert seen == [GIT_TIMEOUT]


def test_repo_fact_git_reads_keep_none_for_a_nonzero_exit(tmp_path: Path) -> None:
    """A nonzero exit is git answering "no" — not a repository — and stays `None`."""
    assert git_head(tmp_path) is None
    assert find_archived_tasknote_at_head(tmp_path, "CBN-011") is None


def _closed_at(tmp_path: Path, plan: str) -> tuple[Path, str]:
    """A repo whose `CBN-011` has closed, HEAD moved, and `plan` is now on disk."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(plan, encoding="utf-8")
    _archive_note(repo, "CBN-011")
    _commit(repo, "close CBN-011")
    return repo, sha


def test_unreadable_archive_listing_raises_rather_than_no_tasknote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-269: a timed-out `ls-tree` used to classify a real close `no-tasknote`."""
    repo, sha = _closed_at(tmp_path, CLOSED_PLAN)

    def unreadable(root: Path, _task_id: str) -> str | None:
        raise GitUnreadable(root)

    monkeypatch.setattr("caobunga.close.find_archived_tasknote_at_head", unreadable)
    with pytest.raises(GitUnreadable, match="git unreadable"):
        inspect_close(repo, "CBN-011", sha)


def test_unreadable_head_raises_unreadable_not_not_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha = _closed_at(tmp_path, CLOSED_PLAN)

    def unreadable(root: Path) -> str | None:
        raise GitUnreadable(root)

    monkeypatch.setattr("caobunga.close.git_head", unreadable)
    with pytest.raises(GitUnreadable, match="git unreadable"):
        inspect_close(repo, "CBN-011", sha)


def test_a_lost_head_raises_head_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the parked check, HEAD gone after launch is an error, not "still running"."""
    repo, sha = _closed_at(tmp_path, CLOSED_PLAN)
    monkeypatch.setattr("caobunga.close.git_head", lambda _root: None)
    with pytest.raises(HeadLost, match="HEAD unreadable after worker launch"):
        inspect_close(repo, "CBN-011", sha)


def test_a_parked_note_absorbs_a_lost_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The parked check runs first, so the same `None` HEAD is a park, not `HeadLost`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "tasknote" / "CBN-011.md").write_text(
        _note("blocked", "drift — premise"), encoding="utf-8"
    )
    monkeypatch.setattr("caobunga.close.git_head", lambda _root: None)
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.BLOCKED
    assert snap.commit_sha is None


def test_parked_note_survives_an_unreadable_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parked branch keeps its verdict; an unreadable HEAD only leaves no commit."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "tasknote" / "CBN-011.md").write_text(
        _note("blocked", "drift — premise"), encoding="utf-8"
    )

    def unreadable(root: Path) -> str | None:
        raise GitUnreadable(root)

    monkeypatch.setattr("caobunga.close.git_head", unreadable)
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.BLOCKED
    assert snap.commit_sha is None


def test_reachable_from_head_is_tri_state(tmp_path: Path) -> None:
    """CBN-275: a real "no" is distinct from "git could not say"."""
    repo, start = _closed_at(tmp_path, CLOSED_PLAN)
    head = git_head(repo)
    assert head is not None
    assert reachable_from_head(repo, head) is True
    assert reachable_from_head(repo, start) is True
    _git(repo, "checkout", "-q", "--orphan", "other")
    _commit(repo, "unrelated root")
    assert reachable_from_head(repo, "0" * 40) is None
    assert reachable_from_head(repo, head) is None  # no merge base at all


def _settled_close(tmp_path: Path) -> tuple[Path, str, CloseSnapshot]:
    """A detected close whose commit has since been amended off-branch (CBN-275)."""
    repo, start = _closed_at(tmp_path, CLOSED_PLAN)
    snap = inspect_close(repo, "CBN-011", start)
    assert snap is not None
    snap = snap.model_copy(update={"log_path": "/logs/run.log"})
    (repo / "amended.txt").write_text("amend\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "--amend", "--no-edit")
    return repo, start, snap


def test_reinspect_settled_adopts_the_amended_close(tmp_path: Path) -> None:
    repo, start, snap = _settled_close(tmp_path)
    fresh = reinspect_settled(repo, "CBN-011", start, snap)
    assert fresh.commit_sha == git_head(repo)
    assert fresh.commit_sha != snap.commit_sha
    assert fresh.outcome is Outcome.CLOSED
    assert fresh.log_path == "/logs/run.log"


def test_reinspect_settled_leaves_a_non_closure_alone(tmp_path: Path) -> None:
    repo, start, snap = _settled_close(tmp_path)
    parked = snap.model_copy(update={"outcome": Outcome.BLOCKED})
    assert reinspect_settled(repo, "CBN-011", start, parked) is parked


def test_reinspect_settled_keeps_the_close_when_git_cannot_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, start, snap = _settled_close(tmp_path)

    def unreadable(root: Path, *_args: Any, **_kwargs: Any) -> CloseSnapshot | None:
        raise GitUnreadable(root)

    monkeypatch.setattr("caobunga.close.inspect_close", unreadable)
    assert reinspect_settled(repo, "CBN-011", start, snap) is snap
    monkeypatch.setattr("caobunga.close.reachable_from_head", lambda *_a: None)
    assert reinspect_settled(repo, "CBN-011", start, snap) is snap


def test_reinspect_settled_keeps_the_close_when_the_reread_is_not_one(
    tmp_path: Path,
) -> None:
    """An off-branch HEAD whose PLAN no longer reads closed is not a reason to drop it."""
    repo, start, snap = _settled_close(tmp_path)
    _git(repo, "reset", "-q", "--hard", start)
    assert reinspect_settled(repo, "CBN-011", start, snap) is snap


def _stack_filing(repo: Path) -> str:
    """Stack a PLAN-touching follow-up filing on HEAD; return the close it sits on."""
    close = git_head(repo)
    assert close is not None
    plan = repo / ".flaitron" / "PLAN.md"
    plan.write_text(
        plan.read_text(encoding="utf-8") + "- [ ] **CBN-099** | later\n", encoding="utf-8"
    )
    _commit(repo, "chore: file CBN-099 follow-up")
    return close


def test_inspect_close_attributes_the_close_under_a_stacked_commit(tmp_path: Path) -> None:
    """CBN-279: a commit stacked before the first poll is foreign, not the close."""
    repo, start = _closed_at(tmp_path, CLOSED_PLAN)
    close = _stack_filing(repo)
    snap = inspect_close(repo, "CBN-011", start)
    assert snap is not None
    assert snap.commit_sha == close
    assert snap.foreign_commits is not None
    assert [c.subject for c in snap.foreign_commits] == ["chore: file CBN-099 follow-up"]


def test_reinspect_settled_attributes_an_amended_close_under_a_stacked_commit(
    tmp_path: Path,
) -> None:
    """CBN-279: amend the close, then stack B — records the amended close, only B foreign."""
    repo, start, snap = _settled_close(tmp_path)
    amended = _stack_filing(repo)
    fresh = reinspect_settled(repo, "CBN-011", start, snap)
    assert fresh.commit_sha == amended
    assert fresh.commit_sha != snap.commit_sha
    assert fresh.foreign_commits is not None
    assert [c.subject for c in fresh.foreign_commits] == ["chore: file CBN-099 follow-up"]


def _measured_close(tmp_path: Path) -> tuple[Path, str, CloseSnapshot]:
    """A detected close that already carries its suite and log (CBN-356)."""
    repo, start = _closed_at(tmp_path, CLOSED_PLAN)
    snap = inspect_close(repo, "CBN-011", start)
    assert snap is not None
    measured = {"log_path": "/logs/run.log", "suite": green_suite()}
    return repo, start, snap.model_copy(update=measured)


def test_reinspect_settled_on_branch_attributes_a_commit_stacked_mid_suite(
    tmp_path: Path,
) -> None:
    """CBN-356: the post-suite pass re-reads an on-branch close, so the stacked
    commit is foreign, and the measured suite and log ride onto the fresh read."""
    repo, start, snap = _measured_close(tmp_path)
    close = _stack_filing(repo)
    assert reinspect_settled(repo, "CBN-011", start, snap) is snap
    fresh = reinspect_settled(repo, "CBN-011", start, snap, on_branch=True)
    assert fresh.commit_sha == close == snap.commit_sha
    assert fresh.foreign_commits is not None
    assert [c.subject for c in fresh.foreign_commits] == ["chore: file CBN-099 follow-up"]
    assert fresh.suite == green_suite()
    assert fresh.log_path == "/logs/run.log"


def test_reinspect_settled_carries_the_suite_onto_an_amended_close(tmp_path: Path) -> None:
    """CBN-356: an amend during the suite re-reads without dropping `suite`."""
    repo, start, snap = _measured_close(tmp_path)
    _amend(repo)
    fresh = reinspect_settled(repo, "CBN-011", start, snap, on_branch=True)
    assert fresh.commit_sha == git_head(repo) != snap.commit_sha
    assert fresh.suite == green_suite()


def test_reinspect_settled_on_branch_keeps_an_on_branch_close_it_would_reattribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-356, CBN-357: a reachable close is the close — a re-read git cannot
    attribute keeps the first snapshot rather than falling back to HEAD."""
    repo, start, snap = _measured_close(tmp_path)
    _stack_filing(repo)
    monkeypatch.setattr("caobunga.close.list_commits_touching", lambda *_a: None)
    assert reinspect_settled(repo, "CBN-011", start, snap, on_branch=True) is snap


def test_inspect_close_falls_back_to_head_unless_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-357: first detect records a close git cannot attribute against HEAD
    (a closed record needs a sha); the strict re-read declines to guess."""
    repo, start = _closed_at(tmp_path, CLOSED_PLAN)
    monkeypatch.setattr("caobunga.close.list_commits_touching", lambda *_a: None)
    snap = inspect_close(repo, "CBN-011", start)
    assert snap is not None
    assert snap.commit_sha == git_head(repo)
    assert inspect_close(repo, "CBN-011", start, strict_attribution=True) is None
    monkeypatch.setattr("caobunga.close.list_commits_touching", lambda *_a: [])
    assert inspect_close(repo, "CBN-011", start, strict_attribution=True) is None


def test_reinspect_settled_on_branch_adopts_a_reattribution_to_a_stacked_commit(
    tmp_path: Path,
) -> None:
    """CBN-357: a no-tasknote close whose note the worker archives in a stacked
    commit is re-attributed to that commit, which the sha guard used to reject."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _commit(repo, "close without note")
    snap = inspect_close(repo, "CBN-011", start)
    assert snap is not None
    assert snap.outcome is Outcome.NO_TASKNOTE
    snap = snap.model_copy(update={"suite": green_suite()})
    _archive_note(repo, "CBN-011")
    _commit(repo, "archive CBN-011")
    fresh = reinspect_settled(repo, "CBN-011", start, snap, on_branch=True)
    assert fresh.outcome is Outcome.CLOSED
    assert fresh.commit_sha == git_head(repo) != snap.commit_sha
    assert fresh.suite == green_suite()
    assert fresh.foreign_commits is not None
    assert [c.subject for c in fresh.foreign_commits] == ["close without note"]


def test_next_task_attended_keeps_an_unmarked_suggestion(tmp_path: Path) -> None:
    """Attended validation never consults the marker — an unmarked line stands."""
    repo, sha = _closed_at(tmp_path, UNMARKED_NEXT_PLAN)
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.next_task == "CBN-012"


def test_next_task_attended_drops_a_stale_suggestion(tmp_path: Path) -> None:
    """CBN-151: a header naming a completed row falls through to the candidate."""
    repo, sha = _closed_at(tmp_path, STALE_NEXT_PLAN)
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.next_task == "CBN-012"


def test_next_task_unattended_omits_a_denied_suggestion(tmp_path: Path) -> None:
    """CBN-EPIC-18.4 F4: the drain must not publish a `next:` it just refused."""
    repo, sha = _closed_at(tmp_path, UNMARKED_NEXT_PLAN)
    snap = inspect_close(repo, "CBN-011", sha, unattended=True)
    assert snap is not None
    assert snap.next_task is None


def test_next_task_unattended_falls_through_to_the_marked_candidate(tmp_path: Path) -> None:
    """A denied suggestion defers to the pick, which is already posture-correct."""
    repo, sha = _closed_at(tmp_path, MARKED_FALLTHROUGH_PLAN)
    snap = inspect_close(repo, "CBN-011", sha, unattended=True)
    assert snap is not None
    assert snap.next_task == "CBN-013"


def test_next_task_unattended_keeps_a_marked_suggestion(tmp_path: Path) -> None:
    """Validated, not dropped — a suggestion this posture would dispatch stands."""
    repo, sha = _closed_at(tmp_path, CLOSED_PLAN)
    snap = inspect_close(repo, "CBN-011", sha, unattended=True)
    assert snap is not None
    assert snap.next_task == "CBN-012"


def test_next_task_unattended_drops_a_parked_suggestion(tmp_path: Path) -> None:
    """CBN-252: a suggested row whose note is parked is not claimed as next."""
    repo, sha = _closed_at(tmp_path, CLOSED_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-012.md").write_text(
        _note("blocked", "drift — premise"), encoding="utf-8"
    )
    snap = inspect_close(repo, "CBN-011", sha, unattended=True)
    assert snap is not None
    assert snap.next_task is None


def test_dispatchable_resolves_ids_against_eligibility() -> None:
    tasks = parse_plan(MARKED_FALLTHROUGH_PLAN)
    assert dispatchable(tasks, "CBN-012")
    assert not dispatchable(tasks, "CBN-012", unattended=True)
    assert dispatchable(tasks, "CBN-013", unattended=True)
    # Completed rows and ids PLAN never mentions both deny, on either posture.
    assert not dispatchable(tasks, "CBN-011")
    assert not dispatchable(tasks, "CBN-999")
    assert not dispatchable(tasks, "CBN-999", unattended=True)


# The five follow-up section shapes this repo's own archive actually carries
# (CBN-121). Every fixture below is a reduction of a real archived note, so the
# extraction is pinned against producer output rather than against its own idea
# of one.
FENCED_FOLLOW_UPS = """# CBN-120.2 | cohort-walk-trial

## 🚀 Phase 4: Closure

### Follow-ups filed / handed over (2026-08-30, operator-approved)

Flaitron-side — ready-to-paste lines, persisted here per F2's own rule:

```markdown
- [ ] **CORE-XXX** [medium] | blocked-by-closure-sweep — Phase 4 sweeps PLAN.
- [ ] **CORE-XXX** [light] | handoff-persistence-rule — SPEC guidance line.
```

**Final Summary:**

Drained the stem.

**Archived:** 2026-09-05
"""

PROSE_NONE_FOLLOW_UPS = """# CBN-EPIC-15.N | audit

### Follow-up candidates

**None.** F1 was small enough to fix inline; nothing else surfaced warrants a
`/ft-file-followup` filing.

## 🧪 Phase 3: Testing & Linting

- [x] Ran targeted test suite for changed code
"""

TABLE_FOLLOW_UPS = """# CBN-120.N | audit

### Follow-up candidates (file after closure)

| Candidate | Why |
|---|---|
| `CBN-125` | F5's caobunga half. |
"""


def test_extract_follow_ups_reads_items_inside_a_fence() -> None:
    """The case that filed this work: CBN-120.2's lines live in a ```markdown fence."""
    found = extract_follow_ups(FENCED_FOLLOW_UPS)
    assert found == [
        "[ ] **CORE-XXX** [medium] | blocked-by-closure-sweep — Phase 4 sweeps PLAN.",
        "[ ] **CORE-XXX** [light] | handoff-persistence-rule — SPEC guidance line.",
    ]


def test_extract_follow_ups_stops_at_the_next_heading() -> None:
    """A section saying "None." has nothing to file, and reports nothing."""
    assert extract_follow_ups(PROSE_NONE_FOLLOW_UPS) is None


def test_extract_follow_ups_ignores_table_rows() -> None:
    """A documented non-target: a row has no canonical projection to one line."""
    assert extract_follow_ups(TABLE_FOLLOW_UPS) is None


def test_extract_follow_ups_matches_the_spelling_family_not_one_spelling() -> None:
    """Seven spellings live in this archive; `Follow-ups proposed` is only canonical."""
    for heading in (
        "### Follow-ups proposed",
        "### Follow-up candidates",
        "### Follow-ups filed / handed over (2026-08-30)",
        "### follow-up candidate (file after closure)",
    ):
        assert extract_follow_ups(f"{heading}\n\n- file it\n") == ["file it"]
    # Heading-initial, so a section that merely mentions follow-ups is not one.
    assert extract_follow_ups("### The cross-repo follow-up failed\n\n- nope\n") is None
    # `##` is a phase heading, never a follow-up section.
    assert extract_follow_ups("## Follow-ups proposed\n\n- nope\n") is None


def test_extract_follow_ups_caps_the_list_and_clips_each_line() -> None:
    body = "### Follow-ups proposed\n\n" + "".join(f"- item {n}\n" for n in range(25))
    found = extract_follow_ups(body)
    assert found is not None
    assert len(found) == 10
    long_line = "word " * 400
    clipped = extract_follow_ups(f"### Follow-ups proposed\n\n- {long_line}\n")
    assert clipped is not None
    assert len(clipped[0]) <= 501
    assert clipped[0].endswith("…")


def test_inspect_close_carries_follow_ups_off_the_archived_note(tmp_path: Path) -> None:
    """The read target is the archived note — the surface an unattended run seals."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _archive_note(repo, "CBN-011")
    archived = repo / ".flaitron" / "tasknote" / "archive" / "cbn" / "CBN-011.md"
    archived.write_text(
        archived.read_text(encoding="utf-8")
        + "\n### Follow-ups proposed\n\n- **CBN-999** | guard — the discovery.\n",
        encoding="utf-8",
    )
    _commit(repo, "close CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.follow_ups == ["**CBN-999** | guard — the discovery."]


_TEMPLATE_BOX = (
    "- [x] **External review** — a context that did not write the diff graded it "
    "against `## ✅ Acceptance` … `N/A` with a one-line reason when the diff is too "
    "small to grade"
)


def _full_note(box: str) -> str:
    return f"## 🧪 Phase 3: Testing & Linting\n\n- [x] Ran tests\n\n{box}\n"


def test_external_review_box_reads_each_disposition_shape() -> None:
    """The shapes the fleet's archives actually carry (CBN-257 Discovery survey)."""
    assert external_review_box(_full_note(_TEMPLATE_BOX)) == "ticked"
    assert external_review_box(_full_note("- [x] **External review**")) == "ticked"
    assert (
        external_review_box(_full_note("- [x] **External review**: `/code-review medium` clean"))
        == "ticked"
    )
    # Text before the label that is not `N/A` does not change the read.
    assert external_review_box(_full_note("- [x] (headless) **External review** — ran")) == "ticked"
    for na in (
        "- [x] **External review** — N/A: a two-line prose correction",
        "- [x] **External review** — N/A, one-line reason: tasknote only",
        "- [x] **External review** — `N/A` — asset-only diff",
        "- [X] **External review**: n/a. No diff exists to grade",
        "- [x] N/A (De-scope) — **External review** — a context that did not write …",
        "- [x] **N/A** — **External review** — x",
        "- [x] **External review** — **N/A**: tiny diff",
        "- [x] **External review** — _N/A_ nothing to grade",
    ):
        assert external_review_box(_full_note(na)) == "na", na


def test_external_review_box_reads_the_na_written_below_the_box() -> None:
    """CBN-337: the ticked box's `N/A` in Testing Notes, as ten cbn archives wrote it."""
    for below in (
        "- External review: N/A — ~8-line change with two direct tests.",
        "- **External review:** `N/A` — the diff is one condition in `loop.py`",
        "External review: `N/A` — the diff contains no source change.",
        "**Testing Notes:** Docs-only; `grep -c` → 4. External review: N/A — small move.",
    ):
        note = _full_note(_TEMPLATE_BOX) + f"\n**Testing Notes:**\n\n{below}\n"
        assert external_review_box(note) == "na", below


def test_external_review_box_below_box_read_stays_in_its_section() -> None:
    """Only the first unfenced line in the box's own `##` section can turn it `na`."""
    base = _full_note(_TEMPLATE_BOX) + "\n**Testing Notes:**\n\n"
    ran = "- External review: `/code-review medium` — no findings."
    assert external_review_box(base + ran + "\n") == "ticked"
    assert external_review_box(base + ran + "\n- External review: N/A — x\n") == "ticked"
    # Quoted in inline code, the phrase is prose about the reader, not a disposition.
    quoted = "Checked that `External review: N/A — …` reads na. External review: /code-review ok"
    assert external_review_box(base + quoted + "\n") == "ticked"
    assert external_review_box(base + "- external review: n/a — tiny\n") == "na"
    fenced = "```\nExternal review: N/A — quoted\n```\n"
    assert external_review_box(base + fenced) == "ticked"
    later = "## 🚀 Phase 4: Closure\n\nExternal review: N/A — in the recap\n"
    assert external_review_box(base + later) == "ticked"
    unticked = _full_note("- [ ] **External review** — …") + "External review: N/A — x\n"
    assert external_review_box(unticked) == "missing"


def test_external_review_box_template_mention_of_na_is_not_na() -> None:
    """A substring match would read every untouched template box as `na`."""
    assert "N/A" in _TEMPLATE_BOX
    assert external_review_box(_full_note(_TEMPLATE_BOX)) == "ticked"


def test_external_review_box_missing_when_unticked_or_absent_on_a_full_note() -> None:
    assert external_review_box(_full_note("- [ ] **External review** — …")) == "missing"
    assert external_review_box(_full_note("")) == "missing"
    # Neither shape recognizable: the conservative read is `missing`, not `na`.
    assert external_review_box("**Final Summary:**\n\nDone.\n") == "missing"


def test_external_review_box_micro_note_is_na() -> None:
    """A pre-v7 micro-tasknote carries no review line by design (SPEC §"🧪 Phase 3")."""
    assert external_review_box("## ⚡ Notes\n\n**Relevance:** Proceed\n") == "na"
    recap = "## ⚡ Notes\n\n**Relevance:** Proceed\n\n## ✅ Recap\n\nDone.\n"
    assert external_review_box(recap) == "na"


def _micro_note(review: str) -> str:
    return f"## ⚡ Notes\n\n**Relevance:** Proceed\n{review}\n\n**Final Summary:** Done.\n"


def test_external_review_box_reads_the_v7_micro_label() -> None:
    """CBN-330: flaitron v7 micro notes record the review as a `## ⚡ Notes` line."""
    assert external_review_box(_micro_note("**External review:** `/code-review medium` clean")) == (
        "ticked"
    )
    # Only a whole `<…>` value is the template placeholder.
    assert external_review_box(
        _micro_note("**External review:** <none found> by /code-review")
    ) == ("ticked")
    for na in (
        "**External review:** N/A — two-line diff",
        "**External review:** `N/A`: tasknote only",
        "**External review:** **N/A** nothing to grade",
    ):
        assert external_review_box(_micro_note(na)) == "na", na
    for unfilled in (
        "**External review:** <findings with disposition …, OR N/A with reason>",
        "**External review:**",
        "**External review:**   ",
        "- **External review:** <findings …>",
        "**External review**: <findings …>",
    ):
        assert external_review_box(_micro_note(unfilled)) == "missing", unfilled


def test_external_review_box_micro_label_ignored_on_a_full_note() -> None:
    """On a full note the box decides; a label in Testing Notes is prose."""
    prose = "**External review:** /code-review medium clean"
    assert external_review_box(_full_note("") + prose + "\n") == "missing"


def test_external_review_box_reads_an_unbolded_box() -> None:
    """CBN-341: a fleet repo's FE-106.2 ticked the box with its bold dropped."""
    fe = "- [x] External review — N/A: verbatim move of ~18 lines, no logic change."
    assert external_review_box(_full_note(fe)) == "na"
    assert external_review_box(_full_note("- [x] External review — /code-review clean")) == (
        "ticked"
    )
    assert external_review_box(_full_note("- [ ] External review")) == "missing"
    # Only a label opening the box text; a mention later in the box is not it.
    mention = "- [x] Ran tests; external review later"
    assert external_review_box(_full_note(mention)) == "missing"


def test_external_review_box_unbolded_box_counts_only_in_phase_3() -> None:
    """A fleet repo's BE-077 lists an unticked unbolded box under Subtasks above its real box."""
    subtask = "## 🧩 Subtasks\n\n- [ ] External review against Acceptance\n\n"
    assert external_review_box(subtask + _full_note("- [x] **External review** — clean")) == (
        "ticked"
    )
    assert external_review_box(subtask + _full_note("- [x] External review — N/A: tiny")) == "na"
    boxless = subtask + _full_note("") + "- External review: N/A — tiny\n"
    assert external_review_box(boxless) == "na"
    # A micro note's label still decides over a checklist line (CBN-330).
    micro = _micro_note("- [ ] External review against Acceptance\n**External review:** N/A — x")
    assert external_review_box(micro) == "na"


def test_external_review_box_reads_a_boxless_phase_3_na() -> None:
    """CBN-341: a fleet repo's BE-284.2 dropped the box and wrote only the disposition."""
    phase_3 = "## Phase 3: Testing Notes\n\n"
    be = "- External review: N/A — ~20-line diff (dependency move + one test)."
    assert external_review_box(phase_3 + be + "\n") == "na"
    assert external_review_box(_full_note("") + "External review: `N/A` — tiny\n") == "na"
    # A non-`N/A` boxless label stays prose (CBN-330), and only Phase 3 counts.
    ran = "- External review: /code-review medium clean\n"
    assert external_review_box(phase_3 + ran) == "missing"
    assert external_review_box(phase_3 + ran + be + "\n") == "missing"
    phase_4 = _full_note("") + "## 🚀 Phase 4: Closure\n\n" + be + "\n"
    assert external_review_box(phase_4) == "missing"
    fenced = phase_3 + "```\n" + be + "\n```\n"
    assert external_review_box(fenced) == "missing"


def test_external_review_box_ignores_a_fenced_micro_label() -> None:
    fenced = "```markdown\n**External review:** clean\n```"
    assert external_review_box(_micro_note(fenced)) == "na"


def test_external_review_box_ignores_a_fenced_box() -> None:
    """A box quoted inside a fence is not the note's own disposition."""
    fenced = f"```markdown\n{_TEMPLATE_BOX}\n```\n"
    assert external_review_box(_full_note(fenced)) == "missing"
    unticked_after = fenced + "- [ ] **External review**\n"
    assert external_review_box(_full_note(unticked_after)) == "missing"


def test_inspect_close_records_external_review_off_the_archived_note(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _archive_note(repo, "CBN-011", follow_ups=_full_note("- [x] **External review** — N/A: tiny"))
    _commit(repo, "close CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.external_review is ExternalReview.NA


def test_inspect_close_records_missing_when_the_archived_note_has_no_box(tmp_path: Path) -> None:
    repo, sha = _closed_at(tmp_path, CLOSED_PLAN)
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.external_review is ExternalReview.MISSING


def test_inspect_close_reads_an_unticked_box_on_a_de_scope_as_na(tmp_path: Path) -> None:
    """A de-scope has no diff to grade; flagging it `missing` is digest noise."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _archive_note(
        repo, "CBN-011", descoped=True, follow_ups=_full_note("- [ ] **External review**")
    )
    _commit(repo, "close CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.DE_SCOPED
    assert snap.external_review is ExternalReview.NA


def test_inspect_close_leaves_follow_ups_unset_when_the_note_proposed_none(
    tmp_path: Path,
) -> None:
    repo, sha = _closed_at(tmp_path, CLOSED_PLAN)
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.follow_ups is None


# `CBN-014` is filed into a launch bucket unmarked (the shape a worker actually
# produces); `CBN-015` into `## Future Opportunities` — the row a queue-scoped
# diff would hide, which is why the diff is not queue-scoped.
FILED_PLAN = CLOSED_PLAN.replace(
    "## Medium\n\n(none)",
    "## Medium\n\n- [ ] **CBN-014** [light] | worker-filed — filed mid-run.",
).replace(
    "## Future Opportunities\n\n(none)",
    "## Future Opportunities\n\n- [ ] **CBN-015** [light] | parked-idea — filed mid-run.",
)


def _baseline(plan: str) -> list[str]:
    return [task.task_id for task in parse_plan(plan)]


def test_diff_new_rows_names_ids_absent_from_the_baseline() -> None:
    rows = diff_new_rows(parse_plan(FILED_PLAN), _baseline(OPEN_PLAN))
    assert rows is not None
    assert [row.task_id for row in rows] == ["CBN-014", "CBN-015"]


def test_diff_new_rows_reports_a_future_opportunities_row() -> None:
    """A queue-scoped diff would hide it; hiding a discovery is the failure mode."""
    rows = diff_new_rows(parse_plan(FILED_PLAN), _baseline(OPEN_PLAN))
    assert rows is not None
    assert "CBN-015" in {row.task_id for row in rows}


def test_diff_new_rows_carries_the_literal_marker_not_the_eligibility_verdict() -> None:
    """`CBN-016` is blocked *and* marked: eligibility denies it, the marker is present."""
    plan = FILED_PLAN.replace(
        "- [ ] **CBN-014** [light] | worker-filed — filed mid-run.",
        "- [ ] **CBN-014** [light] | worker-filed — filed mid-run.\n"
        "- [ ] **CBN-016** [light] [unattended] | blocked-but-marked — Blocked by [[CBN-012]].",
    )
    rows = diff_new_rows(parse_plan(plan), _baseline(OPEN_PLAN))
    assert rows is not None
    state = {row.task_id: row.unattended for row in rows}
    assert state == {"CBN-014": False, "CBN-015": False, "CBN-016": True}


def test_diff_new_rows_ignores_a_row_that_merely_closed() -> None:
    """`CBN-011` moved to `## Completed`; its id was in the baseline, so it is not new."""
    assert diff_new_rows(parse_plan(CLOSED_PLAN), _baseline(OPEN_PLAN)) is None


def test_diff_new_rows_without_a_baseline_is_not_measured() -> None:
    """`None` for an absent baseline — distinct from measured-and-empty."""
    assert diff_new_rows(parse_plan(FILED_PLAN), None) is None
    assert diff_new_rows(parse_plan(FILED_PLAN), []) is None


def test_inspect_close_carries_new_rows_against_the_launch_baseline(tmp_path: Path) -> None:
    repo, sha = _closed_at(tmp_path, FILED_PLAN)
    snap = inspect_close(repo, "CBN-011", sha, baseline=_baseline(OPEN_PLAN))
    assert snap is not None
    assert snap.new_rows is not None
    assert [(row.task_id, row.unattended) for row in snap.new_rows] == [
        ("CBN-014", False),
        ("CBN-015", False),
    ]


def test_inspect_close_leaves_new_rows_unset_when_nothing_was_filed(tmp_path: Path) -> None:
    repo, sha = _closed_at(tmp_path, CLOSED_PLAN)
    snap = inspect_close(repo, "CBN-011", sha, baseline=_baseline(OPEN_PLAN))
    assert snap is not None
    assert snap.new_rows is None


def test_inspect_close_reports_new_rows_on_a_park(tmp_path: Path) -> None:
    """A worker that filed a row and then parked still filed it."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / ".flaitron" / "PLAN.md").write_text(FILED_PLAN, encoding="utf-8")
    note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntitle: t\nstatus: blocked\npark-reason: dependency — needs the pin\n---\n",
        encoding="utf-8",
    )
    _commit(repo, "park CBN-011")
    snap = inspect_close(repo, "CBN-011", sha, baseline=_baseline(OPEN_PLAN))
    assert snap is not None
    assert snap.outcome is Outcome.BLOCKED
    assert snap.new_rows is not None
    assert [row.task_id for row in snap.new_rows] == ["CBN-014", "CBN-015"]


FILING_ROW = "- [ ] **CBN-014** [light] | worker-filed — filed mid-run."


def _file_follow_up(repo: Path, *, also: str | None = None) -> None:
    """Commit a worker's own `/ft-file-followup` filing: CBN-014 into `## Medium`."""
    plan = repo / ".flaitron" / "PLAN.md"
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            "## Medium\n\n(none)", f"## Medium\n\n{FILING_ROW}"
        ),
        encoding="utf-8",
    )
    if also is not None:
        (repo / also).write_text("not a filing\n", encoding="utf-8")
    _commit(repo, "chore: file CBN-014 follow-up")


def _close_over_filing(repo: Path) -> None:
    (repo / ".flaitron" / "PLAN.md").write_text(FILED_PLAN, encoding="utf-8")
    _archive_note(repo, "CBN-011")
    _commit(repo, "close CBN-011")


def test_inspect_close_labels_a_pure_filing_commit_a_filing_not_foreign(tmp_path: Path) -> None:
    """CBN-299: a window commit that only adds rows `new_rows` reports is accounted for."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    _file_follow_up(repo)
    _close_over_filing(repo)
    snap = inspect_close(repo, "CBN-011", sha, baseline=_baseline(OPEN_PLAN))
    assert snap is not None
    assert snap.foreign_commits is None
    assert snap.filed_commits is not None
    assert [c.subject for c in snap.filed_commits] == ["chore: file CBN-014 follow-up"]
    assert snap.new_rows is not None
    assert "CBN-014" in [row.task_id for row in snap.new_rows]


def test_inspect_close_keeps_a_filing_that_also_touches_another_path_foreign(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    _file_follow_up(repo, also="scratch.txt")
    _close_over_filing(repo)
    snap = inspect_close(repo, "CBN-011", sha, baseline=_baseline(OPEN_PLAN))
    assert snap is not None
    assert snap.filed_commits is None
    assert snap.foreign_commits is not None
    assert [c.subject for c in snap.foreign_commits] == ["chore: file CBN-014 follow-up"]


def test_inspect_close_keeps_a_filing_new_rows_does_not_report_foreign(tmp_path: Path) -> None:
    """No baseline, or a baseline that already carried the row: nothing accounts for it."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    _file_follow_up(repo)
    _close_over_filing(repo)
    for baseline in (None, _baseline(FILED_PLAN)):
        snap = inspect_close(repo, "CBN-011", sha, baseline=baseline)
        assert snap is not None
        assert snap.filed_commits is None
        assert snap.foreign_commits is not None
        assert len(snap.foreign_commits) == 1


def test_inspect_close_keeps_a_commit_that_edits_an_existing_row_foreign(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    plan = repo / ".flaitron" / "PLAN.md"
    plan.write_text(
        plan.read_text(encoding="utf-8").replace("reader work.", "reader work, reworded."),
        encoding="utf-8",
    )
    _file_follow_up(repo)
    _close_over_filing(repo)
    snap = inspect_close(repo, "CBN-011", sha, baseline=_baseline(OPEN_PLAN))
    assert snap is not None
    assert snap.filed_commits is None
    assert snap.foreign_commits is not None


def test_inspect_close_keeps_foreign_a_removed_line_that_looks_like_a_patch_header(
    tmp_path: Path,
) -> None:
    """Only lines before the first hunk are header; a removed `-- a/…` line is a removal."""
    repo = _git_repo(tmp_path, OPEN_PLAN + "-- a/note\n")
    sha = git_head(repo)
    assert sha is not None
    plan = repo / ".flaitron" / "PLAN.md"
    plan.write_text(plan.read_text(encoding="utf-8").replace("-- a/note\n", ""), encoding="utf-8")
    _file_follow_up(repo)
    _close_over_filing(repo)
    snap = inspect_close(repo, "CBN-011", sha, baseline=_baseline(OPEN_PLAN))
    assert snap is not None
    assert snap.filed_commits is None
    assert snap.foreign_commits is not None


def test_inspect_close_labels_a_filing_on_a_park_too(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    _file_follow_up(repo)
    note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntitle: t\nstatus: blocked\npark-reason: dependency — needs the pin\n---\n",
        encoding="utf-8",
    )
    _commit(repo, "park CBN-011")
    snap = inspect_close(repo, "CBN-011", sha, baseline=_baseline(OPEN_PLAN))
    assert snap is not None
    assert snap.outcome is Outcome.BLOCKED
    assert snap.foreign_commits is None
    assert snap.filed_commits is not None
    assert len(snap.filed_commits) == 1


def test_inspect_close_reports_foreign_commits_on_a_park(tmp_path: Path) -> None:
    """The commit that moved `HEAD` to the park is not itself foreign."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / "scratch.txt").write_text("filed CBN-999\n", encoding="utf-8")
    _commit(repo, "chore: unrelated sweep filing", author="Someone Else")
    (repo / ".flaitron" / "PLAN.md").write_text(OPEN_PLAN, encoding="utf-8")
    note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntitle: t\nstatus: blocked\npark-reason: dependency — needs the pin\n---\n",
        encoding="utf-8",
    )
    _commit(repo, "park CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.BLOCKED
    assert snap.foreign_commits is not None
    assert len(snap.foreign_commits) == 1
    assert snap.foreign_commits[0].subject == "chore: unrelated sweep filing"


def test_inspect_close_reports_a_stray_status_commit_as_foreign(
    tmp_path: Path,
) -> None:
    """CBN-188.2: nothing writes the last-run card into the target repo anymore,
    so a commit that happens to reuse its old message is foreign like any other."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    (repo / "caobunga-status.md").write_text("# stale\n", encoding="utf-8")
    _commit(repo, "caobunga: update GitHub-facing status", author="caobunga")
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _archive_note(repo, "CBN-011")
    _commit(repo, "close CBN-011")
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.foreign_commits is not None
    assert len(snap.foreign_commits) == 1
    assert snap.foreign_commits[0].subject == "caobunga: update GitHub-facing status"


PARKED_NOTE_WITH_FOLLOW_UPS = """---
title: t
status: blocked
park-reason: dependency — needs the pin
---

### Follow-ups proposed

- Bump the pin so `--unattended` reaches `/ft-file-followup`.
- Rotate `## Completed` past 150 rows.
"""


def _parked(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose candidate came back `blocked` with follow-ups in its open note."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(PARKED_NOTE_WITH_FOLLOW_UPS, encoding="utf-8")
    _commit(repo, "park CBN-011")
    return repo, sha


def test_a_drain_reads_follow_ups_off_a_parked_note(tmp_path: Path) -> None:
    """Under a drain a park is just a run that ended, and nobody is reading it.

    CBN-121 skipped the parked note because its follow-ups are still in front of
    the operator — true at `gated`, and exactly wrong on a drain, where the note
    sits in a tree nobody opens for days (CBN-120.2's failure, one door over).
    """
    repo, sha = _parked(tmp_path)
    snap = inspect_close(repo, "CBN-011", sha, unattended=True, drain=True)
    assert snap is not None
    assert snap.outcome is Outcome.BLOCKED
    assert snap.park_reason == "dependency"
    assert snap.follow_ups == [
        "Bump the pin so `--unattended` reaches `/ft-file-followup`.",
        "Rotate `## Completed` past 150 rows.",
    ]


def test_an_attended_close_still_leaves_a_parked_notes_follow_ups_alone(tmp_path: Path) -> None:
    """The posture is the whole distinction, so `gated` must be unchanged.

    Pinning the negative matters more than the positive here: reading the note at
    every tier would have satisfied the ticket while quietly overturning CBN-121's
    recorded decision for the case that decision was actually about.
    """
    repo, sha = _parked(tmp_path)
    snap = inspect_close(repo, "CBN-011", sha)
    assert snap is not None
    assert snap.outcome is Outcome.BLOCKED
    assert snap.follow_ups is None


def test_a_non_utf8_parked_note_reads_as_no_follow_ups(tmp_path: Path) -> None:
    """Undecodable bytes lose the follow-ups, not the escalation (CBN-EPIC-314.3).

    Called directly: the project reader that finds open notes has its own skip, so
    only a note that goes bad between that read and this one reaches here.
    """
    note = tmp_path / "CBN-011.md"
    note.write_bytes(PARKED_NOTE_WITH_FOLLOW_UPS.encode("utf-8") + b"\n\xff\xfe broken\n")
    open_note = OpenTasknote(task_id="CBN-011", path="CBN-011.md", status="blocked")

    assert read_parked_follow_ups(tmp_path, open_note) is None


def test_a_non_utf8_committed_file_reads_through_git_show(tmp_path: Path) -> None:
    """Committed bytes that are not UTF-8 are content, not a decode error (CBN-318)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    (repo / ".flaitron" / "PLAN.md").write_bytes(OPEN_PLAN.encode("utf-8") + b"\xff\xfe\n")
    _commit(repo, "non-utf-8 plan")

    shown = git_show(repo, ".flaitron/PLAN.md")
    assert shown is not None
    assert shown.startswith(OPEN_PLAN)
    assert "\ufffd" in shown


def test_a_missing_parked_note_reads_as_no_follow_ups(tmp_path: Path) -> None:
    open_note = OpenTasknote(task_id="CBN-011", path="CBN-011.md", status="blocked")

    assert read_parked_follow_ups(tmp_path, open_note) is None


def test_a_parked_note_with_no_follow_ups_records_none_under_a_drain(tmp_path: Path) -> None:
    """`None`, never `[]` — the shape `exclude_none` needs to keep the key off."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    sha = git_head(repo)
    assert sha is not None
    note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntitle: t\nstatus: blocked\npark-reason: dependency — needs the pin\n---\n",
        encoding="utf-8",
    )
    _commit(repo, "park CBN-011")
    snap = inspect_close(repo, "CBN-011", sha, unattended=True, drain=True)
    assert snap is not None
    assert snap.follow_ups is None


def test_inspect_close_falls_back_to_head_when_the_range_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-279: a range git cannot read keeps the HEAD attribution it replaced."""
    repo, start = _closed_at(tmp_path, CLOSED_PLAN)
    _stack_filing(repo)
    monkeypatch.setattr("caobunga.close.list_commits_touching", lambda *_a: None)
    snap = inspect_close(repo, "CBN-011", start)
    assert snap is not None
    assert snap.commit_sha == git_head(repo)


def test_inspect_close_attributes_the_archive_commit_when_the_flip_landed_alone(
    tmp_path: Path,
) -> None:
    """CBN-279: a flip without its archived note is not yet the close."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    start = git_head(repo)
    assert start is not None
    (repo / ".flaitron" / "PLAN.md").write_text(CLOSED_PLAN, encoding="utf-8")
    _commit(repo, "flip CBN-011")
    _archive_note(repo, "CBN-011")
    _commit(repo, "archive CBN-011")
    snap = inspect_close(repo, "CBN-011", start)
    assert snap is not None
    assert snap.commit_sha == git_head(repo)
    assert snap.foreign_commits is not None
    assert [c.subject for c in snap.foreign_commits] == ["flip CBN-011"]


def test_inspect_close_attributes_the_final_flip_after_a_reopen(tmp_path: Path) -> None:
    """CBN-279: a close later reopened does not count; the last closed run does."""
    repo, start = _closed_at(tmp_path, CLOSED_PLAN)
    plan = repo / ".flaitron" / "PLAN.md"
    plan.write_text(OPEN_PLAN, encoding="utf-8")
    _commit(repo, "reopen CBN-011")
    plan.write_text(CLOSED_PLAN, encoding="utf-8")
    _commit(repo, "re-close CBN-011")
    close = _stack_filing(repo)
    snap = inspect_close(repo, "CBN-011", start)
    assert snap is not None
    assert snap.commit_sha == close
