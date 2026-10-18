"""Last-run status card (CBN-033; caller-side since CBN-188.2)."""

from __future__ import annotations

from pathlib import Path

from caobunga.models import RunRecord
from caobunga.store import Store


def format_status(run: RunRecord) -> str:
    """Markdown last-run card. Transcript-free by construction."""
    lines = [
        "# Caobunga status",
        "",
        f"- **project:** {run.project}",
        f"- **task:** {run.task_id}",
        f"- **outcome:** {run.outcome.value}",
    ]
    if run.commit_sha:
        lines.append(f"- **commit:** {run.commit_sha}")
    if run.next_task:
        lines.append(f"- **next:** {run.next_task}")
    lines.append(f"- **summary:** {run.summary.strip()}")
    lines.append("")
    return "\n".join(lines)


def publish_status(store: Store, run: RunRecord) -> Path:
    """Overwrite the last-run card at `$CAOBUNGA_HOME/status/{project}.md` (CBN-188.2).

    Caller-side: no write, commit, or rollback in the target repo. `Store.save_status`
    does the one atomic temp-then-`os.replace` write, under the store lock, like
    rollups.
    """
    return store.save_status(run.project, format_status(run))
