"""On-disk ~/.caobunga/ (or $CAOBUNGA_HOME) registry, run records, and report copies."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ValidationError

from caobunga.errors import UnknownProjectError, UserError
from caobunga.models import ID_PATTERN, RegistryRecord, RunRecord, compact_utc
from caobunga.text import read_text_or_none

if TYPE_CHECKING:
    from caobunga.invocation import InvocationResult

ENV_HOME = "CAOBUNGA_HOME"


def default_home() -> Path:
    """Absolute always: a relative `$CAOBUNGA_HOME` would otherwise resolve
    against whatever process's cwd calls this, and the worker subprocess
    (CBN-247's hook) runs rooted in the project cwd, not this one (CBN-251)."""
    raw = os.environ.get(ENV_HOME)
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else Path.cwd() / path
    return Path.home() / ".caobunga"


class Store:
    """File-native store. All mutating methods take the exclusive lock."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home if home is not None else default_home()

    @property
    def lock_path(self) -> Path:
        return self.home / "lock"

    def projects_dir(self) -> Path:
        return self.home / "projects"

    def project_file(self, project_id: str) -> Path:
        """Registry record path for `project_id`.

        Validates the slug shape first (rather than trusting the caller) so a
        `../`-style `project_id` can't build a path outside `projects_dir()` —
        every caller of `get`/`project_file` benefits from the one check.
        """
        if not ID_PATTERN.fullmatch(project_id):
            raise UnknownProjectError(project_id)
        return self.projects_dir() / f"{project_id}.yaml"

    def runs_dir(self, project_id: str) -> Path:
        return self.home / "runs" / project_id

    def invocation_file(self, invocation_id: str) -> Path:
        # IDs are filename keys, never user-supplied project paths.
        if not invocation_id or any(c not in "0123456789TZabcdef-" for c in invocation_id):
            raise UserError("invalid invocation id")
        return self.home / "invocations" / f"{invocation_id}.yaml"

    def write_invocation(self, result: InvocationResult) -> Path:
        with self.locked():
            path = self.invocation_file(result.receipt_id)
            self._write_yaml(path, result)
            return path

    def load_invocation(self, invocation_id: str) -> InvocationResult:
        from caobunga.invocation import InvocationResult

        path = self.invocation_file(invocation_id)
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return InvocationResult.model_validate(raw)

    def running_invocations(self, project_id: str) -> list[InvocationResult]:
        """This project's receipts still at `status: running`, oldest first (CBN-376).

        A text prefilter skips terminal receipts before the YAML parse, since the
        directory is global and only grows. Skip-not-fail like `list_runs`: an
        unreadable receipt is not a reason to stop a run.
        """
        from caobunga.invocation import InvocationResult

        directory = self.home / "invocations"
        if not directory.is_dir():
            return []
        found: list[InvocationResult] = []
        for path in sorted(directory.glob("*.yaml")):
            text = read_text_or_none(path)
            if text is None or "\nstatus: running\n" not in text:
                continue
            try:
                result = InvocationResult.model_validate(yaml.safe_load(text))
            except (yaml.YAMLError, ValidationError):
                continue
            # A body naming another file would be rewritten there, leaving this one running.
            if path.stem != result.receipt_id:
                continue
            if result.project == project_id and result.status.value == "running":
                found.append(result)
        return found

    def rollups_dir(self, project_id: str) -> Path:
        return self.home / "rollups" / project_id

    def status_dir(self) -> Path:
        return self.home / "status"

    def status_file(self, project_id: str) -> Path:
        return self.status_dir() / f"{project_id}.md"

    def logs_dir(self, project_id: str) -> Path:
        return self.home / "logs" / project_id

    def suites_dir(self, project_id: str) -> Path:
        return self.home / "suites" / project_id

    def log_file(self, project_id: str, invocation_id: str, task_id: str) -> Path:
        """Worker stdout/stderr tee target. Diagnostics only — never read by a report surface."""
        return self.logs_dir(project_id) / f"{invocation_id}-{task_id}.log"

    def rollup_file(self, project_id: str, closed_at_stamp: str, epic_key: str) -> Path:
        safe_key = epic_key.replace("/", "-")
        return self.rollups_dir(project_id) / f"{closed_at_stamp}-{safe_key}.md"

    def run_file(self, record: RunRecord) -> Path:
        name = f"{compact_utc(record.closed_at)}-{record.task_id}.yaml"
        return self.runs_dir(record.project) / name

    def suite_file(self, project_id: str, closed_at_stamp: str, task_id: str) -> Path:
        # The loop-side suite's rendered output, under the run's own stamp and
        # name on the CBN-148 rule (CBN-EPIC-202.3): the record's `suite` field
        # carries the fold and one clipped line per verb, this file the rest.
        return self.suites_dir(project_id) / f"{closed_at_stamp}-{task_id}.log"

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._blocking_flock(self.lock_path):
            yield

    def run_locks_dir(self) -> Path:
        return self.home / "run-locks"

    def run_lock_path(self, repo_path: str | Path) -> Path:
        """Lock file for the repo at `repo_path`, keyed on its identity (CBN-EPIC-313.2).

        `_same_repo`'s rule — resolved, case-folded — hashed to a filename, so
        two registry ids at one repo share a lock file. `execute_run` passes the
        record path's git toplevel (CBN-EPIC-313.6), so ids at nested paths of
        one checkout share it too; a reader of a live lock must key the same way.
        """
        key = str(Path(repo_path).expanduser().resolve()).casefold()
        return self.run_locks_dir() / f"{hashlib.sha256(key.encode()).hexdigest()}.lock"

    @contextmanager
    def run_lock(self, project_id: str, repo_path: str | Path) -> Iterator[None]:
        """Per-repo mutex held across one run (CBN-EPIC-16.2, CBN-EPIC-313.2).

        `locked()`'s sibling with the opposite trade: non-blocking and
        per-repo instead of blocking and global, because the caller holds it
        for a whole worker run rather than one file write — a second concurrent
        run on the same repo must fail fast, not queue behind a session that can
        take hours. Keyed on the repo path, not the slug, so two ids registered
        at one repo (which `fleet` runs side by side) still exclude each other;
        `project_id` only names the refused run. `flock` dies with the process,
        so a crashed run leaves no stale lock to clean up.
        """
        path = self.run_lock_path(repo_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        with path.open("r+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UserError(
                    f"another caobunga run is already active for {project_id}'s repo "
                    f"{repo_path}; not launching a second"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    @property
    def suite_lock_path(self) -> Path:
        return self.home / "suite.lock"

    @contextmanager
    def suite_lock(self, on_wait: Callable[[], None] | None = None) -> Iterator[None]:
        """Fleet-wide mutex held across one verb's spawn of a close's suite (CBN-308).

        Blocking and global, like `locked()`, but its own file: the holder runs
        minutes of `just test` / `lint` / `typecheck`, and every registry and
        run write must not queue behind that. Different slugs still run
        concurrently; only their verbs take turns, so one slug's test pool
        cannot load another's into a false red. `on_wait` is called once, only
        when the lock is held elsewhere, just before this call blocks (CBN-310).
        """
        with self._blocking_flock(self.suite_lock_path, on_wait):
            yield

    @contextmanager
    def _blocking_flock(
        self, path: Path, on_wait: Callable[[], None] | None = None
    ) -> Iterator[None]:
        self.home.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        with path.open("r+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if on_wait is not None:
                    # Advisory: an echo to a closed pipe must not skip the lock below,
                    # nor be mistaken for an `flock`-less mount by the caller (CBN-310).
                    with contextlib.suppress(OSError):
                        on_wait()
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def get(self, project_id: str) -> RegistryRecord:
        path = self.project_file(project_id)
        if not path.is_file():
            raise UnknownProjectError(project_id)
        return self._load_registry(path)

    def list_projects(self) -> list[RegistryRecord]:
        directory = self.projects_dir()
        if not directory.is_dir():
            return []
        records = [
            self._load_registry(path)
            for path in sorted(directory.glob("*.yaml"))
            if not path.name.endswith(".tmp")
        ]
        return sorted(records, key=lambda rec: rec.id)

    def find_by_path(self, path: Path) -> RegistryRecord | None:
        """The registry record whose `path` names this directory, else None.

        Resolves symlinks and compares by `_same_repo` (case-fold, CBN-131), so
        a caller may pass `~/Code/foo` against a record written `~/code/foo`.
        The suite runner (`suite_verbs.run_verb`, CBN-174) is the consumer:
        "registered" is its whole bound, and the rule that decides identity
        lives here.
        """
        incoming = str(path.expanduser().resolve())
        for record in self.list_projects():
            if _same_repo(record.path, incoming):
                return record
        return None

    def register(self, record: RegistryRecord, *, replace: bool = False) -> RegistryRecord:
        with self.locked():
            existing_path = self.project_file(record.id)
            if existing_path.is_file():
                current = self._load_registry(existing_path)
                if not _same_repo(current.path, record.path) and not replace:
                    raise UserError(
                        f"id {record.id!r} is already registered at {current.path}; "
                        "pass --replace to overwrite"
                    )
                if _same_repo(current.path, record.path):
                    updated = current.model_copy(update=_merge(record))
                    self._write_yaml(existing_path, updated)
                    return updated
            self._write_yaml(existing_path, record)
            return record

    def write_run(self, record: RunRecord) -> Path:
        with self.locked():
            path = self.run_file(record)
            self._write_yaml(path, record)
            return path

    def last_run(self, project_id: str) -> RunRecord | None:
        """Newest *readable* record, walking back past any that will not load."""
        for path in reversed(self._run_files(project_id)):
            record = self._load_run(path)
            if record is not None:
                return record
        return None

    def save_suite(self, project_id: str, closed_at_stamp: str, task_id: str, body: str) -> Path:
        """Keep the suite's full rendered output beside the run it measured (CBN-EPIC-202.3)."""
        with self.locked():
            return self._write_text(self.suite_file(project_id, closed_at_stamp, task_id), body)

    def save_rollup(self, project_id: str, closed_at_stamp: str, epic_key: str, body: str) -> Path:
        with self.locked():
            return self._write_text(self.rollup_file(project_id, closed_at_stamp, epic_key), body)

    def save_status(self, project_id: str, body: str) -> Path:
        """Overwrite the one-per-project last-run card (CBN-188.2)."""
        with self.locked():
            return self._write_text(self.status_file(project_id), body)

    def has_rollup(self, project_id: str, epic_key: str) -> bool:
        directory = self.rollups_dir(project_id)
        if not directory.is_dir():
            return False
        safe_key = epic_key.replace("/", "-")
        return any(path.name.endswith(f"-{safe_key}.md") for path in directory.glob("*.md"))

    def last_rollup(self, project_id: str) -> str | None:
        return self._last_sorted_text(self.rollups_dir(project_id), "*.md")

    def list_runs(self, project_id: str) -> list[RunRecord]:
        """Every readable record for one project, in filename order.

        Unreadable records are dropped, not raised (CBN-106): every consumer of
        this list folds it — `stats.fleet_stats` across the whole fleet — so one
        bad file would otherwise fail the read for every project.
        """
        loaded = (self._load_run(path) for path in self._run_files(project_id))
        return [record for record in loaded if record is not None]

    def runs_for_tasks(self, project_id: str, task_ids: frozenset[str]) -> list[RunRecord]:
        return [run for run in self.list_runs(project_id) if run.task_id in task_ids]

    def _run_files(self, project_id: str) -> list[Path]:
        """Run-record paths for one project, oldest first. `.tmp` writes excluded.

        Filename order is `closed_at` order — `run_file` keys the name on the
        stamp — which is why `last_run` and `list_runs` can sort by name.
        """
        directory = self.runs_dir(project_id)
        if not directory.is_dir():
            return []
        return sorted(
            (p for p in directory.glob("*.yaml") if not p.name.endswith(".tmp")),
            key=lambda p: p.name,
        )

    def _last_sorted_text(self, directory: Path, pattern: str) -> str | None:
        """The newest readable file's text; `None` when none is (CBN-318).

        Falls back past an unreadable newest, the way `last_run` does, so one bad
        file does not hide an older readable one.
        """
        if not directory.is_dir():
            return None
        files = sorted(
            (p for p in directory.glob(pattern) if not p.name.endswith(".tmp")),
            key=lambda p: p.name,
        )
        for path in reversed(files):
            text = read_text_or_none(path)
            if text is not None:
                return text
        return None

    def _load_registry(self, path: Path) -> RegistryRecord:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise UserError(f"corrupt registry file: {path}")
        return RegistryRecord.model_validate(raw)

    def _load_run(self, path: Path) -> RunRecord | None:
        """One run record, or `None` when the file cannot be read as one.

        Skip-not-fail, extending the shape `flaitron._tasknote_status` already
        uses for malformed tasknote frontmatter (CBN-EPIC-7.4). Three inputs
        reach here and all three are treated alike: unparseable YAML, valid YAML
        that is not a mapping, and a mapping that fails the schema. Before
        CBN-106 the middle one raised `UserError` (→ 400) and the other two
        escaped `api.install_exception_handlers` entirely as 500s, in both cases
        taking down `/api/stats` for the whole fleet over one file. A file that
        cannot be read at all — bytes that are not UTF-8, or one that vanished or
        turned unreadable after `_run_files` listed it — is the same skip
        (CBN-EPIC-314.3).

        Deliberately asymmetric with `_load_registry`, which still raises: the
        registry is explicitly-registered operator state, and skipping a corrupt
        entry there would silently unregister a project.
        """
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError, OSError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return RunRecord.model_validate(raw)
        except ValidationError:
            return None

    def _write_text(self, path: Path, body: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
        return path

    def _write_yaml(self, path: Path, model: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = model.model_dump(mode="json", exclude_none=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def _same_repo(stored: str, incoming: str) -> bool:
    """Do two resolved paths name the same repo? Case-insensitively (CBN-131).

    macOS is case-insensitive but case-*preserving*, and `Path.resolve()` does
    not normalize case, so `~/Code/foo` and a record written as `~/code/foo`
    are one directory that stringifies two ways. `docs/CONTRACT.md`
    §"Entry points" already binds the in-repo skill to this rule; comparing
    raw strings here made the two entry points disagree about repo identity.

    `casefold()` rather than `os.path.normcase()` (the identity function on
    POSIX, so a no-op on the platform the mismatch occurs on) and rather than
    `.lower()` (ASCII-only): full case folding is what APFS/HFS+ do. The
    difference is unreachable for ASCII repo paths, so this stays in step with
    the skill's `tr '[:upper:]' '[:lower:]'`.
    """
    return stored.casefold() == incoming.casefold()


def _merge(record: RegistryRecord) -> dict[str, object | None]:
    """Fields a re-register writes over an existing record at the same path.

    The provider pin and the allow-list are mutually exclusive, so each clears
    the other rather than merging beside it (CBN-EPIC-95.3 F0): a record holding
    both makes `picker.pick` short-circuit to the pin and silently ignore the
    allow-list. Pins move as a unit — `--provider` without `--model` drops a
    stale model pin. An update naming neither leaves both alone.
    """
    updates: dict[str, object | None] = {"tier": record.tier}
    if record.provider is not None:
        updates["provider"] = record.provider
        updates["model"] = record.model
        updates["providers"] = None
    elif record.providers is not None:
        updates["providers"] = record.providers
        updates["provider"] = None
        updates["model"] = None
    return updates
