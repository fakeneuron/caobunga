"""Provider+model picker (CBN-022). Injected usage; no live quota calls."""

from __future__ import annotations

import pytest

from caobunga.picker import PickReason, ProviderUnavailable, pick
from caobunga.taskmap import resolve
from caobunga.usage import Leftover, ProviderUsage, WorkerProvider


def _row(
    provider: str,
    *,
    installed: bool = True,
    leftover: Leftover = Leftover.UNKNOWN,
    remaining: float | None = None,
) -> ProviderUsage:
    return ProviderUsage(
        provider=provider,
        installed=installed,
        leftover=leftover,
        remaining=remaining,
    )


def test_unknown_leftover_picks_first_installed_v1() -> None:
    usage = [
        _row("grok_cli"),
        _row("claude_code"),
        _row("codex"),
        _row("cursor_cli"),
    ]
    chosen = pick("medium", usage=usage)
    assert chosen.provider == WorkerProvider.CODEX
    assert chosen.model == resolve("medium", "codex")
    assert chosen.reason is PickReason.INSTALLED
    assert chosen.why == "installed provider=codex leftover=unknown model=gpt-6.1-sol"


def test_skips_missing_binary_then_v1_order() -> None:
    usage = [
        _row("grok_cli", installed=False),
        _row("claude_code"),
        _row("codex", installed=False),
        _row("cursor_cli"),
    ]
    chosen = pick("heavy", usage=usage)
    assert chosen.provider == WorkerProvider.CLAUDE
    assert chosen.model == "opus"
    assert chosen.reason is PickReason.INSTALLED


def test_prefers_available_leftover_over_unknown() -> None:
    usage = [
        _row("grok_cli"),
        _row("claude_code", leftover=Leftover.AVAILABLE, remaining=0.2),
        _row("codex"),
        _row("cursor_cli"),
    ]
    chosen = pick("medium", usage=usage)
    assert chosen.provider == WorkerProvider.CLAUDE
    assert chosen.model == "opus"
    assert chosen.reason is PickReason.LEFTOVER
    assert chosen.why == "leftover provider=claude_code remaining=0.2 model=opus"


def test_highest_remaining_wins_among_available() -> None:
    usage = [
        _row("grok_cli", leftover=Leftover.AVAILABLE, remaining=0.1),
        _row("claude_code", leftover=Leftover.AVAILABLE, remaining=0.8),
        _row("codex", leftover=Leftover.AVAILABLE, remaining=0.5),
        _row("cursor_cli", leftover=Leftover.EXHAUSTED, remaining=0.0),
    ]
    chosen = pick(None, usage=usage)
    assert chosen.provider == WorkerProvider.CLAUDE
    assert chosen.reason is PickReason.LEFTOVER


def test_none_remaining_sorts_after_known_remaining() -> None:
    usage = [
        _row("grok_cli", leftover=Leftover.AVAILABLE),
        _row("claude_code", leftover=Leftover.AVAILABLE, remaining=0.05),
    ]
    chosen = pick("light", usage=usage)
    assert chosen.provider == WorkerProvider.CLAUDE


def test_skips_exhausted_even_if_installed() -> None:
    usage = [
        _row("grok_cli", leftover=Leftover.EXHAUSTED, remaining=0.0),
        _row("claude_code"),
    ]
    chosen = pick("medium", usage=usage)
    assert chosen.provider == WorkerProvider.CLAUDE
    assert chosen.reason is PickReason.INSTALLED


def test_none_eligible_is_value_error() -> None:
    usage = [
        _row("grok_cli", leftover=Leftover.EXHAUSTED, remaining=0.0),
        _row("claude_code", installed=False),
    ]
    with pytest.raises(ProviderUnavailable, match="no eligible worker"):
        pick("medium", usage=usage)


def test_override_wins_even_when_exhausted() -> None:
    usage = [
        _row("grok_cli", leftover=Leftover.EXHAUSTED, remaining=0.0),
        _row("claude_code", leftover=Leftover.AVAILABLE, remaining=0.9),
    ]
    chosen = pick("medium", provider="grok_cli", usage=usage)
    assert chosen.provider == WorkerProvider.GROK
    assert chosen.model == "grok-4.7"
    assert chosen.reason is PickReason.OVERRIDE
    assert "project-override" in chosen.why
    assert "leftover=exhausted" in chosen.why


def test_override_model_is_runtime_string() -> None:
    usage = [_row("claude_code")]
    chosen = pick("light", provider="claude_code", model="opus", usage=usage)
    assert chosen.model == "opus"
    assert chosen.reason is PickReason.OVERRIDE


def test_override_not_installed_is_value_error() -> None:
    usage = [_row("grok_cli", installed=False)]
    with pytest.raises(ProviderUnavailable, match="not installed"):
        pick("medium", provider="grok_cli", usage=usage)


def test_unknown_override_provider_is_value_error() -> None:
    with pytest.raises(ValueError, match="unknown provider") as excinfo:
        pick("medium", provider="not-a-provider", usage=[])
    assert not isinstance(excinfo.value, ProviderUnavailable)


def test_model_override_requires_provider() -> None:
    with pytest.raises(ValueError, match="model override requires provider") as excinfo:
        pick("medium", model="grok-4.6", usage=[])
    assert not isinstance(excinfo.value, ProviderUnavailable)


@pytest.mark.parametrize("model", ["fable", "claude-Fable-5-1", "mythos", "gpt-6-astra"])
def test_frontier_model_pin_is_refused_as_a_usage_error(model: str) -> None:
    """CBN-448 runner cap: a registry `model:` pin never launches a frontier model.

    A plain `ValueError`, not `ProviderUnavailable`, so the loop maps it to exit 2.
    """
    usage = [_row("claude_code"), _row("codex")]
    provider = "codex" if "astra" in model else "claude_code"
    with pytest.raises(ValueError, match="frontier family") as excinfo:
        pick("heavy", provider=provider, model=model, usage=usage)
    assert not isinstance(excinfo.value, ProviderUnavailable)


def test_frontier_model_pin_outranks_an_uninstalled_provider() -> None:
    """Review fix: the registration error (exit 2) wins over machine state (exit 5)."""
    usage = [_row("codex", installed=False)]
    with pytest.raises(ValueError, match="frontier family") as excinfo:
        pick("heavy", provider="codex", model="gpt-6-astra", usage=usage)
    assert not isinstance(excinfo.value, ProviderUnavailable)


def test_heavy_ceiling_model_pin_still_launches() -> None:
    chosen = pick("heavy", provider="codex", model="gpt-6.1-sol", usage=[_row("codex")])
    assert chosen.model == "gpt-6.1-sol"


def test_blank_override_is_treated_as_omit() -> None:
    usage = [_row("grok_cli"), _row("claude_code")]
    chosen = pick("medium", provider="  ", model="", usage=usage)
    assert chosen.provider == WorkerProvider.CLAUDE
    assert chosen.reason is PickReason.INSTALLED


def test_opencode_cli_override_resolves_taskmap() -> None:
    usage = [_row("opencode_cli")]
    chosen = pick("medium", provider="opencode_cli", usage=usage)
    assert chosen.provider == WorkerProvider.OPENCODE
    assert chosen.model == "anthropic/claude-sonnet-4-6"
    assert chosen.reason is PickReason.OVERRIDE


@pytest.mark.parametrize("provider", ["kimi_cli", "copilot_cli"])
def test_dropped_providers_are_unknown(provider: str) -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        pick("medium", provider=provider, usage=[_row(provider)])


def test_omitted_usage_calls_probe_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "caobunga.picker.probe_all",
        lambda: [_row("cursor_cli")],
    )
    chosen = pick("light")
    assert chosen.provider == WorkerProvider.CURSOR
    assert chosen.model == "composer-2.5"
    assert chosen.reason is PickReason.INSTALLED


def test_live_probe_all_blocked_without_injection() -> None:
    """CBN-EPIC-6.5: bare pick() must not reach host PATH via probe_all."""
    with pytest.raises(RuntimeError, match="live usage.probe_all"):  # noqa: RUF043
        pick("light")


# --- Provider allow-list (CBN-091) ---


def test_default_order_is_codex_then_claude_then_grok() -> None:
    usage = [_row("grok_cli"), _row("claude_code"), _row("codex")]
    assert pick("medium", usage=usage).provider == WorkerProvider.CODEX
    assert pick("medium", usage=usage[:2]).provider == WorkerProvider.CLAUDE
    assert pick("medium", usage=usage[:1]).provider == WorkerProvider.GROK


def test_allow_list_restricts_and_orders_when_all_unknown() -> None:
    # The CBN-EPIC-9.4 case: every DUMP_ARGV is None, so PICK_ORDER would
    # hand this to codex. The declared order must win instead.
    usage = [
        _row("grok_cli"),
        _row("claude_code"),
        _row("codex"),
        _row("cursor_cli"),
    ]
    chosen = pick("medium", providers=["claude_code", "codex"], usage=usage)
    assert chosen.provider == WorkerProvider.CLAUDE
    assert chosen.model == "opus"
    assert chosen.reason is PickReason.INSTALLED


def test_allow_list_order_is_the_tiebreak_not_v1_order() -> None:
    usage = [_row("grok_cli"), _row("claude_code"), _row("codex")]
    chosen = pick("medium", providers=["codex", "grok_cli"], usage=usage)
    assert chosen.provider == WorkerProvider.CODEX


def test_leftover_beats_declared_order() -> None:
    usage = [
        _row("claude_code", leftover=Leftover.UNKNOWN),
        _row("codex", leftover=Leftover.AVAILABLE, remaining=0.4),
    ]
    chosen = pick("medium", providers=["claude_code", "codex"], usage=usage)
    assert chosen.provider == WorkerProvider.CODEX
    assert chosen.reason is PickReason.LEFTOVER


def test_higher_remaining_beats_declared_order_within_available() -> None:
    usage = [
        _row("claude_code", leftover=Leftover.AVAILABLE, remaining=0.2),
        _row("codex", leftover=Leftover.AVAILABLE, remaining=0.9),
    ]
    chosen = pick("medium", providers=["claude_code", "codex"], usage=usage)
    assert chosen.provider == WorkerProvider.CODEX


def test_allow_list_skips_exhausted_member_for_later_member() -> None:
    usage = [
        _row("claude_code", leftover=Leftover.EXHAUSTED),
        _row("codex"),
    ]
    chosen = pick("medium", providers=["claude_code", "codex"], usage=usage)
    assert chosen.provider == WorkerProvider.CODEX


def test_allow_list_skips_uninstalled_member() -> None:
    usage = [
        _row("claude_code", installed=False),
        _row("codex"),
    ]
    chosen = pick("medium", providers=["claude_code", "codex"], usage=usage)
    assert chosen.provider == WorkerProvider.CODEX


def test_no_eligible_member_of_allow_list_is_value_error() -> None:
    usage = [
        _row("claude_code", leftover=Leftover.EXHAUSTED),
        _row("codex", installed=False),
        _row("grok_cli"),  # eligible, but not declared
    ]
    with pytest.raises(ValueError, match="no eligible worker in claude_code, codex"):
        pick("medium", providers=["claude_code", "codex"], usage=usage)


def test_allow_list_member_absent_from_usage_rows_is_skipped() -> None:
    chosen = pick("medium", providers=["opencode_cli", "codex"], usage=[_row("codex")])
    assert chosen.provider == WorkerProvider.CODEX


def test_empty_allow_list_is_treated_as_undeclared() -> None:
    usage = [_row("grok_cli"), _row("claude_code")]
    assert pick("medium", providers=[], usage=usage).provider == WorkerProvider.CLAUDE
    assert pick("medium", providers=["  "], usage=usage).provider == WorkerProvider.CLAUDE


def test_allow_list_dedupes_keeping_first_position() -> None:
    usage = [_row("grok_cli"), _row("claude_code"), _row("codex")]
    chosen = pick("medium", providers=["codex", "claude_code", "codex"], usage=usage)
    assert chosen.provider == WorkerProvider.CODEX


def test_provider_pin_overrides_allow_list_entirely() -> None:
    usage = [
        _row("grok_cli", leftover=Leftover.EXHAUSTED),
        _row("claude_code"),
    ]
    chosen = pick("medium", provider="grok_cli", providers=["claude_code"], usage=usage)
    assert chosen.provider == WorkerProvider.GROK
    assert chosen.reason is PickReason.OVERRIDE
