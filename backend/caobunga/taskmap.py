"""Map Flaitron PLAN [model] tokens to runtime provider model ids and effort rungs.

Library, not a CLI verb. Does not probe leftover usage or pick a provider.
Pins are a named table — bump cells when CLIs rename; do not bump schema_version.
"""

from __future__ import annotations

from enum import StrEnum

from caobunga.usage import WorkerProvider


class TaskClass(StrEnum):
    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"


# SPEC/model.md inherent-tier aliases. Unknown / omitted tokens → MEDIUM.
ALIASES: dict[str, TaskClass] = {
    "light": TaskClass.LIGHT,
    "haiku": TaskClass.LIGHT,
    "medium": TaskClass.MEDIUM,
    "sonnet": TaskClass.MEDIUM,
    "grok": TaskClass.MEDIUM,
    "codex": TaskClass.MEDIUM,
    "heavy": TaskClass.HEAVY,
    # SPEC/model.md's exploratory rung sits *above* heavy, but caobunga's ladder
    # has three rungs and flaitron bands no roster model at xheavy by default, so
    # it buckets to the heaviest one available rather than the medium default an
    # unrecognized token would take (CBN-112). This is a *tier* answer for attended
    # reads only — no launch ever reaches it, because `flaitron.eligibility` denies
    # `[xheavy]` outright (CBN-114): the rung is the operator's, manual-only.
    "xheavy": TaskClass.HEAVY,
    "opus": TaskClass.HEAVY,
    # No `fable` / `mythos` (nor `frontier`): the frontier band is denied at launch
    # (`flaitron.eligibility`, CBN-331), so no dispatch ever classifies them.
}

# Runtime provider `model=` strings, pinned 2026-08-20 from live CLI inventories
# (grok_cli / cursor_cli rows refreshed 2026-09-24, CBN-254, live `grok models` +
# `cursor-agent models` — Cursor's Grok 4.7 ids dropped the `cursor-` prefix the
# 4.6 generation used, so the live-listed `grok-4.7-*` ids are pinned as-is rather
# than string-replacing the old `cursor-grok-4.6-*` shape).
# Grok has no lighter SKU and Codex's tiers are account-gated here (Sol/Astra refused
# 2026-09-12 on a ChatGPT-account Codex — CONTRACT §"Task-class → model map"), so
# their rows are flat: the task class picks no distinct model id for them. It is no
# longer *discarded* for them — `EFFORT` below routes the same class to a
# reasoning-effort rung, which the local adapter passes
# (claude `--effort`, grok `--reasoning-effort`, codex `-c model_reasoning_effort=`).
# That was the gap CBN-EPIC-14.5 surfaced and CBN-EPIC-15.3 closed.
# Claude coding floor is `sonnet` (help advertises sonnet/opus/fable, not haiku;
# `fable` is never dispatched — CBN-331 caps caobunga at `[heavy]`);
# medium takes `opus` (CBN-255, operator decision 2026-09-24).
# Cursor bakes effort into the id; light uses Composer (smallest capable).
MODELS: dict[str, dict[TaskClass, str]] = {
    WorkerProvider.GROK: {
        TaskClass.LIGHT: "grok-4.7",
        TaskClass.MEDIUM: "grok-4.7",
        TaskClass.HEAVY: "grok-4.7",
    },
    WorkerProvider.CLAUDE: {
        TaskClass.LIGHT: "sonnet",
        TaskClass.MEDIUM: "opus",
        TaskClass.HEAVY: "opus",
    },
    WorkerProvider.CODEX: {
        TaskClass.LIGHT: "gpt-5.6-terra",
        TaskClass.MEDIUM: "gpt-5.6-terra",
        TaskClass.HEAVY: "gpt-5.6-terra",
    },
    WorkerProvider.CURSOR: {
        TaskClass.LIGHT: "composer-2.5",
        TaskClass.MEDIUM: "grok-4.7-high",
        TaskClass.HEAVY: "grok-4.7-xhigh",
    },
    WorkerProvider.OPENCODE: {
        TaskClass.LIGHT: "anthropic/claude-sonnet-4-6",
        TaskClass.MEDIUM: "anthropic/claude-sonnet-4-6",
        TaskClass.HEAVY: "anthropic/claude-opus-4-6",
    },
}

# Provider → task class → reasoning-effort rung, passed to the CLI by `local.build_argv`
# via the recipe's `effort_argv` (CBN-EPIC-15.2). Per-provider since CBN-332: each row is
# that provider's column of flaitron `docs/PLATFORMS.md` §"Tier x platform map"
# (as of 2026-10-08). Ladders verified live on 2026-08-26:
#
#   claude_code  low medium high xhigh max            (`claude --help`)
#   grok_cli     low medium high xhigh                (its own rejection message)
#   codex        low medium high xhigh max ultra      (`~/.codex/models_cache.json`)
#
# claude_code is sonnet@high, opus@medium, opus@high (CBN-331), so light runs a higher
# rung than medium on purpose: it is a smaller model. grok_cli and codex have flat model
# ids. grok_cli light takes the upper end of its medium-high range (CBN-360), so light
# and medium both dispatch high and only heavy (xhigh) differs. codex stays
# medium/high/xhigh: Terra is pinned flat, and effort is still what separates its
# classes (CBN-332). Cursor's heavy pin (`grok-4.7-xhigh`, above) bakes in the same
# rung grok_cli dispatches, and so does its medium pin (`grok-4.7-high`, CBN-333).
# A v1 provider with no row takes no effort flag.
# Bump a cell when a vendor's ladder changes; the CONTRACT table is guarded against this one.
EFFORT: dict[str, dict[TaskClass, str]] = {
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

# PLAN tokens that pass through as runtime ids when the provider advertises them.
CLAUDE_RUNTIME_ALIASES: frozenset[str] = frozenset({"sonnet", "opus"})


def classify(plan_model: str | None) -> TaskClass:
    """Bucket a PLAN.md `[model]` token to a task class. Omit/unknown → medium."""
    if not plan_model:
        return TaskClass.MEDIUM
    return ALIASES.get(plan_model.strip().casefold(), TaskClass.MEDIUM)


def resolve(plan_model: str | None, provider: str) -> str:
    """Runtime provider model id for one provider. Unknown provider raises ValueError."""
    table = MODELS.get(provider)
    if table is None:
        msg = f"unknown provider {provider!r}"
        raise ValueError(msg)
    token = _normalize_token(plan_model)
    if token is not None and provider == WorkerProvider.CLAUDE and token in CLAUDE_RUNTIME_ALIASES:
        return token
    return table[classify(plan_model)]


def resolve_effort(plan_model: str | None, provider: str) -> str | None:
    """Reasoning-effort rung for a PLAN.md `[model]` token on one provider.

    Omit/unknown token → the provider's medium rung. A known provider with no `EFFORT`
    row takes no effort flag (Cursor bakes it into the model id) and gets `None`.
    Unknown provider raises ValueError, like `resolve`, so a misspelled key cannot
    silently drop the flag.
    """
    if provider not in MODELS:
        msg = f"unknown provider {provider!r}"
        raise ValueError(msg)
    table = EFFORT.get(provider)
    if table is None:
        return None
    return table[classify(plan_model)]


def _normalize_token(plan_model: str | None) -> str | None:
    if not plan_model:
        return None
    token = plan_model.strip().casefold()
    return token or None
