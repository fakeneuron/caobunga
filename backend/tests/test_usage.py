"""Leftover-usage adapters (CBN-020). Injected PATH/runner; no live quota calls."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence

import pytest

from caobunga.usage import (
    V1_PROVIDERS,
    Leftover,
    WorkerProvider,
    parse_leftover,
    probe,
    probe_all,
)


def _which_of(*names: str):
    present = set(names)

    def which(name: str) -> str | None:
        return f"/bin/{name}" if name in present else None

    return which


def _run_json(payload: object):
    def run(_argv: Sequence[str]) -> str:
        return json.dumps(payload)

    return run


def test_probe_all_order_and_missing_binaries() -> None:
    rows = probe_all(which=lambda _name: None)
    assert [row.provider for row in rows] == [p.value for p in V1_PROVIDERS]
    assert len(rows) == 5
    assert all(row.leftover is Leftover.UNKNOWN for row in rows)
    assert all(row.installed is False for row in rows)
    assert all(row.remaining is None for row in rows)


def test_installed_without_dump_is_unknown_not_available() -> None:
    usage = probe(WorkerProvider.GROK, which=_which_of("grok"))
    assert usage.installed is True
    assert usage.leftover is Leftover.UNKNOWN
    assert usage.remaining is None


def test_cursor_uses_cursor_agent_not_ide_or_grok_agent() -> None:
    seen: list[str] = []

    def which(name: str) -> str | None:
        seen.append(name)
        return None

    probe(WorkerProvider.CURSOR, which=which)
    assert seen == ["cursor-agent"]


def test_parse_remaining_fraction_and_percent() -> None:
    leftover, remaining = parse_leftover('{"remaining": 0.25}')
    assert leftover is Leftover.AVAILABLE
    assert remaining == pytest.approx(0.25)

    leftover, remaining = parse_leftover('{"remaining": 40}')
    assert leftover is Leftover.AVAILABLE
    assert remaining == pytest.approx(0.4)

    leftover, remaining = parse_leftover('{"remaining_percent": 10}')
    assert leftover is Leftover.AVAILABLE
    assert remaining == pytest.approx(0.1)

    leftover, remaining = parse_leftover('{"used_percent": 100}')
    assert leftover is Leftover.EXHAUSTED
    assert remaining == pytest.approx(0.0)


def test_explicit_leftover_token_wins() -> None:
    leftover, remaining = parse_leftover('{"leftover": "exhausted", "remaining": 0.9}')
    assert leftover is Leftover.EXHAUSTED
    assert remaining == pytest.approx(0.9)


def test_probe_injected_dump_available() -> None:
    usage = probe(
        WorkerProvider.CODEX,
        which=_which_of("codex"),
        dump_argv=("codex", "usage"),
        run=_run_json({"remaining": 0.5}),
    )
    assert usage.installed is True
    assert usage.leftover is Leftover.AVAILABLE
    assert usage.remaining == pytest.approx(0.5)


def test_probe_injected_dump_exhausted() -> None:
    usage = probe(
        WorkerProvider.CLAUDE,
        which=_which_of("claude"),
        dump_argv=("claude", "usage"),
        run=_run_json({"leftover": "exhausted", "remaining": 0}),
    )
    assert usage.leftover is Leftover.EXHAUSTED
    assert usage.remaining == pytest.approx(0.0)


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("slow"),
        subprocess.TimeoutExpired(cmd="x", timeout=1),
        OSError("gone"),
        RuntimeError("exit 1"),
        json.JSONDecodeError("bad", "doc", 0),
    ],
)
def test_probe_degrades_unknown_on_failure(exc: Exception) -> None:
    def run(_argv: Sequence[str]) -> str:
        raise exc

    usage = probe(
        WorkerProvider.GROK,
        which=_which_of("grok"),
        dump_argv=("grok", "usage"),
        run=run,
    )
    assert usage.installed is True
    assert usage.leftover is Leftover.UNKNOWN
    assert usage.remaining is None


def test_garbage_stdout_is_unknown() -> None:
    usage = probe(
        WorkerProvider.CODEX,
        which=_which_of("codex"),
        dump_argv=("codex", "usage"),
        run=lambda _argv: "not-json",
    )
    assert usage.leftover is Leftover.UNKNOWN


def test_unknown_provider_token_is_unknown() -> None:
    usage = probe("not-a-provider", which=_which_of("not-a-provider"))
    assert usage.provider == "not-a-provider"
    assert usage.installed is False
    assert usage.leftover is Leftover.UNKNOWN


def test_default_runner_reads_stdout() -> None:
    usage = probe(
        WorkerProvider.GROK,
        which=_which_of("grok"),
        dump_argv=(sys.executable, "-c", "print('{\"remaining\": 0.75}')"),
    )
    assert usage.leftover is Leftover.AVAILABLE
    assert usage.remaining == pytest.approx(0.75)


def test_default_runner_nonzero_is_unknown() -> None:
    usage = probe(
        WorkerProvider.GROK,
        which=_which_of("grok"),
        dump_argv=(sys.executable, "-c", "raise SystemExit(2)"),
    )
    assert usage.installed is True
    assert usage.leftover is Leftover.UNKNOWN
