"""Operator-presence read: the tree state the gate refuses on and the report renders."""

from pathlib import Path

import pytest
from test_loop import _git_repo

from caobunga.errors import GitUnreadable
from caobunga.flaitron import read_project
from caobunga.models import ExecutionTier
from caobunga.presence import launch_pick, read_tree, tree_state

# Two marked rows, `CBN-011` first in dispatch order. The open tasknote goes on
# `CBN-012` — the *second* row — which is what makes the coupling observable:
# `gated`'s in-progress override lifts that row to candidate (so its note is
# exempt), while a drain leaves the queue head as candidate (so it is not). An
# open note on the queue head would be candidate either way and show nothing.
# This mirrors the live shape: a fleet repo's open `DEPLOY-017` note against a
# freshly seeded `BE-019` that sorted ahead of it (CBN-123 F3).
COUPLING_PLAN = """# sample

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

IN_PROGRESS = "---\nstatus: in-progress\n---\n"


def _state(repo: Path, tier: ExecutionTier):
    return read_tree(read_project(repo), repo, tier)


def test_a_clean_tree_names_the_candidate_it_is_clean_for(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    state = _state(repo, ExecutionTier.GATED)
    assert state.readable is True
    assert state.clean is True
    assert state.blocking == []
    assert state.candidate == "CBN-011"


def test_the_candidates_own_tasknote_is_exempt(tmp_path: Path) -> None:
    """A stranded `in-progress` note is always uncommitted (CBN-EPIC-17.3).

    Flaitron commits an active note only at Phase 4, inside the atomic closure
    commit, so treating it as foreign dirt would refuse every CBN-109 resume.
    Under `gated` the in-progress override makes that note's own task the
    candidate, so the exemption applies and the tree reads clean.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-012.md").write_text(IN_PROGRESS, encoding="utf-8")
    state = _state(repo, ExecutionTier.GATED)
    assert state.candidate == "CBN-012"
    assert state.exempt_task == "CBN-012"
    assert [entry.task_id for entry in state.dirt] == ["CBN-012"]
    assert state.blocking == []
    assert state.clean is True


def test_the_same_tree_is_dirty_under_a_drain_because_the_candidate_moved(
    tmp_path: Path,
) -> None:
    """The coupling, in one fixture (CBN-130 / CBN-123 F3).

    Nothing about the tree changes between these two reads — the same file is
    uncommitted in both. What changes is the *candidate*: `proceed-on-green`
    drops the in-progress override (CBN-EPIC-17.3), so the queue head `CBN-011`
    is dispatched, the exemption no longer covers `CBN-012`'s note, and the
    operator's own open tasknote becomes a launch blocker. That is the "which
    row you seed decides whether an existing tasknote is foreign dirt" finding,
    reproduced.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-012.md").write_text(IN_PROGRESS, encoding="utf-8")

    gated = _state(repo, ExecutionTier.GATED)
    drain = _state(repo, ExecutionTier.PROCEED_ON_GREEN)

    assert gated.clean is True
    assert drain.clean is False
    assert drain.candidate == "CBN-011"
    assert drain.exempt_task is None
    assert [entry.task_id for entry in drain.blocking] == ["CBN-012"]
    # The counterfactual: the one dispatch this tree would read clean for.
    assert drain.clean_for == "CBN-012"


def test_foreign_dirt_offers_no_counterfactual(tmp_path: Path) -> None:
    """No candidate choice exempts a half-edited source tree, so none is offered.

    `clean_for` is a claim about the gate, and the honest answer here is that
    the operator cleans up (v1 failure policy) rather than picking a row.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-012.md").write_text(IN_PROGRESS, encoding="utf-8")
    (repo / "scratch.txt").write_text("wip\n", encoding="utf-8")

    state = _state(repo, ExecutionTier.PROCEED_ON_GREEN)
    assert len(state.blocking) == 2
    assert state.clean_for is None


def test_two_blocking_tasknotes_offer_no_counterfactual_either(tmp_path: Path) -> None:
    """One candidate exempts one note, so two blocking notes cannot both clear.

    `CBN-099` has no PLAN row at all — a note left behind by work that closed or
    moved — which is what makes both notes blocking at once: no candidate choice
    covers the pair.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    notes = repo / ".flaitron" / "tasknote"
    (notes / "CBN-012.md").write_text(IN_PROGRESS, encoding="utf-8")
    (notes / "CBN-099.md").write_text(IN_PROGRESS, encoding="utf-8")

    state = _state(repo, ExecutionTier.PROCEED_ON_GREEN)
    assert state.candidate == "CBN-011"
    assert {entry.task_id for entry in state.blocking} == {"CBN-012", "CBN-099"}
    assert state.clean_for is None


def test_the_drain_candidate_still_exempts_its_own_note(tmp_path: Path) -> None:
    """Dropping the in-progress *preference* is not dropping the exemption.

    Under a drain the queue head wins the pick — but if that head happens to
    carry an open note (the CBN-109 dead-worker strand), it is still the
    candidate's own note and still exempt. Only the *other* task's note blocks.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    notes = repo / ".flaitron" / "tasknote"
    (notes / "CBN-011.md").write_text(IN_PROGRESS, encoding="utf-8")
    (notes / "CBN-012.md").write_text(IN_PROGRESS, encoding="utf-8")

    state = _state(repo, ExecutionTier.PROCEED_ON_GREEN)
    assert state.candidate == "CBN-011"
    assert state.exempt_task == "CBN-011"
    assert [entry.task_id for entry in state.blocking] == ["CBN-012"]
    assert state.clean_for == "CBN-012"


def test_an_untracked_tasknote_directory_still_classifies(tmp_path: Path) -> None:
    """Pins `-uall` through the mechanism that now depends on it (CBN-130).

    Git stores no empty directories, so this fixture's `.flaitron/tasknote/` is
    wholly untracked at the commit — the ordinary state of a fresh adopter, and
    the case CBN-EPIC-17.3 found by a failing test. Default porcelain collapses
    it to one `?? .flaitron/tasknote/` line, which names a *directory* and
    matches no open note's path; the exempt note would classify as foreign dirt
    and every CBN-109 resume would refuse. Same failure CBN-EPIC-17.3 met through
    the pathspec door, met through the path-matching one — hence the assertion on
    the classified `path`, not merely on the verdict.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    assert not (repo / ".flaitron" / "tasknote" / ".gitkeep").exists()
    (repo / ".flaitron" / "tasknote" / "CBN-012.md").write_text(IN_PROGRESS, encoding="utf-8")

    state = _state(repo, ExecutionTier.GATED)
    assert [entry.path for entry in state.dirt] == [".flaitron/tasknote/CBN-012.md"]
    assert state.clean is True


def test_an_unreadable_status_is_not_clean(tmp_path: Path) -> None:
    """Bias-to-refuse, carried through the report (CBN-EPIC-17.3).

    `readable=False` is a third state, not a flavour of clean: the CLI and the
    dashboard both render it distinctly, and the gate refuses on it.
    """
    plain = tmp_path / "not-a-repo"
    (plain / ".flaitron" / "tasknote").mkdir(parents=True)
    (plain / ".flaitron" / "PLAN.md").write_text(COUPLING_PLAN, encoding="utf-8")

    state = _state(plain, ExecutionTier.GATED)
    assert state.readable is False
    assert state.clean is False
    assert state.blocking == []


def test_a_hung_git_is_unreadable_in_the_report_and_raises_at_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-317: `tree_state` raises for the gate's exit 5; `read_tree` only states it."""
    repo = _git_repo(tmp_path, COUPLING_PLAN)

    def hung(root: Path, **_: object) -> list[str] | None:
        raise GitUnreadable(root)

    monkeypatch.setattr("caobunga.presence.git_porcelain", hung)
    state = _state(repo, ExecutionTier.GATED)
    assert state.readable is False
    assert state.candidate is not None
    with pytest.raises(GitUnreadable):
        tree_state(repo, None, [])


def test_an_unclassifiable_line_blocks_rather_than_slipping_through(tmp_path: Path) -> None:
    """A path this reader cannot recover is foreign dirt, never a silent exemption.

    Git C-quotes a path with a character it considers unusual, and `_porcelain_path`
    declines to unquote. The failure direction of the *parse* therefore matches the
    failure direction CBN-EPIC-17.3 chose for the *read*: what we cannot explain,
    we do not exempt.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    (repo / "café.txt").write_text("wip\n", encoding="utf-8")

    state = _state(repo, ExecutionTier.GATED)
    assert len(state.blocking) == 1
    quoted = state.blocking[0]
    assert quoted.text.startswith('?? "')
    assert quoted.path is None
    assert quoted.task_id is None


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (ExecutionTier.OBSERVE, "CBN-012"),
        (ExecutionTier.GATED, "CBN-012"),
        (ExecutionTier.PROCEED_ON_GREEN, "CBN-011"),
    ],
)
def test_launch_pick_reads_the_projects_own_tier(
    tmp_path: Path, tier: ExecutionTier, expected: str
) -> None:
    """`observe` picks as `gated` would — it never dispatches, so it is informational.

    Reported anyway, on CBN-EPIC-18.3's coverage argument: the report is about
    the repo, and blanking the tier that has not been promoted yet blanks
    exactly the rows a promotion wave needs to see.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-012.md").write_text(IN_PROGRESS, encoding="utf-8")
    candidate = launch_pick(read_project(repo), tier)
    assert candidate is not None
    assert candidate.task_id == expected


def test_an_unmarked_queue_has_no_candidate_and_still_reports(tmp_path: Path) -> None:
    """Nothing eligible is not nothing to say: the tree verdict stands alone."""
    unmarked = COUPLING_PLAN.replace(" [unattended]", "")
    repo = _git_repo(tmp_path, unmarked)
    (repo / "scratch.txt").write_text("wip\n", encoding="utf-8")

    state = _state(repo, ExecutionTier.PROCEED_ON_GREEN)
    assert state.candidate is None
    assert state.exempt_task is None
    assert len(state.blocking) == 1


def test_tree_state_takes_the_notes_it_is_given(tmp_path: Path) -> None:
    """The exemption is a match against open tasknotes, so an empty list exempts nothing.

    Pins the seam the gate uses: `loop._refuse_if_dirty` re-reads the notes at
    dispatch time rather than threading the pick's list down, so this function
    must never source them itself.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-012.md").write_text(IN_PROGRESS, encoding="utf-8")
    read = read_project(repo)
    candidate = launch_pick(read, ExecutionTier.GATED)

    assert tree_state(repo, candidate, read.open_tasknotes).clean is True
    assert tree_state(repo, candidate, []).clean is False


def test_a_worker_filing_is_named_but_never_exempted(tmp_path: Path) -> None:
    """`filing_only` characterizes the blocking set; it does not shrink it (CBN-138.4).

    The whole change is a message, not an exemption: CBN-EPIC-17.3 Q1 fixed the
    candidate's own tasknote as the *only* exempt path, and CBN-138.1 F3 named the
    gap as an unexplained refusal rather than a wrong one. So a tree holding
    nothing but a worker's PLAN filing still blocks — it just says why.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    (repo / ".flaitron" / "PLAN.md").write_text(COUPLING_PLAN + "\n- [ ] **CBN-013**\n", "utf-8")

    state = _state(repo, ExecutionTier.GATED)
    assert state.clean is False
    assert [entry.path for entry in state.blocking] == [".flaitron/PLAN.md"]
    assert state.filing_only is True


def test_a_sidequest_stub_is_a_filing_too(tmp_path: Path) -> None:
    """`/ft-file-followup --park` writes here, so the third filing path counts.

    The directory is wholly untracked at the commit, so porcelain may collapse it
    to one `?? .flaitron/sidequest/` line naming a directory (CBN-130's footgun).
    A prefix test matches that line as readily as a file under it, which is the
    reason the predicate is a prefix rather than a set of known filenames.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    stubs = repo / ".flaitron" / "sidequest"
    stubs.mkdir(parents=True)
    (stubs / "CBN-014.md").write_text("---\nstatus: starter\n---\n", encoding="utf-8")

    state = _state(repo, ExecutionTier.GATED)
    assert state.clean is False
    assert state.filing_only is True
    assert all(
        entry.path is not None and entry.path.startswith(".flaitron/sidequest/")
        for entry in state.blocking
    )


def test_one_line_of_operator_dirt_sinks_the_whole_filing_verdict(tmp_path: Path) -> None:
    """All-or-nothing, and this is the case that makes it matter.

    A tree holding a worker's filing *and* a half-edited source file is not a
    filing to commit — telling the operator it is would be advice to stage their
    own unreviewed work. So one foreign line returns the generic verdict, which
    is also what keeps every CBN-EPIC-17.3 message assertion true unedited.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    (repo / ".flaitron" / "PLAN.md").write_text(COUPLING_PLAN + "\n- [ ] **CBN-013**\n", "utf-8")
    (repo / "scratch.txt").write_text("operator edit\n", encoding="utf-8")

    state = _state(repo, ExecutionTier.GATED)
    assert len(state.blocking) == 2
    assert state.filing_only is False


def test_an_unrecoverable_path_is_never_a_filing(tmp_path: Path) -> None:
    """Bias-to-refuse survives into the message, not just the verdict.

    `_porcelain_path` declines to unquote a C-quoted path, so the line carries
    `path=None`. Reading that as a filing would let a name nobody could parse
    borrow the reassuring message — the same fail-open CBN-130 refused for the
    exemption itself, one layer up.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    (repo / "café.txt").write_text("unusual\n", encoding="utf-8")

    state = _state(repo, ExecutionTier.GATED)
    assert [entry.path for entry in state.blocking] == [None]
    assert state.filing_only is False


def test_a_clean_tree_claims_no_filing(tmp_path: Path) -> None:
    """There is no blocking set to characterize, so the flag is False, not vacuous True.

    `all()` over an empty list is True, which would have made every clean tree
    report a filing — the one bug a bare comprehension invites here.
    """
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    state = _state(repo, ExecutionTier.GATED)
    assert state.clean is True
    assert state.filing_only is False


def test_leftover_status_snippet_is_ordinary_dirt(tmp_path: Path) -> None:
    """The CBN-154 exemption is gone (CBN-188.3): nothing Caobunga writes lands in
    the target repo any more, so a stale card is the operator's to `git rm`."""
    repo = _git_repo(tmp_path, COUPLING_PLAN)
    (repo / "caobunga-status.md").write_text("# stale card\n", encoding="utf-8")
    state = _state(repo, ExecutionTier.GATED)
    assert [entry.path for entry in state.blocking] == ["caobunga-status.md"]
    assert state.clean is False
