"""Last-run status card (CBN-033; caller-side since CBN-188.2)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from caobunga.models import LandingState, RunRecord, WorktreeRun
from caobunga.store import Store


def format_status(run: RunRecord, parked: Sequence[str] = ()) -> str:
    """Markdown last-run card. Transcript-free by construction.

    `parked` is the invocation's park handoff lines (CBN-443): a drain that went
    on past a park would otherwise overwrite the only card that named it.
    An isolated run's card names its branch and landing state (CBN-EPIC-450.6),
    so a `closed` outcome main did not get never reads as main completion.
    """
    lines = [
        "# Caobunga status",
        "",
        f"- **project:** {run.project}",
        f"- **task:** {run.task_id}",
        f"- **outcome:** {run.outcome.value}",
    ]
    if run.commit_sha:
        lines.append(f"- **commit:** {run.commit_sha}")
    if run.worktree is not None:
        lines.append(f"- **worktree:** {_worktree_line(run.worktree)}")
    if run.next_task:
        lines.append(f"- **next:** {run.next_task}")
    lines.append(f"- **summary:** {run.summary.strip()}")
    lines.extend(f"- **parked:** {line}" for line in parked)
    lines.append("")
    return "\n".join(lines)


def _worktree_line(state: WorktreeRun) -> str:
    """`wt-<ID> landed|not landed|retained[ — detail]`."""
    if state.landing is LandingState.UNLANDED:
        landing = "not landed"
    else:
        landing = state.landing.value if state.landing is not None else "unknown"
    detail = f" — {state.detail}" if state.detail else ""
    return f"{state.branch} {landing}{detail}"


def publish_status(store: Store, run: RunRecord, *, parked: Sequence[str] = ()) -> Path:
    """Overwrite the last-run card at `$CAOBUNGA_HOME/status/{project}.md` (CBN-188.2).

    Caller-side: no write, commit, or rollback in the target repo. `Store.save_status`
    does the one atomic temp-then-`os.replace` write, under the store lock, like
    rollups.
    """
    return store.save_status(run.project, format_status(run, parked))
