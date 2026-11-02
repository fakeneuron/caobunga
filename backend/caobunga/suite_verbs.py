"""Run a registered repo's fleet suite verbs, nothing else (CBN-174).

Born as the Hermes reviewer's `code_suite` MCP server: two closes on
2026-09-12 (CBN-167, CBN-171) were green on the worker's targeted run and red
on the full suite, and a reviewer that can only read cannot tell the two
apart. Since CBN-EPIC-202.3 the loop runs the same verbs itself on every close
(`suite.py`), and CBN-258 retired the server with the Hermes review — this
module is now the library that loop-side suite and the fitness probe share
(`suite_mcp` until CBN-EPIC-244.7 renamed it for that role).

The grant is bounded three ways, in order of authority:

1. **The argv is fixed.** `run_tests` is `just test`; `run_lint` is
   `just lint`; `run_typecheck` is `just typecheck` (CBN-185 — pyright is a
   fleet gate since CBN-170 but a separate recipe from `lint`, so without it a
   type-red close was a check the reviewer listed as not performed). The
   caller chooses *whether* the suite runs, never *what* runs — free
   arguments would admit `ruff --fix`, a write no command allow-list stops.
2. **The repo must be registered.** `repo_path` is accepted only when it
   names a registry record (`Store.find_by_path`, CBN-131's case-fold rule),
   or the same subpath of a registered project's own `wt-<ID>` dispatch
   worktree (`worktree.origin`, CBN-EPIC-450.3), where the pre-launch
   baseline runs. Anything else refuses before a subprocess exists.
3. **The tree is measured, not trusted.** `git status --porcelain -uall` is
   captured before and after, and a difference is reported on the result —
   the suite's own caches are gitignored, so a changed tree is a fact the
   result reports, not one the tool hides.

What the bound does not cover, recorded rather than hidden: the suite is the
*repo's* code (its justfile, its tests) and runs as the operator's user. That
is the same code the worker ran minutes earlier in the same context.

Tests inject `run=` and `store=`; no live `just`.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from caobunga.git import git_dirt
from caobunga.spawn import as_text, run_detached
from caobunga.store import Store
from caobunga.worktree import origin

# Tool name → the one argv it may run, from the repo root. `just` is the
# fleet's uniform verb layer: `test` covers every stack present (backend
# pytest and frontend vitest), `lint` and `typecheck` likewise — the suite as
# the operator and CI define it, not a Python-only subset.
VERBS: dict[str, tuple[str, ...]] = {
    "run_tests": ("just", "test"),
    "run_lint": ("just", "lint"),
    "run_typecheck": ("just", "typecheck"),
}
# Per-call ceilings. A full backend suite on this fleet runs ~100 s
# (measured on caobunga itself).
# `typecheck` (pyright + tsc) measured ~7 s warm on caobunga; the ceiling
# leaves room for a cold pyright on a larger fleet backend.
TIMEOUTS: dict[str, float] = {
    "run_tests": 600.0,
    "run_lint": 120.0,
    "run_typecheck": 300.0,
}
# The reader wants the *end* of the output — pytest's failure summary and
# `ruff`'s findings both trail — so a chatty suite is cut from the front.
TAIL_CHARS = 12_000
JUSTFILE_NAMES = ("justfile", "Justfile", ".justfile")
# The one refusal that is the machine's, not the repo's: the pre-launch
# baseline exits 5 on it rather than skipping the repo (CBN-420).
JUST_MISSING = "`just` is not on PATH"

# just's own error when the justfile is present but the verb's recipe is not
# (just 1.58.0: ``error: justfile does not contain recipe `typecheck` ``; the
# capitalised spelling is older releases). A missing recipe is the same class as
# a missing justfile — the repo cannot answer the verb — so `run_verb`
# reclassifies the nonzero exit as `refused` rather than letting it fold `red`
# (CBN-211: a fleet repo's `CORE-12`, 2026-09-20, stopped the fleet's one
# `proceed-on-green` drain as a code red over a `check:`-not-`typecheck:`
# justfile). Matched on the verb's own name only: a recipe that exists and
# fails by invoking a missing sub-recipe is that recipe failing, and stays red.
MISSING_RECIPE = re.compile(
    r"^error: [Jj]ustfile does not contain recipe `([^`\n]+)`", re.MULTILINE
)

# The fleet verb names, derived from `VERBS` rather than a second literal
# tuple — `suite.py`'s `_to_verb` makes the same argument for its own copy.
FLEET_VERBS: tuple[str, ...] = tuple(argv[-1] for argv in VERBS.values())
# `just --summary` (CBN-215) is read-only and near-instant; this ceiling only
# guards a hung or interactive-prompting justfile, not a real recipe run.
SUMMARY_TIMEOUT = 10.0

RunFn = Callable[[Sequence[str], Path, float], tuple[int, str]]
# Opens the caller's mutual exclusion around one verb's spawn (CBN-310).
GuardFn = Callable[[], AbstractContextManager[None]]


@dataclass(frozen=True)
class SuiteResult:
    verb: str
    argv: tuple[str, ...]
    cwd: Path
    exit_code: int | None
    duration: float
    output: str
    truncated: bool = False
    timed_out: bool = False
    refused: str | None = None
    tree_before: tuple[str, ...] = ()
    tree_after: tuple[str, ...] = ()
    tree_unknown: bool = False

    @property
    def tree_changed(self) -> bool:
        return not self.tree_unknown and self.tree_before != self.tree_after

    def render(self) -> str:
        """The rendered result `suite.py` logs. Every fact a reader may need is here."""
        if self.refused:
            return f"{self.verb}: refused — {self.refused}"
        lines = [
            f"{self.verb}: `{' '.join(self.argv)}` in {self.cwd}",
            f"exit: {self.exit_code if self.exit_code is not None else 'none'}"
            + (" (timed out)" if self.timed_out else ""),
            f"duration: {self.duration:.1f}s",
        ]
        if self.tree_unknown:
            lines.append("tree: unknown (git status failed)")
        elif self.tree_changed:
            added = sorted(set(self.tree_after) - set(self.tree_before))
            gone = sorted(set(self.tree_before) - set(self.tree_after))
            lines.append(
                "tree: CHANGED — "
                + "; ".join(
                    part
                    for part in (
                        f"appeared: {', '.join(added)}" if added else "",
                        f"vanished: {', '.join(gone)}" if gone else "",
                    )
                    if part
                )
            )
        else:
            lines.append("tree: unchanged (git status --porcelain -uall)")
        lines.append(
            "output" + (f" (last {TAIL_CHARS} chars; head dropped)" if self.truncated else "") + ":"
        )
        lines.append(self.output.rstrip() or "(no output)")
        return "\n".join(lines)


def run_verb(
    verb: str,
    repo_path: str,
    *,
    store: Store | None = None,
    run: RunFn | None = None,
    timeout: float | None = None,
    guard: GuardFn | None = None,
) -> SuiteResult:
    """Run one fixed verb in a registered repo. Refusals are results, not raises.

    `guard` is entered only after the refusal check, so a verb that cannot run
    never waits on it; it spans the porcelain reads and the spawn, so the wait
    stays outside the measured `duration` (CBN-310).
    """
    argv = VERBS[verb]
    ceiling = timeout if timeout is not None else TIMEOUTS[verb]
    raw = Path(repo_path)
    refused = _refusal(raw, store if store is not None else Store())
    if refused is not None:
        return SuiteResult(verb, argv, raw, None, 0.0, "", refused=refused)
    repo = raw.expanduser().resolve()
    with guard() if guard is not None else contextlib.nullcontext():
        before, unknown = _porcelain(repo)
        started = time.monotonic()
        timed_out = False
        try:
            code, output = (run or run_detached)(argv, repo, ceiling)
            exit_code: int | None = code
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            output = as_text(exc.output) + as_text(exc.stderr)
        duration = time.monotonic() - started
        after, unknown_after = _porcelain(repo)
    tail, truncated = tail_output(output)
    refused = _missing_recipe(argv[-1], exit_code, output)
    return SuiteResult(
        verb,
        argv,
        repo,
        None if refused else exit_code,
        duration,
        tail,
        truncated=truncated,
        timed_out=timed_out,
        refused=refused,
        tree_before=before,
        tree_after=after,
        tree_unknown=unknown or unknown_after,
    )


def _refusal(raw: Path, store: Store) -> str | None:
    if not raw.is_absolute():
        return f"repo_path must be absolute, got {str(raw)!r}"
    repo = raw.expanduser().resolve()
    if not repo.is_dir():
        return f"not a directory: {repo}"
    if store.find_by_path(repo) is None:
        stands_for = origin(repo)
        if stands_for is None or store.find_by_path(stands_for) is None:
            return f"not a registered caobunga project: {repo}"
    if not any((repo / name).is_file() for name in JUSTFILE_NAMES):
        return f"no justfile at {repo} — nothing to run"
    if shutil.which("just") is None:
        return JUST_MISSING
    return None


def _missing_recipe(recipe: str, exit_code: int | None, output: str) -> str | None:
    """`MISSING_RECIPE` naming this verb's own recipe → the refusal text; else None."""
    if not exit_code:
        return None
    match = MISSING_RECIPE.search(output)
    if match is None or match.group(1) != recipe:
        return None
    return f"no `{recipe}` recipe in justfile — nothing to run"


@dataclass(frozen=True)
class FitnessResult:
    """Whether a repo's justfile could ever answer the fleet suite (CBN-215).

    Structural, not a run: never invokes `just test` / `lint` / `typecheck`
    (their `TIMEOUTS` ceilings are 600/120/300s, wrong for a synchronous
    `register` / `observe` call), only `just --summary`. Report-only — never
    raised as a refusal.
    """

    fit: bool
    refused: str | None = None
    missing: tuple[str, ...] = ()
    present: tuple[str, ...] = ()

    def render(self) -> str:
        if self.refused is not None:
            return f"suite-fitness: refused — {self.refused}"
        if self.missing:
            has = ", ".join(self.present) if self.present else "none"
            missing = ", ".join(self.missing)
            return f"suite-fitness: unreachable — missing recipe(s): {missing} (has: {has})"
        return "suite-fitness: fit — test/lint/typecheck recipes present"


def probe_fitness(repo: Path, *, run: RunFn | None = None) -> FitnessResult:
    """Report, never refuse: could this repo's justfile ever fold `suite: green`?

    Matches recipe names verbatim against `FLEET_VERBS` — no `check:` →
    `typecheck` alias (CBN-211's `CORE-12` case stays `unreachable`, not silently
    satisfied).
    """
    if not any((repo / name).is_file() for name in JUSTFILE_NAMES):
        return FitnessResult(fit=False, refused=f"no justfile at {repo}")
    if shutil.which("just") is None:
        return FitnessResult(fit=False, refused=JUST_MISSING)
    try:
        code, output = (run or run_detached)(("just", "--summary"), repo, SUMMARY_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return FitnessResult(fit=False, refused=f"`just --summary` failed: {exc}")
    if code != 0:
        detail = next((line for line in output.splitlines() if line.strip()), "no output")
        return FitnessResult(fit=False, refused=f"`just --summary` exited {code}: {detail.strip()}")
    recipes = set(output.split())
    missing = tuple(verb for verb in FLEET_VERBS if verb not in recipes)
    present = tuple(verb for verb in FLEET_VERBS if verb in recipes)
    return FitnessResult(fit=not missing, missing=missing, present=present)


def _porcelain(repo: Path) -> tuple[tuple[str, ...], bool]:
    """`git_dirt`'s `list[str] | None` reshaped to this module's `(lines, unknown)` pair.

    Shares `git_dirt`'s subprocess call, timeout, and `OSError` policy (CBN-238)
    rather than keeping a second `git status --porcelain -uall` read — the two
    porcelain reads (the dispatcher's safety gate, this before/after diff) must
    agree on how long a hung `git` gets before degrading to unknown.
    """
    lines = git_dirt(repo)
    if lines is None:
        return (), True
    return tuple(lines), False


def tail_output(output: str) -> tuple[str, bool]:
    """The last `TAIL_CHARS` of `output`, and whether the head was dropped.

    Shared with `bootstrap.run_bootstrap`, so its log clips the same way.
    """
    if len(output) <= TAIL_CHARS:
        return output, False
    return output[-TAIL_CHARS:], True
