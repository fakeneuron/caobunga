"""Choose a v1 worker provider + runtime model.

Library, not a CLI verb. Composes leftover probes and the task-class map.
The why line is a return value — do not persist it on a report surface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from caobunga.taskmap import resolve
from caobunga.usage import V1_PROVIDERS, Leftover, ProviderUsage, probe_all


class PickReason(StrEnum):
    OVERRIDE = "project-override"
    LEFTOVER = "leftover"
    INSTALLED = "installed"


class ProviderUnavailable(ValueError):
    """No provider can run here: none eligible, or the pinned one is not installed.

    A `ValueError` like every other pick failure; the loop maps this subclass to
    exit 5 and the rest (unknown pin, model-without-provider) to 2 (CBN-EPIC-313.5).
    """


class Pick(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    model: str
    reason: PickReason
    why: str


_ORDER = {provider.value: index for index, provider in enumerate(V1_PROVIDERS)}
_KNOWN = frozenset(_ORDER)


def pick(
    plan_model: str | None,
    *,
    provider: str | None = None,
    model: str | None = None,
    providers: Sequence[str] | None = None,
    usage: Sequence[ProviderUsage] | None = None,
) -> Pick:
    """Pick a provider + runtime model. Unknown / ineligible → ValueError.

    `providers` is the registry allow-list: candidates are filtered to it and the
    declared order replaces `V1_PROVIDERS` as the tiebreak. Leftover still ranks
    first, so a real budget signal beats the operator's ordering. The singular
    `provider` pin is a hard override and ignores the allow-list entirely.
    """
    pin_provider = _blank_to_none(provider)
    pin_model = _blank_to_none(model)
    if pin_model is not None and pin_provider is None:
        msg = "model override requires provider"
        raise ValueError(msg)
    rows = list(usage) if usage is not None else probe_all()
    by_provider = {row.provider: row for row in rows}

    if pin_provider is not None:
        return _override(plan_model, pin_provider, pin_model, by_provider.get(pin_provider))

    allowed = _allow_list(providers)
    chosen = _best(rows, allowed)
    if chosen is None:
        msg = "no eligible worker"
        if allowed is not None:
            msg = f"{msg} in {', '.join(allowed)}"
        raise ProviderUnavailable(msg)
    runtime = resolve(plan_model, chosen.provider)
    reason = PickReason.LEFTOVER if chosen.leftover is Leftover.AVAILABLE else PickReason.INSTALLED
    return Pick(
        provider=chosen.provider,
        model=runtime,
        reason=reason,
        why=_why(reason, chosen.provider, runtime, chosen),
    )


def _override(
    plan_model: str | None,
    provider: str,
    model: str | None,
    row: ProviderUsage | None,
) -> Pick:
    if provider not in _KNOWN:
        msg = f"unknown provider {provider!r}"
        raise ValueError(msg)
    if row is not None and not row.installed:
        msg = f"provider {provider!r} is not installed"
        raise ProviderUnavailable(msg)
    runtime = model if model is not None else resolve(plan_model, provider)
    return Pick(
        provider=provider,
        model=runtime,
        reason=PickReason.OVERRIDE,
        why=_why(PickReason.OVERRIDE, provider, runtime, row),
    )


def _allow_list(providers: Sequence[str] | None) -> dict[str, int] | None:
    """Declared allow-list → token: rank map. A blank/empty list means undeclared."""
    if providers is None:
        return None
    order: dict[str, int] = {}
    for raw in providers:
        token = _blank_to_none(raw)
        if token is not None and token not in order:
            order[token] = len(order)
    return order or None


def _best(
    rows: Sequence[ProviderUsage],
    allowed: Mapping[str, int] | None,
) -> ProviderUsage | None:
    eligible = [
        row for row in rows if _eligible(row) and (allowed is None or row.provider in allowed)
    ]
    if not eligible:
        return None
    order = allowed if allowed is not None else _ORDER
    return min(eligible, key=lambda row: _rank(row, order))


def _eligible(row: ProviderUsage) -> bool:
    return row.installed and row.leftover is not Leftover.EXHAUSTED


def _rank(row: ProviderUsage, order: Mapping[str, int]) -> tuple[int, float, int]:
    class_rank = 0 if row.leftover is Leftover.AVAILABLE else 1
    remaining = -row.remaining if row.remaining is not None else 1.0
    return (class_rank, remaining, order.get(row.provider, len(order)))


def _why(
    reason: PickReason,
    provider: str,
    model: str,
    row: ProviderUsage | None,
) -> str:
    leftover = row.leftover.value if row is not None else "unknown"
    remaining = row.remaining if row is not None else None
    if reason is PickReason.LEFTOVER:
        rem = "none" if remaining is None else f"{remaining:g}"
        return f"leftover provider={provider} remaining={rem} model={model}"
    if reason is PickReason.OVERRIDE:
        return f"project-override provider={provider} model={model} leftover={leftover}"
    return f"installed provider={provider} leftover={leftover} model={model}"


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
