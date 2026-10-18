"""Worker Protocol conformance and CAOBUNGA_WORKER adapter selection."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import get_protocol_members

import pytest

from caobunga.errors import UserError
from caobunga.local import LocalWorker
from caobunga.worker import (
    DEFAULT_WORKER,
    ENV_WORKER,
    WORKER_LOCAL,
    Session,
    Worker,
    resolve_cwd,
    select_worker,
    worker_name,
)


def test_shipped_adapter_satisfies_the_protocol() -> None:
    assert issubclass(LocalWorker, Worker)


def test_protocol_covers_only_what_the_loop_calls() -> None:
    declared = set(get_protocol_members(Worker))
    assert declared == {
        "launch",
        "send",
        "status",
        "exit_code",
        "stderr_tail",
        "stdout_tail",
        "shutdown",
        "close",
    }


def test_worker_name_defaults_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_WORKER, raising=False)
    assert worker_name() == DEFAULT_WORKER == WORKER_LOCAL


@pytest.mark.parametrize("raw", ["local", "  local  ", "LOCAL"])
def test_worker_name_parses_local(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ENV_WORKER, raw)
    assert worker_name() == WORKER_LOCAL


@pytest.mark.parametrize("raw", ["", "   "])
def test_worker_name_treats_blank_as_unset(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ENV_WORKER, raw)
    assert worker_name() == WORKER_LOCAL


@pytest.mark.parametrize("raw", ["kubernetes", "cao"])
def test_worker_name_rejects_unknown(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """`cao` is an unknown value since CBN-204 retired that adapter."""
    monkeypatch.setenv(ENV_WORKER, raw)
    with pytest.raises(UserError) as excinfo:
        worker_name()
    assert excinfo.value.exit_code == 2


def test_select_worker_builds_the_named_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_WORKER, WORKER_LOCAL)
    # `closing`, not `with … as`: the Protocol declares `close`, not the
    # context-manager pair (same idiom as `cli.py`'s production call site).
    with closing(select_worker()) as worker:
        assert isinstance(worker, LocalWorker)


def test_session_carries_the_adapter_handle() -> None:
    session = Session(session_name="app-CBN-001", terminal_id="pid-9", provider="grok_cli")
    assert session.terminal_id == "pid-9"


def test_resolve_cwd_rejects_missing_and_non_directory(tmp_path: Path) -> None:
    with pytest.raises(UserError):
        resolve_cwd(tmp_path / "nope")
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(UserError):
        resolve_cwd(file_path)
    assert resolve_cwd(tmp_path) == tmp_path.resolve()
