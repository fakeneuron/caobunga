"""Loop-side suite runner (CBN-EPIC-202.3). Injected `run=`; no live `just`."""

from __future__ import annotations

import fcntl
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from caobunga.models import RegistryRecord, SuiteVerdict
from caobunga.store import Store
from caobunga.suite import echo_line, run_suite
from caobunga.suite_verbs import TIMEOUTS, VERBS


def _registered_repo(tmp_path: Path, *, justfile: bool = True) -> tuple[Store, Path]:
    store = Store(tmp_path / "home")
    repo = tmp_path / "Code" / "toolkit"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603, S607
    if justfile:
        (repo / "justfile").write_text("test:\n    echo hi\n", encoding="utf-8")
    store.register(RegistryRecord(id="toolkit", path=str(repo)))
    return store, repo


def _has_just(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("caobunga.suite_verbs.shutil.which", lambda _: "/usr/bin/just")


def test_green_suite_runs_every_verb_in_order_under_its_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixed order test → lint → typecheck, each under `suite_verbs.TIMEOUTS` (D3)."""
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)
    seen: list[tuple[tuple[str, ...], float]] = []

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        assert cwd == repo.resolve()
        seen.append((tuple(argv), timeout))
        return 0, f"> backend: pytest\n\n769 passed in 95.9s\n\n{argv[-1]} done\n"

    measured = run_suite(repo, store, run=run)
    assert [argv for argv, _ in seen] == [("just", "test"), ("just", "lint"), ("just", "typecheck")]
    assert [ceiling for _, ceiling in seen] == [TIMEOUTS[verb] for verb in VERBS]
    assert measured.suite.verdict is SuiteVerdict.GREEN
    assert [verb.verb for verb in measured.suite.verbs] == ["test", "lint", "typecheck"]
    assert all(verb.exit_code == 0 for verb in measured.suite.verbs)
    # `detail` is the last non-blank line, never the tail.
    assert measured.suite.verbs[0].detail == "test done"
    assert "769 passed" not in (measured.suite.verbs[0].detail or "")
    # The log carries what the record does not: every verb's full rendering.
    assert measured.log.startswith("verdict: green\n\n")
    assert "run_tests: `just test` in " in measured.log
    assert "769 passed in 95.9s" in measured.log
    assert measured.log.count("tree: unchanged") == 3


def test_red_verb_folds_to_red_and_keeps_its_last_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        if argv[-1] == "lint":
            return 1, "F401 unused import\nFound 1 error.\n"
        return 0, "ok\n"

    measured = run_suite(repo, store, run=run)
    assert measured.suite.verdict is SuiteVerdict.RED
    lint = measured.suite.verbs[1]
    assert (lint.verb, lint.exit_code, lint.detail) == ("lint", 1, "Found 1 error.")


def test_detail_strips_ansi_escapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """vitest's coloured summary reached a run record raw (CBN-276)."""
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        return 0, "\x1b[2m Duration \x1b[22m 6.11s\x1b[2m (tests 7.10s)\x1b[22m\n\x1b[0m\x1b[K\n"

    measured = run_suite(repo, store, run=run)
    assert measured.suite.verbs[0].detail == "Duration 6.11s (tests 7.10s)"


def test_detail_skips_script_echo_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean lint/typecheck ends on the npm/just command echo, not a result (CBN-296)."""
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        if argv[-1] == "test":
            return 0, "769 passed in 95.9s\n\u203a frontend: vitest\n"
        if argv[-1] == "lint":
            return 0, "All checks passed!\n\n> eslint . --max-warnings 0\n"
        return 0, "> frontend@0.1.0 typecheck\n> tsc -b\n"

    test, lint, typecheck = run_suite(repo, store, run=run).suite.verbs
    assert test.detail == "769 passed in 95.9s"
    assert lint.detail == "All checks passed!"
    # Nothing but echoes: no detail, rather than the command named as the result.
    assert typecheck.detail is None


def test_detail_skips_just_recipe_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A red verb ends on just's `error: Recipe ... failed`, not the runner's summary (CBN-311)."""
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        if argv[-1] == "test":
            return 1, "3 failed\nerror: Recipe `test` failed on line 9 with exit code 1\n"
        return 0, "ok\n"

    test = run_suite(repo, store, run=run).suite.verbs[0]
    assert test.detail == "3 failed"


def test_detail_prefers_a_real_count_over_swift_testing_empty_trailer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XCTest's count prints above swift-testing's empty trailer; the count wins (CBN-354)."""
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)
    xctest = "\t Executed 318 tests, with 0 failures (0 unexpected) in 9.1 (9.2) seconds\n"

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        if argv[-1] == "test":
            noise = "◇ Test run started.\n↳ Testing Library Version: 6.0\n"
            return 0, xctest + noise + "✔ Test run with 0 tests in 0 suites passed\n"
        if argv[-1] == "lint":
            # A swift-testing run that did count tests keeps its trailer.
            return 0, "✔ Test run with 5 tests in 2 suites passed after 0.01 seconds\n"
        # Only the empty trailer: it stays rather than leaving `detail` blank.
        return 0, "✔ Test run with 0 tests in 0 suites passed\n"

    test, lint, typecheck = run_suite(repo, store, run=run).suite.verbs
    assert test.detail == "Executed 318 tests, with 0 failures (0 unexpected) in 9.1 (9.2) seconds"
    assert lint.detail == "✔ Test run with 5 tests in 2 suites passed after 0.01 seconds"
    assert typecheck.detail == "✔ Test run with 0 tests in 0 suites passed"


def test_detail_keeps_a_result_line_that_starts_with_an_angle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the runner's `> cmd` shape is an echo; pytest's indented source line is a result."""
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        return 1, "E   assert 1 == 2\n>       assert x == 1\n"

    measured = run_suite(repo, store, run=run)
    assert measured.suite.verbs[0].detail == "> assert x == 1"  # clip collapses the indent


def test_timeout_is_a_result_not_a_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        if argv[-1] == "test":
            raise subprocess.TimeoutExpired(list(argv), timeout, output="collecting ...\n")
        return 0, "ok\n"

    measured = run_suite(repo, store, run=run)
    assert measured.suite.verdict is SuiteVerdict.TIMEOUT
    test = measured.suite.verbs[0]
    assert test.timed_out is True
    assert test.exit_code is None
    assert test.detail == "collecting ..."
    assert "exit: none (timed out)" in measured.log


def test_refusal_records_every_verb_and_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No justfile: three refused entries, verdict `refused`, no subprocess (D5)."""
    store, repo = _registered_repo(tmp_path, justfile=False)
    _has_just(monkeypatch)
    calls: list[str] = []

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        calls.append(argv[-1])
        return 0, ""

    measured = run_suite(repo, store, run=run)
    assert calls == []
    assert measured.suite.verdict is SuiteVerdict.REFUSED
    refusal = f"no justfile at {repo} — nothing to run"
    assert [verb.refused for verb in measured.suite.verbs] == [refusal] * 3
    assert all(verb.exit_code is None and verb.duration == 0.0 for verb in measured.suite.verbs)
    assert "run_tests: refused — no justfile" in measured.log


def test_missing_recipe_folds_refused_beside_green_verbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fleet repo's `CORE-12` (2026-09-20): `test` 0, `lint` 0, `typecheck` exit 1 on
    `justfile does not contain recipe` — a layer gap, not a code failure. Folds
    `refused` (CBN-211), so the drain stop reads cannot-measure and Fleet HQ's
    sweep counts it under Refused, not Red.
    """
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        if argv[-1] == "typecheck":
            return 1, "error: justfile does not contain recipe `typecheck`\n"
        return 0, "ok\n"

    measured = run_suite(repo, store, run=run)
    assert measured.suite.verdict is SuiteVerdict.REFUSED
    test, lint, typecheck = measured.suite.verbs
    assert (test.exit_code, lint.exit_code) == (0, 0)
    assert typecheck.refused == "no `typecheck` recipe in justfile — nothing to run"
    assert typecheck.exit_code is None
    assert typecheck.detail == "error: justfile does not contain recipe `typecheck`"
    assert "run_typecheck: refused — no `typecheck` recipe" in measured.log


def test_runner_error_becomes_a_refused_verb_never_a_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_verb` returns refusals and timeouts; anything else that escapes is caught here."""
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        if argv[-1] == "typecheck":
            raise OSError(13, "Permission denied")
        return 0, "ok\n"

    measured = run_suite(repo, store, run=run)
    assert measured.suite.verdict is SuiteVerdict.REFUSED
    typecheck = measured.suite.verbs[2]
    assert typecheck.refused is not None
    assert typecheck.refused.startswith("PermissionError:")
    assert typecheck.exit_code is None
    assert measured.suite.verbs[0].exit_code == 0


def test_echo_line_names_verdict_and_every_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        if argv[-1] == "lint":
            raise subprocess.TimeoutExpired(list(argv), timeout)
        return (2 if argv[-1] == "test" else 0), "x\n"

    measured = run_suite(repo, store, run=run)
    line = echo_line(measured.suite)
    assert line.startswith("suite: red — test 2 ")
    assert " · lint timeout " in line
    assert " · typecheck 0 " in line
    assert line.endswith("s")


def test_every_verb_runs_under_the_fleet_suite_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent slug's suite cannot overlap this one (CBN-308)."""
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)
    held: list[bool] = []

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        with store.suite_lock_path.open("r+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                held.append(False)
        return 0, "ok\n"

    run_suite(repo, store, run=run)
    assert held == [True, True, True]
    with store.suite_lock():
        pass


def test_a_refused_verb_never_takes_the_suite_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is decided before the spawn, so it does not queue behind a holder (CBN-310)."""
    store, repo = _registered_repo(tmp_path, justfile=False)
    _has_just(monkeypatch)
    waits: list[str] = []
    done = threading.Event()
    verdicts: list[SuiteVerdict] = []

    def measure() -> None:
        verdicts.append(run_suite(repo, store, on_wait=lambda: waits.append("x")).suite.verdict)
        done.set()

    with store.suite_lock():
        worker = threading.Thread(target=measure, daemon=True)
        worker.start()
        assert done.wait(5)
    worker.join()
    assert verdicts == [SuiteVerdict.REFUSED]
    assert waits == []


def test_a_queued_verb_says_it_waits_then_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`on_wait` fires once, when the first verb blocks; the rest find the lock free (CBN-310)."""
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)
    waiting = threading.Event()
    waits: list[str] = []
    verdicts: list[SuiteVerdict] = []

    def on_wait() -> None:
        waits.append("waiting")
        waiting.set()

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:  # noqa: ARG001
        return 0, "ok\n"

    def measure() -> None:
        measured = run_suite(repo, store, run=run, on_wait=on_wait)
        verdicts.append(measured.suite.verdict)

    with store.suite_lock():
        worker = threading.Thread(target=measure, daemon=True)
        worker.start()
        assert waiting.wait(5)
        assert verdicts == []
    worker.join(5)
    assert not worker.is_alive()
    assert waits == ["waiting"]
    assert verdicts == [SuiteVerdict.GREEN]


def test_a_home_that_cannot_lock_still_measures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_suite` never raises: an `flock`-less home runs unserialized (CBN-308)."""
    store, repo = _registered_repo(tmp_path)
    _has_just(monkeypatch)

    def no_flock(_fd: int, _op: int) -> None:
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr("caobunga.store.fcntl.flock", no_flock)
    measured = run_suite(repo, store, run=lambda argv, cwd, timeout: (0, "ok\n"))  # noqa: ARG005
    assert measured.suite.verdict is SuiteVerdict.GREEN
