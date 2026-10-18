"""Loop-side deterministic suite: the three fleet verbs on every close (CBN-EPIC-202.3).

Composes `suite_verbs.run_verb` over `VERBS` in fixed order under `TIMEOUTS`
and folds the three results into the run record's `suite` field
(`models.Suite`, CBN-EPIC-202.2). Until this module the only suite result on
any surface was reviewer prose — two closes on 2026-09-12 (CBN-167, CBN-171)
were green on the worker's targeted run and red on the full suite, and the
reviewer that caught them was a sampled, advisory LLM. This runs the same
`just test` / `just lint` / `just typecheck` Fleet HQ's `STACK-TENDENCIES.md`
§"Command interface" makes every fleet repo answer, deterministically, and
writes the fold where a dispatch decision can read it (CBN-EPIC-202.4).

Owns *composition* only. What one verb is and how it is bounded — the fixed
argv, the registry check, porcelain before and after, the detached process
group and its kill on timeout — stays `suite_verbs.run_verb`'s (CBN-174,
CBN-185), reached by call rather than by copy so the loop's run and the
reviewer's re-run cannot disagree on what a timeout or a refusal is.

**Never raises.** `run_verb` already returns a refusal or a timeout as a
result; anything that still escapes — a spawn error, a bug — becomes a
`refused` entry for that verb, so the close it measures is recorded either
way (CBN-171's posture for loop-side steps). The record's `verdict` is
`Suite.fold`'s, never decided here.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from caobunga.models import RunRecord, Suite, SuiteVerb, SuiteVerdict
from caobunga.store import Store
from caobunga.suite_verbs import TIMEOUTS, VERBS, GuardFn, RunFn, SuiteResult, run_verb
from caobunga.text import clip, strip_ansi

# A script runner's command echo — npm/pnpm `> tsc --noEmit`, or a just recipe's
# `<U+203A> frontend: tsc`. It trails a clean lint/typecheck, so as `detail` it
# names the command rather than the result (CBN-296).
_SCRIPT_ECHO = re.compile(r"[>\u203a] \S")
# just's trailing `error: Recipe `test` failed on line 9 with exit code 1` — it
# names the recipe, not what failed, and buried the runner's own summary (CBN-311).
_RECIPE_ERROR = re.compile(r"error: recipe .* failed .*exit code \d+", re.IGNORECASE)
# swift-testing's trailer on a package whose tests are all XCTest — `<U+2714> Test run
# with 0 tests in 0 suites passed` — prints after XCTest's `Executed 318 tests`, behind a
# few `<U+25C7> Test run started.` / `<U+21B3> ...` lines, so as `detail` it hid the real
# count (CBN-354). A trailer with a non-zero count is a result and stays the detail.
_EMPTY_TEST_RUN = re.compile(r"\bTest run with 0 tests\b")
_XCTEST_COUNT = re.compile(r"\bExecuted \d+ tests?\b")


@dataclass(frozen=True)
class SuiteRun:
    """What one close's suite produced: the record field and the log body.

    `log` is the full rendered output of every verb — the text the reviewer
    used to get from a `code_suite` call — for `Store.save_suite`; the record
    carries only the fold and one clipped `detail` line per verb, since the
    deny-list forbids raw CLI stdout on every report surface.
    """

    suite: Suite
    log: str


def run_suite(
    repo: Path,
    store: Store,
    *,
    run: RunFn | None = None,
    on_wait: Callable[[], None] | None = None,
) -> SuiteRun:
    """Run every fleet verb in `VERBS` order against `repo`; refusals are results.

    Each verb's spawn runs under `Store.suite_lock` (CBN-308, narrowed to the
    spawn at CBN-310): a concurrent slug's verb waits there, outside every
    verb's measured duration and ceiling, and a refusal — decided before the
    spawn — never queues. `on_wait` is called when a verb must block. A home
    that cannot take the lock (an `flock`-less mount) runs the suite
    unserialized rather than raise — measuring the close outranks keeping it
    off a busy host.
    """

    def guard() -> contextlib.AbstractContextManager[None]:
        return _spawn_lock(store, on_wait)

    results = [_measure(verb, repo, store, run, guard) for verb in VERBS]
    verbs = [_to_verb(result) for result in results]
    suite = Suite.from_verbs(verbs)
    log = "\n\n".join([f"verdict: {suite.verdict.value}", *(r.render() for r in results)]) + "\n"
    return SuiteRun(suite=suite, log=log)


def echo_line(suite: Suite) -> str:
    """`suite: green — test 0 98.4s · lint 0 3.1s · typecheck 0 6.6s`.

    The one loop echo for this step, shaped like `foreign:`: the record
    already carries every fact, this is the operator's glance at the drain.
    """
    parts = (f"{verb.verb} {_exit_word(verb)} {verb.duration:.1f}s" for verb in suite.verbs)
    return f"suite: {suite.verdict.value} — {' · '.join(parts)}"


def suite_stops(run: RunRecord) -> bool:
    """A closure whose deterministic suite is not `green` (CBN-EPIC-202.4).

    `red`, `timeout`, and `refused` all stop: a drain that cannot measure its
    suite is the unattended state the gate exists for. `suite` is `None` only
    on a non-closure outcome (`RunRecord._suite_iff_closure` requires it at
    `schema_version` 2 on every closure), and the outcome rule stops
    those.
    """
    return run.suite is not None and run.suite.verdict is not SuiteVerdict.GREEN


@contextlib.contextmanager
def _spawn_lock(store: Store, on_wait: Callable[[], None] | None) -> Iterator[None]:
    with contextlib.ExitStack() as stack:
        with contextlib.suppress(OSError):
            stack.enter_context(store.suite_lock(on_wait))
        yield


def _measure(verb: str, repo: Path, store: Store, run: RunFn | None, guard: GuardFn) -> SuiteResult:
    try:
        return run_verb(verb, str(repo), store=store, run=run, timeout=TIMEOUTS[verb], guard=guard)
    except Exception as exc:  # noqa: BLE001 — the contract is "never raises"
        return SuiteResult(
            verb, VERBS[verb], repo, None, 0.0, "", refused=f"{type(exc).__name__}: {exc}"
        )


def _to_verb(result: SuiteResult) -> SuiteVerb:
    # The fleet verb (`test`), not the tool name (`run_tests`): the last argv
    # token of a fixed `("just", <verb>)` — derived, so there is no second
    # table beside `VERBS` to drift from it.
    return SuiteVerb(
        verb=result.argv[-1],
        exit_code=result.exit_code,
        duration=result.duration,
        timed_out=result.timed_out,
        refused=result.refused,
        tree_changed=result.tree_changed,
        detail=_last_line(result.output),
    )


def _last_line(output: str) -> str | None:
    """The last non-blank, non-echo output line, clipped — pytest's and ruff's summaries trail.

    An empty swift-testing trailer yields to XCTest's `Executed N tests` line above it,
    and stands only when there is none.
    """
    empty_run: str | None = None
    for line in reversed(output.splitlines()):
        # CSI escapes (colour, erase): vitest's summary line arrived on a run record
        # wrapped in `\x1b[2m…\x1b[22m` (CBN-276). Stripped before the blank check, so
        # a line that is only escapes counts as blank.
        plain = strip_ansi(line)
        if not plain.strip() or _SCRIPT_ECHO.match(plain) or _RECIPE_ERROR.match(plain):
            continue
        if empty_run is None:
            if not _EMPTY_TEST_RUN.search(plain):
                return clip(plain)
            empty_run = clip(plain)
        elif _XCTEST_COUNT.search(plain):
            return clip(plain)
    return empty_run


def _exit_word(verb: SuiteVerb) -> str:
    if verb.exit_code is not None:
        return str(verb.exit_code)
    return "timeout" if verb.timed_out else "refused"
