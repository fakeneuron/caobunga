"""Task-class → runtime model map (CBN-021). No live CLI calls."""

from __future__ import annotations

import pytest

from caobunga.local import RECIPES
from caobunga.taskmap import (
    ALIASES,
    EFFORT,
    MODELS,
    TaskClass,
    classify,
    resolve,
    resolve_effort,
)
from caobunga.usage import V1_PROVIDERS, WorkerProvider


def test_classify_omit_and_unknown_are_medium() -> None:
    assert classify(None) is TaskClass.MEDIUM
    assert classify("") is TaskClass.MEDIUM
    assert classify("  ") is TaskClass.MEDIUM
    assert classify("gpt-5") is TaskClass.MEDIUM
    assert classify("nope") is TaskClass.MEDIUM


def test_classify_aliases_match_spec_inherent_tiers() -> None:
    assert classify("light") is TaskClass.LIGHT
    assert classify("HAIKU") is TaskClass.LIGHT
    assert classify("sonnet") is TaskClass.MEDIUM
    assert classify(" grok ") is TaskClass.MEDIUM
    assert classify("codex") is TaskClass.MEDIUM
    assert classify("xheavy") is TaskClass.HEAVY
    assert classify("opus") is TaskClass.HEAVY
    # The frontier band is denied at launch (CBN-331), so it has no alias.
    assert classify("fable") is TaskClass.MEDIUM
    assert classify("mythos") is TaskClass.MEDIUM
    assert classify("frontier") is TaskClass.MEDIUM
    assert set(ALIASES) == {
        "light",
        "haiku",
        "medium",
        "sonnet",
        "grok",
        "codex",
        "heavy",
        "xheavy",
        "opus",
    }


def test_cursor_pins_bake_the_grok_cli_effort_rung() -> None:
    for task_class in (TaskClass.MEDIUM, TaskClass.HEAVY):
        rung = EFFORT[WorkerProvider.GROK][task_class]
        assert MODELS[WorkerProvider.CURSOR][task_class] == f"grok-4.7-{rung}"


def test_pinned_runtime_models() -> None:
    assert resolve("medium", "grok_cli") == "grok-4.7"
    assert resolve("light", WorkerProvider.GROK) == "grok-4.7"
    assert resolve("heavy", WorkerProvider.GROK) == "grok-4.7"
    assert resolve("light", WorkerProvider.CLAUDE) == "sonnet"
    assert resolve("medium", WorkerProvider.CLAUDE) == "opus"
    assert resolve("heavy", WorkerProvider.CLAUDE) == "opus"
    assert resolve("sonnet", WorkerProvider.CLAUDE) == "sonnet"
    assert resolve("fable", WorkerProvider.CLAUDE) == "opus"
    assert resolve(None, WorkerProvider.CODEX) == "gpt-5.6-terra"
    assert resolve("light", WorkerProvider.CURSOR) == "composer-2.5"
    assert resolve("medium", WorkerProvider.CURSOR) == "grok-4.7-high"
    assert resolve("opus", WorkerProvider.CURSOR) == "grok-4.7-xhigh"
    assert resolve("fable", WorkerProvider.CURSOR) == "grok-4.7-high"
    assert resolve("medium", WorkerProvider.OPENCODE) == "anthropic/claude-sonnet-4-6"
    assert resolve("heavy", WorkerProvider.OPENCODE) == "anthropic/claude-opus-4-6"


def test_concrete_token_is_class_not_provider_pin() -> None:
    assert resolve("opus", WorkerProvider.CLAUDE) == "opus"
    assert resolve("opus", WorkerProvider.GROK) == "grok-4.7"


def test_unknown_provider_is_value_error() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        resolve("medium", "fable")


def test_every_v1_provider_has_all_classes() -> None:
    for provider in V1_PROVIDERS:
        table = MODELS[provider]
        assert set(table) == set(TaskClass)
        for model in table.values():
            assert model


def test_opencode_stays_named_and_recipe_less() -> None:
    """CBN-360: opencode stays in the v1 set; kimi and copilot leave it."""
    names = {provider.value for provider in V1_PROVIDERS}
    assert WorkerProvider.OPENCODE in V1_PROVIDERS
    assert WorkerProvider.OPENCODE in MODELS
    assert WorkerProvider.OPENCODE not in RECIPES
    assert "kimi_cli" not in names
    assert "copilot_cli" not in names


def test_effort_rungs_are_the_decided_mapping() -> None:
    """Each row is that provider's flaitron tier-by-platform column (CBN-331, CBN-332).

    grok light is the upper end of medium-high (CBN-360), so it shares medium's rung.
    """
    assert EFFORT == {
        WorkerProvider.GROK: {
            TaskClass.LIGHT: "high",
            TaskClass.MEDIUM: "high",
            TaskClass.HEAVY: "xhigh",
        },
        WorkerProvider.CLAUDE: {
            TaskClass.LIGHT: "high",
            TaskClass.MEDIUM: "medium",
            TaskClass.HEAVY: "high",
        },
        WorkerProvider.CODEX: {
            TaskClass.LIGHT: "medium",
            TaskClass.MEDIUM: "high",
            TaskClass.HEAVY: "xhigh",
        },
    }


def test_resolve_effort_follows_classify() -> None:
    assert resolve_effort("light", WorkerProvider.CLAUDE) == "high"
    assert resolve_effort("HAIKU", WorkerProvider.CLAUDE) == "high"
    assert resolve_effort("sonnet", WorkerProvider.CLAUDE) == "medium"
    assert resolve_effort("light", WorkerProvider.GROK) == "high"
    assert resolve_effort(" grok ", WorkerProvider.GROK) == "high"
    assert resolve_effort("opus", WorkerProvider.GROK) == "xhigh"
    assert resolve_effort("xheavy", WorkerProvider.CODEX) == "xhigh"


def test_resolve_effort_omit_and_unknown_are_the_medium_rung() -> None:
    for provider, table in EFFORT.items():
        assert resolve_effort(None, provider) == table[TaskClass.MEDIUM]
        assert resolve_effort("", provider) == table[TaskClass.MEDIUM]
        assert resolve_effort("  ", provider) == table[TaskClass.MEDIUM]
        assert resolve_effort("gpt-5", provider) == table[TaskClass.MEDIUM]


def test_resolve_effort_is_none_without_an_effort_flag() -> None:
    """Cursor bakes effort into the model id; opencode takes no flag."""
    for provider in set(V1_PROVIDERS) - set(EFFORT):
        assert resolve_effort("heavy", provider) is None


def test_resolve_effort_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        resolve_effort("heavy", "claude")


def test_effort_rows_are_exactly_the_flag_taking_recipes() -> None:
    """A recipe with an effort slot but no `EFFORT` row would launch every class flagless."""
    assert {p for p, recipe in RECIPES.items() if recipe.effort_argv} == set(EFFORT)


def test_effort_rungs_sit_in_each_vendor_ladder() -> None:
    """Ladders verified live 2026-08-26 (see `taskmap.EFFORT`'s provenance comment)."""
    ladders: dict[str, set[str]] = {
        WorkerProvider.GROK: {"low", "medium", "high", "xhigh"},
        WorkerProvider.CLAUDE: {"low", "medium", "high", "xhigh", "max"},
        WorkerProvider.CODEX: {"low", "medium", "high", "xhigh", "max", "ultra"},
    }
    assert set(EFFORT) == set(ladders)
    for provider, table in EFFORT.items():
        assert set(table) == set(TaskClass)
        assert set(table.values()) <= ladders[provider]


def test_every_class_dispatches_distinctly_on_each_effort_provider() -> None:
    """CBN-332: no two classes share both model id and rung, except grok light/medium.

    CBN-360 takes grok's light cell at the upper end of medium-high, so light and
    medium share grok-4.7 @ high. Heavy stays xhigh. Codex and claude stay distinct.
    """
    for provider in EFFORT:
        dispatched = {
            (resolve(c.value, provider), resolve_effort(c.value, provider)) for c in TaskClass
        }
        if provider == WorkerProvider.GROK:
            assert dispatched == {("grok-4.7", "high"), ("grok-4.7", "xhigh")}
            continue
        assert len(dispatched) == len(TaskClass), provider
