"""User-facing errors mapped to CONTRACT CLI exit codes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from caobunga.models import Escalation

if TYPE_CHECKING:
    from pathlib import Path

    from caobunga.models import RunRecord


class UserError(Exception):
    """Validation or usage failure. Default exit code 2."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class UnknownProjectError(UserError):
    """`{id}` names no registered project. Exit 2 on the CLI; 404 on the Status API.

    Typed so the API maps it by class rather than by message prefix (CBN-263).
    """

    def __init__(self, project_id: str) -> None:
        super().__init__(f"unknown project: {project_id}")
        self.project_id = project_id


class Unlaunchable(UserError):
    """This machine cannot launch a worker: no installed or pickable provider,
    or `git` cannot answer. CONTRACT exit code 5 (CBN-EPIC-313.5).

    Split from exit 2 so `fleet`, which skips a child's 2 as repo state, folds
    an environment failure to 4 instead of reporting a green fleet.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=5)


class GitUnreadable(Unlaunchable):
    """`git` could not answer at all — it hung past `GIT_TIMEOUT` or is missing (CBN-269).

    Distinct from a nonzero exit, which is git answering "no" (not a repository,
    no such path) and stays `None` at the read. Raised only by the reads whose
    `None` a caller would otherwise report as a repo fact; exit 5 (CBN-EPIC-313.5).
    The dirty-tree read raises it too (CBN-317): only a nonzero `git status`
    still refuses as `cannot read git status`, exit 2.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(f"git unreadable in {root} (timed out or not installed)")
        self.root = root


class HeadLost(UserError):
    """`git` answered "no HEAD" after the worker launched (CBN-322).

    The repo had a `HEAD` at launch, so this is not CBN-321's pre-launch
    `not-a-repo`: the worker's partial work may be all that is left. Exit 2.
    """


class WorkerError(UserError):
    """Worker or orchestrator failure. CONTRACT exit code 4 — escalate.

    `escalation` names which stop site produced the failure, so the run loop
    can record a structured reason on the run record instead of re-deriving
    one from the message string (CBN-EPIC-12.2). Optional: raisers outside
    the run loop leave it unset.
    """

    def __init__(self, message: str, escalation: Escalation | None = None) -> None:
        super().__init__(message, exit_code=4)
        self.escalation = escalation


class NoTaskError(UserError):
    """PLAN exhausted or nothing eligible. CONTRACT exit code 3."""

    def __init__(self, message: str = "no next task") -> None:
        super().__init__(message, exit_code=3)


class ReportError(UserError):
    """A report surface (the last-run card) failed after the run record was written. Exit 4."""

    def __init__(self, message: str, record: RunRecord) -> None:
        super().__init__(message, exit_code=4)
        self.record = record
