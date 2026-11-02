"""`caobunga fleet`: bounded pool of child `run`s, one receipt (CBN-312)."""

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner

import caobunga.cli as cli_module
import caobunga.fleet as fleet_module
from caobunga.cli import cli
from caobunga.errors import RECEIPT_UNWRITTEN_PREFIX, UserError
from caobunga.fleet import RECEIPT_PREFIX, FleetLeg, fleet_exit, run_fleet, spawn_run
from caobunga.loop import execute_run
from caobunga.models import (
    ExecutionTier,
    InvocationPark,
    InvocationResult,
    InvocationStatus,
    RegistryRecord,
    StopReason,
    utc_now,
)
from caobunga.store import Store


def _scripted(codes: dict[str, int], lines: dict[str, list[str]] | None = None):
    def spawn(slug: str, on_line: Callable[[str], None]) -> int:
        for line in (lines or {}).get(slug, []):
            on_line(line)
        return codes[slug]

    return spawn


def test_legs_follow_slug_order_and_prefix_each_line() -> None:
    seen: list[str] = []
    legs = run_fleet(
        ["a", "b"],
        2,
        echo=seen.append,
        spawn=_scripted({"a": 0, "b": 3}, {"a": ["one", "two"], "b": ["three"]}),
    )
    assert [(leg.slug, leg.exit_code) for leg in legs] == [("a", 0), ("b", 3)]
    assert sorted(seen) == ["[a] one", "[a] two", "[b] three"]


def test_pool_never_exceeds_jobs() -> None:
    alive = 0
    peak = 0
    lock = threading.Lock()

    def spawn(slug: str, on_line: Callable[[str], None]) -> int:  # noqa: ARG001
        nonlocal alive, peak
        with lock:
            alive += 1
            peak = max(peak, alive)
        time.sleep(0.02)
        with lock:
            alive -= 1
        return 0

    legs = run_fleet([f"p{i}" for i in range(7)], 3, echo=lambda _: None, spawn=spawn)
    assert len(legs) == 7
    assert peak == 3


def test_one_failure_never_halts_the_others() -> None:
    legs = run_fleet(
        ["a", "b", "c"],
        1,
        echo=lambda _: None,
        spawn=_scripted({"a": 4, "b": 1, "c": 0}),
    )
    assert [leg.exit_code for leg in legs] == [4, 1, 0]


def test_a_raising_leg_still_gets_a_receipt_row() -> None:
    def spawn(slug: str, on_line: Callable[[str], None]) -> int:  # noqa: ARG001
        if slug == "a":
            raise RuntimeError("boom")
        return 0

    seen: list[str] = []
    legs = run_fleet(["a", "b"], 2, echo=seen.append, spawn=spawn)
    assert [(leg.slug, leg.exit_code, leg.meaning) for leg in legs] == [
        ("a", 1, "unexpected"),
        ("b", 0, "ok"),
    ]
    assert seen == ["[a] fleet: RuntimeError('boom')"]


def test_interrupt_cancels_queued_slugs() -> None:
    """Ctrl-C must never spawn the slugs still waiting for a pool slot."""
    spawned: list[str] = []

    def spawn(slug: str, on_line: Callable[[str], None]) -> int:  # noqa: ARG001
        spawned.append(slug)
        if slug == "a":
            raise KeyboardInterrupt
        time.sleep(0.2)
        return 0

    with pytest.raises(KeyboardInterrupt):
        run_fleet(["a", "b", "c", "d"], 1, echo=lambda _: None, spawn=spawn)
    assert "c" not in spawned
    assert "d" not in spawned


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([0, 2, 3], 0),
        ([], 0),
        ([0, 4], 4),
        ([3, 1], 4),
        ([0, -9], 4),
        ([0, 2, 5], 4),
    ],
)
def test_fleet_exit_treats_refused_and_idle_as_skips(codes: list[int], expected: int) -> None:
    legs = [FleetLeg(slug=f"p{i}", exit_code=code, seconds=0.0) for i, code in enumerate(codes)]
    assert fleet_exit(legs) == expected


def test_unlaunchable_child_is_named_not_skipped() -> None:
    """CBN-EPIC-313.5: no provider or no `git` is the machine's state, not a skip."""
    leg = FleetLeg(slug="app", exit_code=5, seconds=0.0)
    assert leg.meaning == "unlaunchable"
    assert fleet_exit([leg]) == 4


def _receipt_line(
    store: Store,
    status: InvocationStatus,
    stop_reason: str,
    parked: list[InvocationPark] | None = None,
) -> str:
    """Write a terminal receipt and return the line `run` echoes for it."""
    successful = status in {
        InvocationStatus.COMPLETE,
        InvocationStatus.EMPTY,
        InvocationStatus.OBSERVED,
    }
    now = utc_now()
    result = InvocationResult(
        invocation_id="20261017T164756864Z",
        receipt_id=f"20261017T164756864Z-{uuid4().hex}",
        project="app",
        started_at=now,
        finished_at=now,
        status=status,
        stop_reason=StopReason(stop_reason),
        successful=successful,
        approved_scope_finished=successful,
        legacy_exit_code=0,
        remaining_tasks=[] if successful else ["APP-1"],
        parked=parked or [],
    )
    return f"{RECEIPT_PREFIX}{store.write_invocation(result)}"


def test_a_leg_that_parked_names_the_resume_and_what_ran_next(caobunga_home: Path) -> None:
    """CBN-443: the stopped-short cell carries each park's handoff line."""
    parks = [
        InvocationPark(task_id="APP-1", park_reason="drift", continued_with=["APP-2"]),
        InvocationPark(task_id="APP-3", park_reason="visual-confirm", held="2 uncommitted"),
    ]
    line = _receipt_line(Store(caobunga_home), InvocationStatus.INCOMPLETE, "outcome", parks)
    (leg,) = run_fleet(
        ["app"], 1, echo=lambda _: None, spawn=_scripted({"app": 0}, {"app": [line]})
    )
    assert leg.meaning == (
        "incomplete (outcome)"
        "; parked APP-1 (drift) → resume /ft-task APP-1; continued with APP-2"
        "; parked APP-3 (visual-confirm) → resume /ft-task APP-3; repo held: 2 uncommitted"
    )
    assert fleet_exit([leg]) == 4


def test_a_parked_legs_cell_shows_the_park_question_verbatim(caobunga_home: Path) -> None:
    """CBN-448: the prose (flaitron's question) follows the code, never clipped."""
    question = "Keep the v1 reader or move to v2?"
    parks = [
        InvocationPark(task_id="APP-1", park_reason="input-needed", park_prose=question),
    ]
    line = _receipt_line(Store(caobunga_home), InvocationStatus.INCOMPLETE, "outcome", parks)
    (leg,) = run_fleet(
        ["app"], 1, echo=lambda _: None, spawn=_scripted({"app": 0}, {"app": [line]})
    )
    assert f"parked APP-1 (input-needed — {question}) → resume" in leg.meaning


def test_a_multi_line_park_prose_stays_one_handoff_line() -> None:
    """Review fix: a block-scalar `park-reason: |` keeps newlines; the handoff folds them."""
    park = InvocationPark(task_id="APP-1", park_reason="drift", park_prose="found X.\nKeep v1?\n")
    assert park.handoff() == (
        "parked APP-1 (drift — found X. Keep v1?) → resume /ft-task APP-1; "
        "continued; nothing else dispatched"
    )


@pytest.mark.parametrize(
    ("code", "receipts", "meaning", "fleet_code"),
    [
        (0, [(InvocationStatus.INCOMPLETE, "outcome")], "incomplete (outcome)", 4),
        (0, [(InvocationStatus.INCOMPLETE, "suite")], "incomplete (suite)", 4),
        (0, [(InvocationStatus.COMPLETE, "complete")], "ok", 0),
        (0, [], "ok", 0),
        (
            0,
            [(InvocationStatus.INCOMPLETE, "outcome"), (InvocationStatus.COMPLETE, "gated")],
            "ok",
            0,
        ),
        (2, [(InvocationStatus.REFUSED, "dirty-tree")], "refused", 0),
        (3, [(InvocationStatus.INCOMPLETE, "denied")], "nothing eligible", 0),
    ],
)
def test_an_exit_zero_leg_answers_to_its_receipt(
    caobunga_home: Path,
    code: int,
    receipts: list[tuple[InvocationStatus, str]],
    meaning: str,
    fleet_code: int,
) -> None:
    """CBN-372: exit 0 over a park or a red suite is not `ok`; 2 and 3 stay skips."""
    store = Store(caobunga_home)
    lines = [_receipt_line(store, status, reason) for status, reason in receipts]
    (leg,) = run_fleet(
        ["app"], 1, echo=lambda _: None, spawn=_scripted({"app": code}, {"app": lines})
    )
    assert leg.meaning == meaning
    assert fleet_exit([leg]) == fleet_code


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("20261017T164756864Z-0a.yaml", None),
        ("20261017T164756864Z-0b.yaml", "status: [unterminated\n"),
        ("not/an id.yaml", None),
    ],
)
def test_an_unreadable_receipt_is_not_success(
    caobunga_home: Path, name: str, body: str | None
) -> None:
    """The receipt is read from this home by id; one that will not load fails the leg."""
    path = caobunga_home / "invocations" / name
    if body is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    line = f"{RECEIPT_PREFIX}{path}"
    (leg,) = run_fleet(
        ["app"], 1, echo=lambda _: None, spawn=_scripted({"app": 0}, {"app": [line]})
    )
    assert leg.meaning == "receipt unreadable"
    assert fleet_exit([leg]) == 4


def test_a_broken_echo_still_reads_the_receipt(caobunga_home: Path) -> None:
    """A failing sink stops the echo, never the receipt capture (CBN-372 review)."""
    line = _receipt_line(Store(caobunga_home), InvocationStatus.INCOMPLETE, "outcome")

    def broken(_line: str) -> None:
        raise BrokenPipeError

    (leg,) = run_fleet(
        ["app"], 1, echo=broken, spawn=_scripted({"app": 0}, {"app": ["first", line]})
    )
    assert leg.meaning == "incomplete (outcome)"


def test_a_leg_whose_receipt_is_unwritten_says_why() -> None:
    """CBN-442: a full disk at the receipt write is named, not `unexpected`.

    The fleet policy is unchanged: the next slug still runs.
    """
    lines = [f"{RECEIPT_UNWRITTEN_PREFIX}No space left on device", "recovery: free the space"]
    legs = run_fleet(
        ["app", "next"],
        1,
        echo=lambda _: None,
        spawn=_scripted({"app": 4, "next": 0}, {"app": lines}),
    )
    assert [leg.meaning for leg in legs] == ["receipt unwritten (No space left on device)", "ok"]
    assert fleet_exit(legs) == 4


def test_an_unwritten_marker_on_a_clean_exit_is_only_output(caobunga_home: Path) -> None:
    """CBN-442 review: only `ReceiptUnwritten`'s exit 4 makes the marker a meaning."""
    lines = [
        f"{RECEIPT_UNWRITTEN_PREFIX}quoted in a summary",
        _receipt_line(Store(caobunga_home), InvocationStatus.COMPLETE, "complete"),
    ]
    (leg,) = run_fleet(["app"], 1, echo=lambda _: None, spawn=_scripted({"app": 0}, {"app": lines}))
    assert leg.meaning == "ok"


def test_spawn_run_drives_the_real_cli(caobunga_home: Path) -> None:
    """A real child `caobunga run` — unknown slug, so it refuses before any worker."""
    seen: list[str] = []
    code = spawn_run("no-such-project", seen.append)
    assert code == 2
    assert any("no-such-project" in line for line in seen), seen
    # The prefix `fleet` matches is the one a real `run` echoes (CBN-372).
    (receipt,) = [line for line in seen if line.startswith(RECEIPT_PREFIX)]
    stem = Path(receipt.removeprefix(RECEIPT_PREFIX)).stem
    assert Store(caobunga_home).load_invocation(stem).legacy_exit_code == 2


def test_spawn_run_keeps_draining_when_the_sink_breaks(caobunga_home: Path) -> None:  # noqa: ARG001
    """A closed pipe on our side must not orphan the child mid-drain."""
    calls = 0

    def broken(line: str) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise BrokenPipeError

    assert spawn_run("no-such-project", broken) == 2
    assert calls == 1


def test_spawn_run_reports_a_child_that_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        raise FileNotFoundError("no python")

    monkeypatch.setattr(fleet_module.subprocess, "Popen", refuse)
    seen: list[str] = []
    assert spawn_run("a", seen.append) == 1
    assert seen == ["fleet: could not start run: no python"]


@pytest.mark.parametrize("error", [OSError("read failed"), KeyboardInterrupt()])
def test_spawn_run_reaps_the_child_when_reading_its_output_raises(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """CBN-EPIC-314.4: a raising read must not leave the child alive or unreaped."""
    spawned: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    class _BrokenRead:
        def __init__(self, pipe: object) -> None:
            self._pipe = pipe

        def __iter__(self) -> "_BrokenRead":
            return self

        def __next__(self) -> str:
            raise error

        def close(self) -> None:
            self._pipe.close()  # type: ignore[attr-defined]

    def popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:  # noqa: ARG001
        # A child that would run forever, never writing: only a kill ends it.
        proc = real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
        )
        spawned.append(proc)
        proc.stdout = _BrokenRead(proc.stdout)  # type: ignore[assignment]
        return proc

    monkeypatch.setattr(fleet_module.subprocess, "Popen", popen)
    try:
        with pytest.raises(type(error)):
            spawn_run("a", lambda _line: None)
        (proc,) = spawned
        assert proc.returncode is not None  # waited for, not merely signalled
        assert proc.returncode < 0  # killed by a signal, not a clean exit
    finally:
        for leaked in spawned:
            if leaked.poll() is None:
                leaked.kill()
                leaked.wait()


# A child that counts the SIGTERMs it gets, then reports once they stop coming.
_COUNTING_CHILD = """
import signal, time
terms = 0
def on_term(*_):
    global terms
    terms += 1
signal.signal(signal.SIGTERM, on_term)
print("ready", flush=True)
deadline = time.monotonic() + 30
while not terms and time.monotonic() < deadline:
    time.sleep(0.01)
time.sleep(0.3)
print(f"terms {terms}", flush=True)
"""


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGTERM])
def test_hangup_forwards_once_to_running_children_and_waits(
    monkeypatch: pytest.MonkeyPatch, signum: signal.Signals
) -> None:
    """CBN-430: the fleet stops its children once, waits, then lets the interrupt out."""
    real_popen = subprocess.Popen
    spawned: list[subprocess.Popen[str]] = []

    def popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:  # noqa: ARG001
        proc = real_popen(
            [sys.executable, "-c", _COUNTING_CHILD],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
        )
        spawned.append(proc)
        return proc

    def hang_up(slug: str, on_line: Callable[[str], None]) -> int:  # noqa: ARG001
        def seen(line: str) -> None:
            lines.append(line)
            if line == "ready":
                os.kill(os.getpid(), signum)

        return spawn_run(slug, seen)

    lines: list[str] = []
    monkeypatch.setattr(fleet_module.subprocess, "Popen", popen)
    before = signal.getsignal(signum)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_fleet(["a", "b"], 1, echo=lambda _: None, spawn=hang_up)
        (proc,) = spawned  # "b" was queued, never spawned
        assert proc.returncode == 0  # waited for, and it exited on its own terms
        assert lines == ["ready", "terms 1"]
        assert signal.getsignal(signum) is before
        assert not fleet_module._children.procs
        assert not fleet_module._children.stopping
    finally:
        for leaked in spawned:
            if leaked.poll() is None:
                leaked.kill()
                leaked.wait()


def test_a_child_started_after_the_hangup_is_stopped_at_once() -> None:
    children = fleet_module._Children()
    children.stop()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], text=True)
    try:
        children.add(proc)
        assert proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_repeat_hangup_is_absorbed_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    stops: list[None] = []
    monkeypatch.setattr(fleet_module._children, "stop", lambda: stops.append(None))
    with fleet_module._hangup_guard():
        fleet_module._children.stopping = True  # the first signal's work
        os.kill(os.getpid(), signal.SIGTERM)
    assert stops == []
    assert not fleet_module._children.stopping


def test_hangup_guard_leaves_handlers_alone_off_the_main_thread() -> None:
    before = signal.getsignal(signal.SIGTERM)
    seen: list[object] = []

    def enter() -> None:
        with fleet_module._hangup_guard():
            seen.append(signal.getsignal(signal.SIGTERM))

    thread = threading.Thread(target=enter)
    thread.start()
    thread.join()
    assert seen == [before]


def test_hangup_guard_keeps_an_inherited_sig_ign() -> None:
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        with fleet_module._hangup_guard():
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGTERM, previous)


def _register(store: Store, slug: str, path: Path, *, enabled: bool = True) -> None:
    path.mkdir()
    store.register(
        RegistryRecord(
            id=slug,
            path=str(path),
            tier=ExecutionTier.GATED,
            registered_at=utc_now(),
            enabled=enabled,
        )
    )


def test_cli_fleet_skips_disabled_prints_receipt_and_folds_exit(
    caobunga_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(caobunga_home)
    _register(store, "alpha", tmp_path / "alpha")
    _register(store, "beta", tmp_path / "beta")
    _register(store, "off", tmp_path / "off", enabled=False)
    spawned: list[str] = []
    real_run_fleet = run_fleet

    def spawn(slug: str, on_line: Callable[[str], None]) -> int:
        spawned.append(slug)
        on_line("hello")
        return {"alpha": 3, "beta": 4}[slug]

    monkeypatch.setattr(
        cli_module,
        "run_fleet",
        lambda slugs, jobs, *, echo: real_run_fleet(slugs, jobs, echo=echo, spawn=spawn),
    )
    result = CliRunner().invoke(cli, ["fleet", "--jobs", "2"])
    assert result.exit_code == 4, result.output
    assert sorted(spawned) == ["alpha", "beta"]
    assert "fleet: 2 projects, jobs 2" in result.output
    assert "[alpha] hello" in result.output
    assert "nothing eligible" in result.output
    assert "failed" in result.output
    assert "fleet: 1 nothing eligible · 1 failed" in result.output


def test_cli_fleet_all_skips_exit_zero(
    caobunga_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(caobunga_home)
    _register(store, "alpha", tmp_path / "alpha")
    real_run_fleet = run_fleet
    monkeypatch.setattr(
        cli_module,
        "run_fleet",
        lambda slugs, jobs, *, echo: real_run_fleet(
            slugs, jobs, echo=echo, spawn=_scripted({"alpha": 2})
        ),
    )
    result = CliRunner().invoke(cli, ["fleet"])
    assert result.exit_code == 0, result.output
    assert "jobs 4" in result.output
    assert "1 refused" in result.output


def test_cli_fleet_names_a_parked_leg_and_fails(
    caobunga_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-372: the 2026-10-17 shape — a drain that parked exits 0 under `fleet`."""
    store = Store(caobunga_home)
    _register(store, "alpha", tmp_path / "alpha")
    line = _receipt_line(store, InvocationStatus.INCOMPLETE, "outcome")
    real_run_fleet = run_fleet
    monkeypatch.setattr(
        cli_module,
        "run_fleet",
        lambda slugs, jobs, *, echo: real_run_fleet(
            slugs, jobs, echo=echo, spawn=_scripted({"alpha": 0}, {"alpha": [line]})
        ),
    )
    result = CliRunner().invoke(cli, ["fleet"])
    assert result.exit_code == 4, result.output
    assert f"[alpha] {line}" in result.output
    assert "fleet: 1 incomplete (outcome)" in result.output


def test_cli_fleet_with_no_enabled_projects(caobunga_home: Path) -> None:  # noqa: ARG001
    result = CliRunner().invoke(cli, ["fleet"])
    assert result.exit_code == 0, result.output
    assert "fleet: no enabled projects" in result.output


def test_cli_fleet_rejects_zero_jobs(caobunga_home: Path) -> None:  # noqa: ARG001
    result = CliRunner().invoke(cli, ["fleet", "--jobs", "0"])
    assert result.exit_code == 2


def test_two_ids_at_one_repo_never_run_side_by_side(caobunga_home: Path, tmp_path: Path) -> None:
    """Parallel legs for two ids at one repo: one runs, the other is refused (CBN-EPIC-313.2).

    Each leg drives the real `execute_run` in-process. Observe holds the run lock
    while it echoes, so the first leg parks inside that echo until the second leg
    has been refused — the overlap the slug-keyed lock let through.
    """
    store = Store(caobunga_home)
    repo = tmp_path / "repo"
    (repo / ".flaitron").mkdir(parents=True)
    (repo / ".flaitron/PLAN.md").write_text("# sample\n", encoding="utf-8")
    for slug in ("app", "twin"):
        store.register(
            RegistryRecord(
                id=slug, path=str(repo), tier=ExecutionTier.OBSERVE, registered_at=utc_now()
            )
        )
    refused = threading.Event()
    held: list[bool] = []

    def hold(_line: str) -> None:
        # Park on the first echo only, so a regression fails in one timeout, not five.
        if not held:
            held.append(True)
            refused.wait(timeout=5)

    def spawn(slug: str, on_line: Callable[[str], None]) -> int:
        try:
            execute_run(slug, store=store, echo=hold)
        except UserError as exc:
            on_line(str(exc))
            refused.set()
            return exc.exit_code
        return 0

    seen: list[str] = []
    legs = run_fleet(["app", "twin"], 2, echo=seen.append, spawn=spawn)
    assert sorted(leg.exit_code for leg in legs) == [0, 2]
    loser = next(leg.slug for leg in legs if leg.exit_code == 2)
    assert any(f"already active for {loser}'s repo" in line for line in seen)
