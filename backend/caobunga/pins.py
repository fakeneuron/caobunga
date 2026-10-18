"""Read-only stale-pin check: `taskmap.MODELS` ids vs. each installed provider's
non-interactive model catalog, when one exists (CBN-166).

Library, not a CLI verb. Never refuses, never re-pins — same posture as the
`flaitron` module's unparsed/drift reporting. Query failures degrade to a
`message`, mirroring `usage.probe`; this module never raises.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from caobunga.taskmap import MODELS
from caobunga.usage import V1_PROVIDERS, WorkerProvider, first_binary, run_argv

WhichFn = Callable[[str], str | None]
RunFn = Callable[[Sequence[str]], str]

# Non-interactive model-catalog dumps. None = no safe command. Codex since
# CBN-166; grok and cursor since CBN-256 (both print a text list, verified live
# 2026-09-24). Claude's `--help` advertises aliases only, no listing.
CATALOG_ARGV: dict[str, tuple[str, ...] | None] = {
    WorkerProvider.GROK: ("grok", "models"),
    WorkerProvider.CLAUDE: None,
    WorkerProvider.CODEX: ("codex", "debug", "models"),
    WorkerProvider.CURSOR: ("cursor-agent", "models"),
    WorkerProvider.OPENCODE: None,
}

# Catalog listings are slower than a usage dump (`cursor-agent models` measured
# 11-13s on 2026-09-24, CBN-256 review), so they get their own budget rather than
# `usage.DEFAULT_TIMEOUT`, which would degrade cursor to "unreadable" every time.
CATALOG_TIMEOUT = 30.0

# Providers whose catalog is known to hide ids that are still reachable at
# runtime (CBN-165: codex's TAP-gated Sol/Astra never appear in `codex debug
# models`, yet Terra does and is live). A partial catalog's silence on an id
# proves nothing, so a partial provider's pins are never reported stale.
CATALOG_PARTIAL: frozenset[str] = frozenset({WorkerProvider.CODEX})


class PinReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    installed: bool = False
    partial: bool = False
    stale: tuple[str, ...] = ()
    message: str | None = None


def check_all(
    *,
    which: WhichFn | None = None,
    run: RunFn | None = None,
    timeout: float = CATALOG_TIMEOUT,
) -> list[PinReport]:
    """Pin-freshness report per v1 worker. Order matches V1_PROVIDERS."""
    return [check(provider, which=which, run=run, timeout=timeout) for provider in V1_PROVIDERS]


def check(
    provider: str,
    *,
    which: WhichFn | None = None,
    run: RunFn | None = None,
    timeout: float = CATALOG_TIMEOUT,
) -> PinReport:
    """Compare `provider`'s pinned ids against its catalog, when one exists.

    Uninstalled → not reported (`installed=False`; the caller renders nothing).
    No catalog command → `message="catalog unavailable"`. A catalog known
    partial → `partial=True`, `stale=()` always, since absence there is not
    evidence. A full catalog that parses empty → `message="catalog unreadable"`
    (CBN-256). Otherwise every pinned id absent from a full catalog is `stale`.
    """
    which_fn = which or shutil.which
    installed = first_binary(provider, which_fn) is not None
    if not installed:
        return PinReport(provider=provider, installed=False)

    argv = CATALOG_ARGV.get(provider)
    if argv is None:
        return PinReport(provider=provider, installed=True, message="catalog unavailable")

    run_fn = run or (lambda cmd: run_argv(cmd, timeout))
    parse = CATALOG_PARSE.get(provider, parse_catalog)
    try:
        stdout = run_fn(argv)
        catalog = parse(stdout)
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RuntimeError,
    ):
        return PinReport(provider=provider, installed=True, message="catalog unreadable")

    if provider in CATALOG_PARTIAL:
        return PinReport(provider=provider, installed=True, partial=True)

    # An empty full catalog is a logged-out CLI or a changed output shape, not
    # proof every pin is gone — reporting them all stale would be a false alarm.
    if not catalog:
        return PinReport(provider=provider, installed=True, message="catalog unreadable")

    pinned = sorted({model_id for model_id in MODELS.get(provider, {}).values()})
    stale = tuple(model_id for model_id in pinned if model_id not in catalog)
    return PinReport(provider=provider, installed=True, stale=stale)


def parse_catalog(stdout: str) -> set[str]:
    """Parse a model-catalog JSON dump into the set of advertised `slug` ids.

    Accepts a bare list, or a dict wrapping the list under `models` / `data`
    (`codex debug models`'s own shape is a bare list of `{"slug": ...}`
    objects; the wrapped forms are tolerated for whatever a future provider's
    catalog command ships). Any other shape parses to an empty set rather than
    raising — `check` reports an empty full catalog as unreadable (CBN-256),
    and the partial-catalog guard keeps codex from being compared at all; a
    provider with a non-JSON shape registers its own parser in `CATALOG_PARSE`.
    """
    data = json.loads(stdout.strip())
    if isinstance(data, dict):
        data = data.get("models", data.get("data", []))
    if not isinstance(data, list):
        return set()
    slugs: set[str] = set()
    for entry in data:
        if isinstance(entry, dict):
            slug = entry.get("slug")
            if isinstance(slug, str):
                slugs.add(slug)
        elif isinstance(entry, str):
            slugs.add(entry)
    return slugs


_GROK_ROW = re.compile(r"^\s*[*-]\s+(\S+)")
_CURSOR_ROW = re.compile(r"^(\S+) - ")


def parse_grok_catalog(stdout: str) -> set[str]:
    """`grok models` text: bulleted ids (`  * grok-4.7 (default)`, `  - grok-4.6`)."""
    return {m.group(1) for line in stdout.splitlines() if (m := _GROK_ROW.match(line))}


def parse_cursor_catalog(stdout: str) -> set[str]:
    """`cursor-agent models` text: one `<id> - <Display Name>` line per model."""
    return {m.group(1) for line in stdout.splitlines() if (m := _CURSOR_ROW.match(line))}


# Per-provider catalog parser; a provider absent here takes the JSON `parse_catalog`.
CATALOG_PARSE: dict[str, Callable[[str], set[str]]] = {
    WorkerProvider.GROK: parse_grok_catalog,
    WorkerProvider.CURSOR: parse_cursor_catalog,
}
