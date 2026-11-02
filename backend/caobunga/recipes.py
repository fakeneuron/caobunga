"""Headless argv recipes: how each provider CLI takes a one-shot prompt unattended.

A syntax table plus the one function that walks it (`build_argv`). `local`
spawns what this builds; `taskmap` decides the model and effort values that
fill its slots.
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from dataclasses import dataclass

from caobunga.errors import UserError
from caobunga.usage import WorkerProvider
from caobunga.worker_log import Compactor, compact_stream_line


@dataclass(frozen=True)
class Recipe:
    """How one provider CLI takes a one-shot prompt without a human present."""

    lead: tuple[str, ...] = ()
    # Switches stdout to one JSON event per line as the worker runs (CBN-377),
    # parsed by this row's own `compact` before the tee writes it. Empty for a
    # provider whose default output already streams, or whose flag is unverified.
    stream_argv: tuple[str, ...] = ()
    compact: Compactor | None = None
    approve: tuple[str, ...] = ()
    # Harness-side guards the CLI accepts as argv (CBN-212). Empty for a
    # provider whose verified recipe exposes no hook surface.
    guard_argv: tuple[str, ...] = ()
    model_flag: str | None = None
    prompt_flag: str | None = None
    effort_argv: tuple[str, ...] = ()


# Placeholder substituted into `Recipe.effort_argv` by `build_argv`. Internal
# convention, not a template language: it appears only in `RECIPES` rows below
# and is replaced in exactly one place. Codex needs it embedded in a `key=value`
# token rather than standing alone, which is why the slot is a tuple of fragments
# and not a flag plus a bare value.
EFFORT_SLOT = "{effort}"


# Claude Code `--settings` payload registering `caobunga.hooks.guard` as a
# `PreToolUse` command hook on `Bash` and the file-writing tools. The hook denies
# `run_in_background` with `prompt.FOREGROUND_ONLY`'s instruction as the reason —
# the CBN-199 sentence made mechanical after CBN-210 broke it the day it
# shipped (CBN-212) — and denies `caobunga register` or a file write under the
# store home, so a worker cannot retier or re-enable a project (CBN-247). The
# command is this process's own interpreter (`sys.executable`, `shlex`-quoted:
# Claude Code runs hook commands through a shell) with `-m`, so
# the worker needs nothing on PATH — the orchestrator that built the argv is
# the one whose environment `local._default_spawn` inherits, `CAOBUNGA_HOME`
# re-exported absolute (CBN-251). `-P` keeps the repo cwd off `sys.path`, so a
# target repo's own `caobunga/` cannot shadow this install (as `fleet.spawn_run`
# does, CBN-312). Built once at import;
# `build_argv` stays a pure table walk. Verified live 2026-09-20 under
# `-p --permission-mode bypassPermissions` (hooks run in every permission mode;
# only `--bare` skips them, and no recipe carries it).
FOREGROUND_HOOK_COMMAND = f"{shlex.quote(sys.executable)} -P -m caobunga.hooks"
CLAUDE_GUARD_SETTINGS = json.dumps(
    {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                    "hooks": [{"type": "command", "command": FOREGROUND_HOOK_COMMAND}],
                }
            ]
        }
    },
    separators=(",", ":"),
)


# Verified 2026-08-22 from each CLI's own --help (CBN-EPIC-9.2); `effort_argv`
# verified the same way 2026-08-26 (CBN-EPIC-15.2). Auto-approve is deliberate and
# unconditional: an unattended worker that stops to ask has nobody to answer it.
# Bump a row when a CLI renames a flag; do not add a provider here without checking
# its --help first — that applies to the effort slot as much as to the rest of the row.
#
# No row carries its provider's accepted effort *values*: the sets differ per vendor
# (claude has `max`, grok does not), so no one closed enum describes them honestly,
# and choosing which value a task class gets is `taskmap.EFFORT`'s job (CBN-EPIC-15.3).
# This table states syntax only.
RECIPES: dict[str, Recipe] = {
    WorkerProvider.GROK: Recipe(
        approve=("--always-approve",),
        model_flag="-m",
        prompt_flag="-p",
        # `--effort` is an accepted alias; the canonical long form is pinned.
        effort_argv=("--reasoning-effort", EFFORT_SLOT),
    ),
    WorkerProvider.CLAUDE: Recipe(
        lead=("-p",),
        # Text mode prints only the final result, at exit, so a killed worker
        # left a 0-byte log (CBN-377). `-p` refuses stream-json without `--verbose`.
        stream_argv=("--output-format", "stream-json", "--verbose"),
        compact=compact_stream_line,
        approve=("--permission-mode", "bypassPermissions"),
        guard_argv=("--settings", CLAUDE_GUARD_SETTINGS),
        model_flag="--model",
        effort_argv=("--effort", EFFORT_SLOT),
    ),
    WorkerProvider.CODEX: Recipe(
        lead=("exec",),
        approve=("--dangerously-bypass-approvals-and-sandbox",),
        model_flag="-m",
        # Codex has no effort flag; it takes a config override (`-c key=value`).
        effort_argv=("-c", f"model_reasoning_effort={EFFORT_SLOT}"),
    ),
    WorkerProvider.CURSOR: Recipe(
        lead=("-p",),
        approve=("--force", "--trust"),
        model_flag="--model",
        # Empty on purpose: Cursor encodes effort in the model id, so routing it
        # is `taskmap.MODELS`' job and there is nothing for argv to add.
        effort_argv=(),
    ),
}


def hooks_available() -> bool:
    """Whether this interpreter can run ``python -m caobunga.hooks`` (CBN-216).

    CBN-212's ``--settings`` payload registers that command as a PreToolUse
    hook. A snapshot install that predates ``hooks.py`` still builds the
    payload (the recipe is a constant) but the worker then ``worker-exited``
    on the first Bash call. Omit the pair when the module is missing; echo
    the miss on ``status`` ``source:`` so it is visible without a dispatch.
    """
    return importlib.util.find_spec("caobunga.hooks") is not None


def no_recipe(provider: str) -> UserError:
    """The one message both raise sites use. Names the supported set, not just the miss.

    `RECIPES` is narrower than `usage.V1_PROVIDERS` on purpose, and the registry
    allow-list is validated against the wider set (docs/CONTRACT.md §Registry), so
    this error is where an operator first learns the adapter's real reach.
    """
    supported = ", ".join(sorted(RECIPES))
    return UserError(
        f"no local worker recipe for provider {provider!r}: local supports {supported}"
    )


def build_argv(
    binary: str,
    provider: str,
    model: str | None,
    message: str,
    *,
    effort: str | None = None,
) -> list[str]:
    """Headless one-shot argv for one provider. Unsupported provider is a user error.

    `effort` is keyword-only and defaults to None, so omitting it yields exactly the
    argv this function built before CBN-EPIC-15.2. A provider whose `effort_argv` is
    empty (Cursor, which bakes effort into the model id) drops the value silently —
    the same "this provider does not take one" signal `prompt_flag=None` already uses.
    """
    recipe = RECIPES.get(provider)
    if recipe is None:
        raise no_recipe(provider)
    argv = [binary, *recipe.lead, *recipe.stream_argv, *recipe.approve]
    if recipe.guard_argv and hooks_available():
        argv.extend(recipe.guard_argv)
    if model and recipe.model_flag:
        argv.extend([recipe.model_flag, model])
    if effort and recipe.effort_argv:
        argv.extend(part.replace(EFFORT_SLOT, effort) for part in recipe.effort_argv)
    if recipe.prompt_flag:
        argv.extend([recipe.prompt_flag, message])
    else:
        argv.append(message)
    return argv
