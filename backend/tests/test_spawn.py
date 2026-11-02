"""Detached spawn helpers (CBN-174, CBN-EPIC-314.2, CBN-349). Real processes, no `just`."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from caobunga.spawn import reap, run_detached


def test_run_detached_detaches_stdin_and_kills_the_whole_tree_on_timeout() -> None:
    """The real runner, not the injected one: a child that reads stdin sees EOF
    at once (the caller's stdin is never inherited), and a timeout reaps the child's
    children too — both measured the hard way at CBN-174."""
    code, output = run_detached(["sh", "-c", "read x; echo got:$x"], Path.cwd(), 10.0)
    assert (code, output.strip()) == (0, "got:")
    marker = f"cbn174-{os.getpid()}"
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_detached(
            ["sh", "-c", f"echo started; sleep 30 & sleep 30; : {marker}"], Path.cwd(), 1.0
        )
    assert "started" in (info.value.output or "")
    alive = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert alive.stdout.strip() == "", f"process group survived the timeout: {alive.stdout}"


def test_run_detached_bounds_the_drain_on_an_orphaned_pipe_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grandchild that left the group survives the `killpg` holding stdout open;
    the post-kill drain gives up after `KILL_GRACE` instead of waiting on that EOF
    forever, and keeps what the suite printed (CBN-EPIC-314.2)."""
    monkeypatch.setattr("caobunga.spawn.KILL_GRACE", 0.5)
    marker = f"cbn314-orphan-{os.getpid()}"
    escapee = f"import time; time.sleep(30)  # {marker}"
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {escapee!r}], start_new_session=True); "
        "print('started', flush=True); time.sleep(30)"
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as info:
            run_detached([sys.executable, "-c", script], Path.cwd(), 1.0)
        assert time.monotonic() - started < 10.0
        assert "started" in (info.value.output or "")
    finally:
        subprocess.run(["pkill", "-f", marker], check=False)  # noqa: S603, S607


def test_run_detached_kills_the_group_on_an_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any exception mid-run, not only the timeout, reaps the whole tree before it
    propagates — a Ctrl-C left `just`'s suite running (CBN-EPIC-314.2)."""
    marker = f"cbn314-interrupt-{os.getpid()}"
    original = subprocess.Popen.communicate
    calls: list[float | None] = []

    def interrupted(
        self: subprocess.Popen[str], input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        calls.append(timeout)
        if len(calls) == 1:
            with contextlib.suppress(subprocess.TimeoutExpired):
                original(self, timeout=1.0)
            raise KeyboardInterrupt
        return original(self, input, timeout)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    # The backgrounded grandchild carries the marker in its own argv, so a
    # leader-only kill would leave it for `pgrep` to find.
    grandchild = f"'{sys.executable}' -c 'import time; time.sleep(30)' {marker}"
    with pytest.raises(KeyboardInterrupt):
        run_detached(["sh", "-c", f"{grandchild} & sleep 30"], Path.cwd(), 60.0)
    alive = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert alive.stdout.strip() == "", f"process group survived the interrupt: {alive.stdout}"


def test_reap_treats_killpg_eperm_as_group_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS answers `killpg` with `EPERM` for a group whose members have all
    exited unreaped; `reap` must still drain and return the output (CBN-349)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('done')"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    def eperm_killpg(pgid: int, sig: int) -> None:  # noqa: ARG001
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("caobunga.spawn.os.killpg", eperm_killpg)
    assert reap(proc) == "done\n"
    assert proc.returncode == 0


def test_run_detached_replaces_non_utf8_output() -> None:
    """A suite printing invalid UTF-8 is still a result (CBN-EPIC-314.2)."""
    code, output = run_detached(["sh", "-c", r"printf '\377ok\n'"], Path.cwd(), 10.0)
    assert code == 0
    assert output == "�ok\n"
