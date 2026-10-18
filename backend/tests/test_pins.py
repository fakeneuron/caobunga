"""Stale-pin catalog check (CBN-166). Injected PATH/runner; no live catalog calls."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

import pytest

import caobunga.pins as pins_module
from caobunga.pins import (
    V1_PROVIDERS,
    PinReport,
    check,
    check_all,
    parse_catalog,
    parse_cursor_catalog,
    parse_grok_catalog,
)
from caobunga.taskmap import MODELS
from caobunga.usage import WorkerProvider


def _which_of(*names: str):
    present = set(names)

    def which(name: str) -> str | None:
        return f"/bin/{name}" if name in present else None

    return which


def _run_json(payload: object):
    def run(_argv: Sequence[str]) -> str:
        return json.dumps(payload)

    return run


def test_check_all_order_and_missing_binaries() -> None:
    rows = check_all(which=lambda _name: None)
    assert [row.provider for row in rows] == [p.value for p in V1_PROVIDERS]
    assert len(rows) == 5
    assert all(row.installed is False for row in rows)
    assert all(row.stale == () for row in rows)
    assert all(row.partial is False for row in rows)
    assert all(row.message is None for row in rows)


def test_installed_provider_without_catalog_command_is_unavailable() -> None:
    report = check(WorkerProvider.CLAUDE, which=_which_of("claude"))
    assert report.installed is True
    assert report.message == "catalog unavailable"
    assert report.stale == ()


def test_uninstalled_provider_is_not_installed() -> None:
    report = check(WorkerProvider.CODEX, which=lambda _name: None)
    assert report.installed is False
    assert report.message is None


def test_codex_catalog_is_always_partial_regardless_of_stale_pins() -> None:
    """CBN-165: the codex catalog hides TAP-gated ids, so absence proves nothing."""
    report = check(
        WorkerProvider.CODEX,
        which=_which_of("codex"),
        run=_run_json([{"slug": "gpt-5.5"}]),
    )
    assert report.installed is True
    assert report.partial is True
    assert report.stale == ()
    assert report.message is None


def test_codex_pinned_id_present_in_catalog_still_reports_partial() -> None:
    pinned = next(iter(MODELS[WorkerProvider.CODEX].values()))
    report = check(
        WorkerProvider.CODEX,
        which=_which_of("codex"),
        run=_run_json([{"slug": pinned}]),
    )
    assert report.partial is True
    assert report.stale == ()


# Live output shapes, trimmed, captured 2026-09-24 (CBN-256).
GROK_MODELS = """You are logged in with grok.com.

Default model: grok-4.7

Available models:
  * grok-4.7 (default)
  - grok-4.7-build-fast
  - grok-4.6
  - grok-4.5
"""

CURSOR_MODELS = """Available models

auto - Auto (current, default)
composer-2.5 - Composer 2.5
grok-4.7-high - Grok 4.7  High
grok-4.7-xhigh - Grok 4.7  Extra High
cursor-grok-4.6-medium - Grok 4.6 Medium

Tip: use --model <id> (or /model <id> in interactive mode) to switch.
"""


def test_parse_grok_catalog_reads_bulleted_ids() -> None:
    assert parse_grok_catalog(GROK_MODELS) == {
        "grok-4.7",
        "grok-4.7-build-fast",
        "grok-4.6",
        "grok-4.5",
    }


def test_parse_cursor_catalog_reads_id_lines_only() -> None:
    assert parse_cursor_catalog(CURSOR_MODELS) == {
        "auto",
        "composer-2.5",
        "grok-4.7-high",
        "grok-4.7-xhigh",
        "cursor-grok-4.6-medium",
    }


@pytest.mark.parametrize(
    ("provider", "binary", "stdout"),
    [
        (WorkerProvider.GROK, "grok", GROK_MODELS),
        (WorkerProvider.CURSOR, "cursor-agent", CURSOR_MODELS),
    ],
)
def test_current_pins_are_fresh_against_live_shaped_catalog(
    provider: WorkerProvider, binary: str, stdout: str
) -> None:
    report = check(provider, which=_which_of(binary), run=lambda _argv: stdout)
    assert report.installed is True
    assert report.partial is False
    assert report.message is None
    assert report.stale == ()


def test_complete_catalog_flags_a_pin_missing_from_it() -> None:
    """A full catalog's silence on a pinned id is reportable (grok is not partial)."""
    report = check(
        WorkerProvider.GROK,
        which=_which_of("grok"),
        run=lambda _argv: "Available models:\n  * some-other-model (default)\n",
    )
    assert report.partial is False
    assert report.stale == tuple(sorted({*MODELS[WorkerProvider.GROK].values()}))


@pytest.mark.parametrize(
    ("provider", "binary"),
    [(WorkerProvider.GROK, "grok"), (WorkerProvider.CURSOR, "cursor-agent")],
)
def test_empty_full_catalog_is_unreadable_not_all_stale(
    provider: WorkerProvider, binary: str
) -> None:
    """A logged-out CLI or a changed shape parses empty — never a wall of stale pins."""
    report = check(provider, which=_which_of(binary), run=lambda _argv: "Please log in.\n")
    assert report.message == "catalog unreadable"
    assert report.stale == ()


def test_uninjected_run_uses_catalog_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catalogs get `CATALOG_TIMEOUT`, not the 5s usage budget cursor overruns."""
    seen: list[float] = []

    def fake_run_argv(_argv: Sequence[str], timeout: float) -> str:
        seen.append(timeout)
        return GROK_MODELS

    monkeypatch.setattr(pins_module, "run_argv", fake_run_argv)
    check(WorkerProvider.GROK, which=_which_of("grok"))
    assert seen == [pins_module.CATALOG_TIMEOUT]


def test_parse_catalog_accepts_bare_list_and_wrapped_forms() -> None:
    assert parse_catalog('[{"slug": "a"}, {"slug": "b"}]') == {"a", "b"}
    assert parse_catalog('{"models": [{"slug": "a"}]}') == {"a"}
    assert parse_catalog('{"data": ["a", "b"]}') == {"a", "b"}
    assert parse_catalog('{"nothing": []}') == set()
    assert parse_catalog("[]") == set()


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
def test_check_degrades_to_unreadable_on_failure(exc: Exception) -> None:
    def run(_argv: Sequence[str]) -> str:
        raise exc

    report = check(WorkerProvider.CODEX, which=_which_of("codex"), run=run)
    assert report.installed is True
    assert report.message == "catalog unreadable"
    assert report.stale == ()
    assert report.partial is False


def test_garbage_stdout_is_unreadable() -> None:
    report = check(WorkerProvider.CODEX, which=_which_of("codex"), run=lambda _argv: "not-json")
    assert report.message == "catalog unreadable"


def test_pin_report_is_a_model() -> None:
    report = PinReport(provider="x")
    assert report.installed is False
