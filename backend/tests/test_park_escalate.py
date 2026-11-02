"""Run signal fields (CBN-EPIC-12.2): park reasons, escalations, exit summaries."""

from __future__ import annotations

from pathlib import Path

from conftest import (
    OPEN_PLAN,
    FakeWorker,
    _BusyWorker,
    _close_worker,
    _closing_client,
    _ExitedWorker,
    _git_repo,
    _note,
    _register,
    _run,
    _tail,
)

from caobunga.errors import WorkerError
from caobunga.models import Escalation, Outcome
from caobunga.prompt import PARK_CHECK
from caobunga.store import Store
from caobunga.worker import STATUS_ERROR, STATUS_EXITED, Session

# --- run signal fields (CBN-EPIC-12.2) -----------------------------------


def test_started_at_precedes_closed_at(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with _closing_client(fake, lambda _msg: _close_worker(repo)) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.CLOSED
    assert result.started_at is not None
    assert result.started_at <= result.closed_at
    assert result.escalation_reason is None


def test_worker_parked_records_escalation(tmp_path: Path, caobunga_home: Path) -> None:
    """The worker flipped the note to blocked mid-run — distinct from note-blocked."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked"), encoding="utf-8")

    with _closing_client(fake, park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.BLOCKED
    assert result.escalation_reason is Escalation.WORKER_PARKED


def test_park_reason_is_absent_when_the_note_carries_none(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Legacy parks and pre-`--unattended` notes keep the bare summary."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked"), encoding="utf-8")

    with _closing_client(fake, park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.park_reason is None
    assert result.summary == "tasknote parked (status: blocked)"
    assert "park_reason" not in store.run_file(result).read_text(encoding="utf-8")


def test_worker_parked_records_park_reason(tmp_path: Path, caobunga_home: Path) -> None:
    """The mid-run site reads the reason the worker just wrote."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked", "drift — Discovery found a different root cause"), "utf-8")

    with _closing_client(fake, park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_PARKED
    assert result.park_reason == "drift"
    assert result.summary == (
        "tasknote parked (status: blocked): Discovery found a different root cause"
    )


def test_worker_parked_records_the_prose_unclipped(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-448: the prose, question last, rides its own field beside `summary`'s lede."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    question = "Should the reader keep the v1 shape or move to v2?"
    prose = f"{'Discovery found the cited reader moved and the fixture drifted. ' * 9}{question}"

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked", f"input-needed — {prose}"), "utf-8")

    with _closing_client(fake, park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.park_reason == "input-needed"
    assert result.park_prose == prose
    assert result.summary == f"tasknote parked (status: blocked): {prose}"
    assert "park_prose:" in store.run_file(result).read_text(encoding="utf-8")


def test_park_prose_is_absent_for_a_bare_code(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("blocked", "out-of-scope"), "utf-8")

    with _closing_client(fake, park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.park_reason == "out-of-scope"
    assert result.park_prose is None
    assert "park_prose" not in store.run_file(result).read_text(encoding="utf-8")


def test_park_reason_without_blocked_status_is_worker_exited(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-327: a `park-reason:` on an `in-progress` note is not a park.

    A fleet repo's CORE-243 shape (CBN-274's second Codex retry). The worker is
    now asked to check this before exiting, but close detection reads `status:`
    alone, so a worker that ignores the cue still fails rather than reading as
    parked.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def half_park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(_note("in-progress", "input-needed — bare pyright is red"), "utf-8")
        fake.state = STATUS_EXITED

    with _closing_client(fake, half_park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=600)
    assert PARK_CHECK in fake.messages[0]
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert result.park_reason is None
    # CBN-328: the mismatch is named in the summary, still never read as a park.
    assert "park-reason present but status in-progress, not blocked" in result.summary


def test_worker_exited_without_park_reason_names_no_mismatch(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-328: an `in-progress` note with no `park-reason:` gets no mismatch clause."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def plain_exit(_message: str) -> None:
        fake.state = STATUS_EXITED

    with _closing_client(fake, plain_exit) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=600)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert "park-reason" not in result.summary


def test_worker_parked_survives_a_colon_in_the_park_prose(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-186: a fleet repo's APP-74 shape — `: ` in the prose breaks the note's YAML.

    Before the reader's line-wise fallback the parked note dropped out of the open
    set, `inspect_close` never reached its parked branch, and the run recorded
    `failed / worker-exited` with the worker's unrelated stderr as the cause.
    """
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def park(_message: str) -> None:
        note = repo / ".flaitron" / "tasknote" / "CBN-011.md"
        note.write_text(
            _note("blocked", "visual-confirm — Phase 3 👁️ CONFIRM: header renders"), "utf-8"
        )

    with _closing_client(fake, park) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.BLOCKED
    assert result.escalation_reason is Escalation.WORKER_PARKED
    assert result.park_reason == "visual-confirm"
    assert result.summary == "tasknote parked (status: blocked): Phase 3 👁️ CONFIRM: header renders"


def test_launch_failure_records_escalation(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise WorkerError("worker refused the session")

    fake.launch = boom  # type: ignore[method-assign]
    with fake as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.outcome is Outcome.FAILED
    assert result.escalation_reason is Escalation.WORKER_LAUNCH
    assert result.provider is None


def test_timeout_records_escalation(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()
    with fake as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=0.05)
    assert result is not None
    assert result.escalation_reason is Escalation.TIMEOUT


def test_worker_exited_records_escalation(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def flip(_message: str) -> None:
        fake.state = STATUS_EXITED

    with _closing_client(fake, flip) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=600)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_EXITED


def test_run_record_log_path_is_set_when_the_worker_wrote_one(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-162: the tee path is a run-record field, but only once a file landed."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _BusyWorker(lambda: None, busy_polls=0)

    def close_and_log() -> None:
        assert worker.log_path is not None
        worker.log_path.parent.mkdir(parents=True, exist_ok=True)
        worker.log_path.write_text("[stdout] hi\n")
        _close_worker(repo)

    worker._on_send = close_and_log
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.log_path == str(worker.log_path)


def test_run_record_log_path_is_absent_without_a_file(tmp_path: Path, caobunga_home: Path) -> None:
    """An adapter that ignores `log_path` leaves no dangling pointer."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _BusyWorker(lambda: _close_worker(repo), busy_polls=0)
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.log_path is None


def test_worker_exited_includes_exit_code_and_stderr_line(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-162: a usage-limit refusal, a bad arg, and a crash used to look identical."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(1, "Error: usage limit reached")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert "(exit code 1; stderr: Error: usage limit reached)" in (result.summary or "")


def test_worker_exited_includes_the_last_stdout_line_on_a_clean_exit(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-200: exit 0 usually has no stderr, so the diagnostic is the last stdout line."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(0, "", "Waiting for `just check` to finish.")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    assert "stdout: Waiting for `just check` to finish." in (result.summary or "")
    assert "stderr:" not in (result.summary or "")


class _StuckWorker(_BusyWorker):
    """Never leaves `processing`, but has already captured a stderr/stdout line."""

    def __init__(self, stderr_line: str, stdout_line: str = "") -> None:
        super().__init__(lambda: None, busy_polls=10_000)
        self._stderr_line = stderr_line
        self._stdout_line = stdout_line

    def stderr_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return _tail(self._stderr_line)

    def stdout_tail(self, session: Session) -> tuple[str, ...]:  # noqa: ARG002
        return _tail(self._stdout_line)


def test_timeout_includes_the_last_stderr_line_with_no_exit_code(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """A wedged, still-running worker has no exit code, but its stderr may still help."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _StuckWorker("waiting on a rate limit")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=0.05, grace=0)
    assert result is not None
    assert result.escalation_reason is Escalation.TIMEOUT
    assert "stderr: waiting on a rate limit" in (result.summary or "")
    assert "exit code" not in (result.summary or "")


def test_timeout_includes_both_stderr_and_stdout_lines_when_both_exist(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _StuckWorker("rate limited", "retrying in 30s")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=0.05, grace=0)
    assert result is not None
    assert result.escalation_reason is Escalation.TIMEOUT
    summary = result.summary or ""
    assert "stderr: rate limited" in summary
    assert "stdout: retrying in 30s" in summary


def test_exit_summary_skips_a_trailing_json_brace(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-283: a multi-line JSON error summarized as `stderr: }` (a fleet repo's FE-104)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(1, 'Internal error: {\n  "message": "bad request",\n}\n')
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    summary = result.summary or ""
    assert 'stderr: "message": "bad request",' in summary
    assert "stderr: }" not in summary


def test_exit_summary_prefers_message_over_trailing_json_field(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-285: a `"type": "billing"` field after the message is not the quoted line."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(1, '{\n  "message": "bad request",\n  "type": "invalid"\n}\n')
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert 'stderr: "message": "bad request",' in (result.summary or "")
    assert '"type"' not in (result.summary or "")


def test_exit_summary_prefers_usage_limit_line_and_strips_ansi(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-285: the refusal line beats a newer `"message"` line; a colour reset is no line."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stderr = '\x1b[31mError: usage limit reached\x1b[0m\n{"message": "later"}\n\x1b[0m\n'
    worker = _ExitedWorker(1, stderr)
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    summary = result.summary or ""
    assert "stderr: Error: usage limit reached)" in summary
    assert "\x1b" not in summary


def test_exit_summary_prefers_a_cause_bearing_stdout_line(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-292: a trailing `Follow-up:` aside is not the quoted line (a fleet repo's TEST-004)."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    stdout = (
        "I stopped before scaffolding anything: TEST-004 hasn't been started.\n"
        "- **Task state:** TEST-004 is open in `.flaitron/PLAN.md`.\n"
        "- **Fix:** run `/ft-update` to bump the `.flaitron/core` pin.\n"
        "- **Checks:** 0 errors, 42 passed, 0 failed.\n"
        "- **Follow-up:** I didn't file one, since the cause is a version mismatch.\n"
    )
    worker = _ExitedWorker(0, "Error: stopped\nlast stderr line\n", stdout)
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    summary = result.summary or ""
    assert "stdout: - **Fix:** run `/ft-update`" in summary
    assert "Follow-up" not in summary
    assert "stderr: last stderr line;" in summary


def _stdout_fragment(tmp_path: Path, caobunga_home: Path, stdout: str) -> str:
    """The `stdout:` fragment of a clean-exit summary for a worker that printed `stdout`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    result = _run(
        "app", store=store, worker=_ExitedWorker(0, "", stdout), poll_interval=0, timeout=2
    )
    assert result is not None
    return (result.summary or "").partition("stdout: ")[2]


def test_exit_summary_skips_a_trailing_aside_that_carries_a_marker(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-300: `Follow-up:` / `Note:` / `Next steps:` are commentary, even saying "can't"."""
    stdout = (
        "I stopped before scaffolding anything.\n"
        "- **Fix:** run `/ft-update` to bump the `.flaitron/core` pin.\n"
        "- **Note:** I can\u2019t verify the pin from here.\n"
        "\u2022 Next steps: it's blocked until then.\n"
        "1. Follow-up: I can't file one.\n"
    )
    assert _stdout_fragment(tmp_path, caobunga_home, stdout).startswith("- **Fix:** run")


def test_exit_summary_fix_marker_needs_a_word_start(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-300: `prefix:` is not `fix:`."""
    stdout = "- **Fix:** run `/ft-update`.\n- Config prefix: v2\n"
    assert _stdout_fragment(tmp_path, caobunga_home, stdout).startswith("- **Fix:** run")


def test_exit_summary_cause_outranks_a_newer_stdout_message_line(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-300: a streamed `"message"` JSON line after the cause is not the quoted line."""
    stdout = '- **Fix:** run `/ft-update`.\n{"message": "session closed"}\n'
    assert _stdout_fragment(tmp_path, caobunga_home, stdout).startswith("- **Fix:** run")


def test_punctuation_only_tail_adds_no_fragment(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-283: a tail with nothing informative is omitted, not quoted as `}`."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(1, "}\n]\n")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert "(exit code 1)" in (result.summary or "")
    assert "stderr:" not in (result.summary or "")


def test_worker_exited_folds_tree_snapshot_with_dirt_and_open_tasknote(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-171: an exit-0 with an unclosed diff used to read identically to a no-op."""
    repo = _git_repo(tmp_path, OPEN_PLAN, notes={"CBN-011": _note("in-progress")})
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ExitedWorker(0, "")
    worker._on_send = lambda: (repo / "stray.txt").write_text("left behind\n", encoding="utf-8")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_EXITED
    summary = result.summary or ""
    assert "tree: 1 file(s) left (" in summary
    assert "stray.txt" in summary
    assert "tasknote open" in summary


def test_timeout_folds_tree_snapshot_when_clean_and_no_open_tasknote(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _StuckWorker("")
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=0.05, grace=0)
    assert result is not None
    assert result.escalation_reason is Escalation.TIMEOUT
    summary = result.summary or ""
    assert "tree: 0 file(s) left (no open tasknote)" in summary


def test_worker_error_records_escalation(tmp_path: Path, caobunga_home: Path) -> None:
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    fake = FakeWorker()

    def flip(_message: str) -> None:
        fake.state = STATUS_ERROR

    with _closing_client(fake, flip) as worker:
        result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert "exit code" not in (result.summary or "")
    assert "tree:" not in (result.summary or "")


class _ErroringWorker(_BusyWorker):
    """Reaches `error` on the first poll and reports a concrete exit code.

    Models `LocalWorker` after a non-zero subprocess exit — CBN-110's F2.
    """

    def __init__(self, code: int) -> None:
        super().__init__(lambda: None, busy_polls=0)
        self._code = code

    def status(self, session: Session) -> str:  # noqa: ARG002
        self.polls += 1
        return STATUS_ERROR

    def exit_code(self, session: Session) -> int | None:  # noqa: ARG002
        return self._code


def test_worker_error_includes_exit_code_when_the_adapter_has_one(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-110. A recoverable cause reaches `summary` without a transcript."""
    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _ErroringWorker(17)
    result = _run("app", store=store, worker=worker, poll_interval=0, timeout=2)
    assert result is not None
    assert result.escalation_reason is Escalation.WORKER_ERROR
    assert "(exit code 17)" in (result.summary or "")
