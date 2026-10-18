"""`caobunga.hooks` — the PreToolUse guard (CBN-212, CBN-247)."""

from __future__ import annotations

import io
import json
import shlex
import subprocess
from pathlib import Path

import pytest

from caobunga.hooks import DENIAL, REGISTRY_DENIAL, guard, home
from caobunga.local import FOREGROUND_HOOK_COMMAND
from caobunga.store import ENV_HOME, default_home


def _run(payload: object) -> tuple[int, str]:
    err = io.StringIO()
    code = guard(io.StringIO(json.dumps(payload)), err)
    return code, err.getvalue()


def test_denies_a_backgrounded_bash_call_with_the_foreground_instruction() -> None:
    code, err = _run(
        {"tool_name": "Bash", "tool_input": {"command": "pytest", "run_in_background": True}}
    )
    assert code == 2
    assert err.strip() == DENIAL
    assert "foreground" in DENIAL and "ending your turn ends the session" in DENIAL


def test_allows_everything_that_is_not_a_backgrounded_bash_call() -> None:
    allowed = [
        {"tool_name": "Bash", "tool_input": {"command": "pytest"}},
        {"tool_name": "Bash", "tool_input": {"command": "pytest", "run_in_background": False}},
        {"tool_name": "Read", "tool_input": {"file_path": "x", "run_in_background": True}},
        {"tool_name": "Bash"},
        {"tool_name": "Bash", "tool_input": "not a dict"},
        [],
    ]
    for payload in allowed:
        assert _run(payload) == (0, ""), payload


def test_unreadable_input_never_blocks() -> None:
    """A bug here must not wedge a worker on every Bash call: exit 0, say nothing."""
    err = io.StringIO()
    assert guard(io.StringIO("{not json"), err) == 0
    assert guard(io.StringIO(""), err) == 0
    assert err.getvalue() == ""


def test_module_entry_is_what_the_launcher_registers() -> None:
    """`python -m caobunga.hooks` is the hook command (`local.FOREGROUND_HOOK_COMMAND`)."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"run_in_background": True}})
    proc = subprocess.run(  # noqa: S603
        shlex.split(FOREGROUND_HOOK_COMMAND),
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stderr.strip() == DENIAL


def _guard(payload: object) -> tuple[int, str]:
    err = io.StringIO()
    code = guard(io.StringIO(json.dumps(payload)), err)
    return code, err.getvalue()


def test_home_matches_the_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`hooks.home` restates `store.default_home` so it can stay off `store`'s imports."""
    monkeypatch.delenv(ENV_HOME, raising=False)
    assert home() == default_home()
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "h"))
    assert home() == default_home() == tmp_path / "h"


def test_home_matches_the_store_for_a_relative_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """CBN-251: a relative `$CAOBUNGA_HOME` must still resolve identically in both
    (both anchor to the calling process's cwd; production keeps them in sync by
    exporting an already-absolute value at spawn — see `local._default_spawn`)."""
    monkeypatch.setenv(ENV_HOME, "relative-home")
    assert home() == default_home() == Path.cwd() / "relative-home"


@pytest.mark.parametrize(
    "command",
    [
        "caobunga register ~/Code/x --tier proceed-on-green --replace",
        "uv run caobunga register . --tier gated",
        "cd /tmp && python -m caobunga register /r",
        "/Users/me/.local/bin/caobunga register /r",
        "caobunga --version; caobunga  register /r",
        "uv run --directory backend caobunga register /r",
        "CAOBUNGA_HOME=/x caobunga register /r",
        "echo $(caobunga register /r)",
    ],
)
def test_denies_caobunga_register_in_bash(command: str) -> None:
    code, err = _guard({"tool_name": "Bash", "tool_input": {"command": command}})
    assert (code, err.strip()) == (2, REGISTRY_DENIAL)


@pytest.mark.parametrize(
    "command",
    [
        "grep -n register backend/caobunga/cli.py",
        "caobunga status",
        "caobunga-mcp-suite register",
        "uv run pytest -q tests/test_cli.py -k register",
        "caobunga registered",
        'grep -rn "caobunga register" docs/',
        "git commit -m 'operator runs caobunga register to retier'",
        "cat <<'EOF'\nthe operator runs caobunga register to retier\nEOF",
    ],
)
def test_allows_bash_that_only_mentions_register(command: str) -> None:
    assert _guard({"tool_name": "Bash", "tool_input": {"command": command}}) == (0, "")


@pytest.mark.parametrize(
    ("tool", "key"),
    [
        ("Write", "file_path"),
        ("Edit", "file_path"),
        ("MultiEdit", "file_path"),
        ("NotebookEdit", "notebook_path"),
    ],
)
def test_denies_file_writes_under_the_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool: str, key: str
) -> None:
    store = tmp_path / "store"
    (store / "projects").mkdir(parents=True)
    monkeypatch.setenv(ENV_HOME, str(store))
    target = str(store / "projects" / "x.yaml")
    assert _guard({"tool_name": tool, "tool_input": {key: target}}) == (2, REGISTRY_DENIAL + "\n")


def test_home_check_resolves_relative_paths_and_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = tmp_path / "real"
    (real / "projects").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv(ENV_HOME, str(link))
    relative = {
        "tool_name": "Write",
        "tool_input": {"file_path": "projects/x.yaml"},
        "cwd": str(real),
    }
    assert _guard(relative)[0] == 2
    direct = {"tool_name": "Edit", "tool_input": {"file_path": str(real / "projects" / "x.yaml")}}
    assert _guard(direct)[0] == 2


def test_allows_file_writes_outside_the_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "store"))
    allowed = [
        {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "repo" / "a.py")}},
        {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "store-sibling")}},
        {"tool_name": "Edit", "tool_input": {}},
        {"tool_name": "Edit", "tool_input": {"file_path": 3}},
        {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "store" / "x")}},
    ]
    for payload in allowed:
        assert _guard(payload) == (0, ""), payload


def test_guard_keeps_the_foreground_denial_and_never_blocks_on_junk() -> None:
    code, err = _guard(
        {"tool_name": "Bash", "tool_input": {"command": "x", "run_in_background": True}}
    )
    assert (code, err.strip()) == (2, DENIAL)
    err_io = io.StringIO()
    assert guard(io.StringIO("{not json"), err_io) == 0
    assert guard(io.StringIO("[]"), err_io) == 0
    assert err_io.getvalue() == ""


def test_module_entry_runs_the_registry_guard() -> None:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "caobunga register ."}})
    proc = subprocess.run(  # noqa: S603
        shlex.split(FOREGROUND_HOOK_COMMAND),
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (proc.returncode, proc.stderr.strip()) == (2, REGISTRY_DENIAL)


def test_home_check_ignores_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """macOS's default volume is case-insensitive: `~/.CAOBUNGA` is the home."""
    store = tmp_path / "store"
    monkeypatch.setenv(ENV_HOME, str(store))
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "STORE" / "x")}}
    assert _guard(payload)[0] == 2
