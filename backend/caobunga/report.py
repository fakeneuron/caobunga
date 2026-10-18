"""The report surfaces: last-run card and epic rollup.

`caobunga.report` owns what happens *after* a close is written — the card every
close gets and the rollup a just-completed epic cohort gets. `report` is the
per-close entry `finish.finish` calls. A leaf module: `errors`, the flaitron
façade, `status_card`, `models`, `rollup`, `store`, and `text` cross in, and
nothing calls back into `loop.py`. Split out of `loop.py` at CBN-EPIC-232.6.

The Hermes review — its brief, cadence window, verdict parse, and delivery —
lived here until CBN-258 retired it (verdict: CBN-242). Old run records keep
their `hermes_*` keys: readable, no longer written.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from caobunga.errors import ReportError, UserError, WorkerError
from caobunga.flaitron import PLAN_REL
from caobunga.models import RegistryRecord, RunRecord
from caobunga.rollup import maybe_write_rollup
from caobunga.status_card import publish_status as write_status_card
from caobunga.store import Store
from caobunga.text import read_text_or_none


def report(
    store: Store,
    record: RegistryRecord,
    run: RunRecord,
    *,
    echo: Callable[[str], None] | None,
    publish: Callable[[Store, RunRecord], Path] | None,
) -> None:
    """Run the report surfaces. A failed card write escalates as `ReportError`."""
    root = Path(record.path)
    try:
        # The card write is the less important of the two surfaces, so its
        # failure is held and re-raised at the end rather than skipping the
        # rollup (CBN-EPIC-9.5 F4).
        publisher = publish if publish is not None else write_status_card
        github_error: Exception | None = None
        try:
            status_path = publisher(store, run)
        except (UserError, WorkerError) as exc:
            github_error = exc
            if echo is not None:
                echo(f"card: failed: {exc}")
        else:
            if echo is not None:
                echo(f"card: {status_path}")
        _rollup(store, root, run, echo)
        if github_error is not None:
            # Every other surface landed; still escalate (v1 failure policy).
            raise ReportError(str(github_error), run) from github_error  # noqa: TRY301
    except ReportError:
        raise
    except (UserError, WorkerError) as exc:
        raise ReportError(str(exc), run) from exc


def _rollup(store: Store, root: Path, run: RunRecord, echo: Callable[[str], None] | None) -> None:
    """Write the epic rollup if this close completed a cohort.

    A PLAN missing or unreadable here skips the rollup: the run record is already
    written, and the rollup is derived from it (CBN-318). Only the unreadable case
    is echoed — a missing PLAN is not news at this point.
    """
    plan_path = root / PLAN_REL
    plan_text = read_text_or_none(plan_path)
    if plan_text is None:
        if plan_path.exists() and echo is not None:
            echo(f"rollup: skipped ({PLAN_REL} unreadable)")
        return
    rollup_path = maybe_write_rollup(store, run.project, run.task_id, plan_text)
    if rollup_path is not None and echo is not None:
        echo(f"rollup: {rollup_path}")
