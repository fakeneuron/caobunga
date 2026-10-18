"""docs/CONTRACT.md enumerations vs. the code they restate.

CBN-EPIC-11.4 / CBN-101 / CBN-107 / CBN-EPIC-13.2.

CONTRACT's provider tokens, PATH binaries, execution tiers, task-class pin
cells, CLI exit codes, run-record field table, and the outcome / escalation /
Hermes-review / Hermes-verdict-code enum tables are prose copies of
`usage.V1_PROVIDERS`, `usage.BINARIES`, `models.ExecutionTier`,
`taskmap.MODELS`, `errors.*` exit codes, and `models.RunRecord` /
`Outcome` / `Escalation` / `HermesReview` / `HermesVerdictCode`.
These tests make the next rename fail the suite instead of silently rotting
the doc. One guard (CBN-EPIC-13.2) reaches across the flaitron submodule
boundary instead of at a Python symbol: the parse-contract SPEC version
pin. Doc parsing lives here — the package never grows a CONTRACT reader.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
import yaml

from caobunga import errors, loop
from caobunga.cli import run as run_command
from caobunga.flaitron import (
    FT_TASK_SKILL_DIR_REL,
    UNATTENDED_MODULE,
    NextTask,
    OpenTasknote,
    PlanPriority,
    external_review_box,
    phase_progress,
    tasknote,
)
from caobunga.local import CLAUDE_GUARD_SETTINGS, RECIPES, build_argv
from caobunga.models import (
    Escalation,
    ExecutionTier,
    ExternalReview,
    HermesReview,
    HermesVerdictCode,
    Outcome,
    RunRecord,
    SuiteVerb,
    SuiteVerdict,
    utc_now,
)
from caobunga.prompt import worker_message
from caobunga.store import Store
from caobunga.taskmap import EFFORT, MODELS, TaskClass
from caobunga.usage import BINARIES, V1_PROVIDERS

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs" / "CONTRACT.md"
CONTRACT = CONTRACT_PATH.read_text(encoding="utf-8")
CONTRACT_LINES = CONTRACT.splitlines()

# CONTRACT §"Flaitron reader" pins the task-line grammar to a flaitron SPEC
# version. The submodule is the source of truth for what is actually vendored.
SPEC_PATH = Path(__file__).resolve().parents[2] / ".flaitron" / "core" / "SPEC.md"

# CONTRACT §"Fleet boundary" records the tasknote-template headings
# `phase_progress()` and `external_review_box()` read (CBN-235). The vendored templates
# are the source of truth for what those readers actually see.
TEMPLATE_DIR = SPEC_PATH.parent / "templates"
FULL_TEMPLATE_PATH = TEMPLATE_DIR / "tasknote-template.md"
MICRO_TEMPLATE_PATH = TEMPLATE_DIR / "tasknote-micro-template.md"

# The public export ships without `.flaitron/` (CBN-367), so the two guards
# below that read the vendored submodule skip there (CBN-369). Keyed on
# `.flaitron/`, not on `.flaitron/core`: in the private repo an uninitialized
# submodule still fails.
needs_flaitron = pytest.mark.skipif(
    not SPEC_PATH.parents[1].is_dir(), reason="public export: no .flaitron/ (CBN-369)"
)

EXPECTED_PROVIDERS = [provider.value for provider in V1_PROVIDERS]
EXPECTED_BINARIES = [BINARIES[provider][0] for provider in V1_PROVIDERS]
EXPECTED_TIERS = [tier.value for tier in ExecutionTier]
EXPECTED_PIN_ROWS = [
    (
        task_class.value,
        [MODELS[provider][task_class] for provider in V1_PROVIDERS],
    )
    for task_class in TaskClass
]
EXPECTED_EFFORT_ROWS = [
    (task_class.value, [table[task_class] for table in EFFORT.values()]) for task_class in TaskClass
]
EXPECTED_RUN_RECORD_FIELDS = list(RunRecord.model_fields)
EXPECTED_SUITE_VERB_FIELDS = list(SuiteVerb.model_fields)

# CONTRACT embeds rollup / digest / card body-shape templates in fenced blocks
# that carry their own `#` headings, so a generic section splitter would
# mis-slice the file. Every extraction below is anchored to its own line or
# table header instead.
_BACKTICKED = re.compile(r"`([^`]+)`")


def _sole_match(pattern: str, label: str) -> str:
    """The one capture for an anchored pattern — a drifted anchor fails loudly."""
    matches = re.findall(pattern, CONTRACT, re.MULTILINE)
    assert len(matches) == 1, (
        f"{CONTRACT_PATH.name}: expected exactly one {label}, found {len(matches)}. "
        "The anchor drifted — re-anchor the pattern in this test."
    )
    return matches[0]


def _table_body_start(header: str, label: str) -> int:
    """Line index of the first body row under ``header`` (after the separator)."""
    rows = [i for i, line in enumerate(CONTRACT_LINES) if line.strip() == header]
    assert len(rows) == 1, (
        f"{CONTRACT_PATH.name}: expected exactly one {label} header row "
        f"{header!r}, found {len(rows)}. The anchor drifted."
    )
    separator = CONTRACT_LINES[rows[0] + 1]
    assert set(separator) <= set("|- "), (
        f"{CONTRACT_PATH.name}: {label} header is not followed by a separator row "
        f"(got {separator!r})"
    )
    return rows[0] + 2


def _table_first_column(header: str, label: str) -> list[str]:
    """Backticked first-column values of the markdown table under ``header``."""
    values = []
    for line in CONTRACT_LINES[_table_body_start(header, label) :]:
        if not line.startswith("|"):
            break
        cell = _BACKTICKED.findall(line.split("|")[1])
        assert len(cell) == 1, (
            f"{CONTRACT_PATH.name}: {label} row has no single backticked first column: {line!r}"
        )
        values.append(cell[0])
    assert values, f"{CONTRACT_PATH.name}: {label} has no rows"
    return values


def _pin_table_body(header: str, label: str) -> list[tuple[str, list[str]]]:
    """``(class, [model, …])`` rows from the task-class pin table under ``header``.

    First column is plain class text; remaining cells are backticked model ids
    in header / ``V1_PROVIDERS`` order.
    """
    rows: list[tuple[str, list[str]]] = []
    for line in CONTRACT_LINES[_table_body_start(header, label) :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) >= 2, f"{CONTRACT_PATH.name}: {label} row has too few cells: {line!r}"
        class_name = cells[0]
        assert class_name and "`" not in class_name, (
            f"{CONTRACT_PATH.name}: {label} row class cell should be plain text: {line!r}"
        )
        models = []
        for cell in cells[1:]:
            found = _BACKTICKED.findall(cell)
            assert len(found) == 1, (
                f"{CONTRACT_PATH.name}: {label} model cell needs one backtick pair: {cell!r}"
            )
            models.append(found[0])
        rows.append((class_name, models))
    assert rows, f"{CONTRACT_PATH.name}: {label} has no rows"
    return rows


def _argv_table_rows(header: str, label: str) -> dict[str, str]:
    """``{provider: argv}`` from the local-adapter argv table under ``header``.

    Three backticked columns (provider, binary, argv); the binary is already covered
    by the `usage.BINARIES` guard, so only the argv travels back.
    """
    rows: dict[str, str] = {}
    for line in CONTRACT_LINES[_table_body_start(header, label) :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == 3, (
            f"{CONTRACT_PATH.name}: {label} row should have three cells: {line!r}"
        )
        provider, _binary, argv = (_BACKTICKED.findall(cell) for cell in cells)
        assert len(provider) == 1 and len(argv) == 1, (
            f"{CONTRACT_PATH.name}: {label} provider and argv cells each need one "
            f"backtick pair: {line!r}"
        )
        rows[provider[0]] = argv[0]
    assert rows, f"{CONTRACT_PATH.name}: {label} has no rows"
    return rows


# Every CONTRACT site that enumerates the *full* v1 provider set. The
# §Worker interface `RECIPES` table is deliberately a narrower subset
# (CONTRACT §Worker interface → `local`) and is out of scope here.
PROVIDER_SITES = (
    ("Usage adapters canonical list", r"v1 providers \(CAO tokens, stable order\):([^.]+)\."),
    ("Picker order restatement", r"otherwise `V1_PROVIDERS` order \(([^)]*)\)"),
    ("Task-class model-map table header", r"^\| class \|(.+)\|$"),
)

BINARY_SITE = (
    "Usage adapters binaries on PATH",
    r"Binaries on PATH:([^.]+)\.",
)

PIN_TABLE_HEADER = (
    "| class | `grok_cli` | `claude_code` | `codex` | `cursor_cli` | `opencode_cli` |"
)

# Deliberately "task class", not "class": the pin-table header anchor above is
# `^\| class \|(.+)\|$`, and a second `| class | …` table would drift it (CBN-EPIC-15.3).
# Columns come from `EFFORT`'s own keys, so a reordered or added provider row fails too.
EFFORT_TABLE_HEADER = "| task class | " + " | ".join(f"`{p}`" for p in EFFORT) + " |"

LOCAL_ARGV_TABLE_HEADER = "| Provider | Binary | Argv |"

# The doc's argv cells are `build_argv` output with placeholders standing in for the
# runtime values, minus the leading binary (the table carries that in its own column).
# Deriving them rather than restating them is why this guard covers the whole row and
# not only the effort fragment it was filed for (CBN-EPIC-15.4).
ARGV_PLACEHOLDERS = {"model": "<model>", "message": "<prompt>", "effort": "<effort>"}

TIER_TABLES = (
    ("Execution tiers table", "| Value | Meaning | v1 |"),
    ("Run loop tier table", "| Tier | Behavior |"),
)

RUN_RECORD_FIELD_TABLE_HEADER = "| Field | Type | Required | Source | Why |"
SUITE_VERB_FIELD_TABLE_HEADER = "| Verb field | Type | Required | Why |"
# CONTRACT §"Run record" → §Example is the one place the nested `suite` shape
# is shown in full (CBN-EPIC-202.2). Anchored on the run-record example's own
# first three keys — the rollup and digest sections carry `### Example`
# headings of their own.
RUN_RECORD_EXAMPLE_SITE = (
    "run record example",
    r"(?s)### Example\n\n```yaml\n"
    r"(schema_version: \d+\nproject: caobunga\ntask_id: CBN-003\n.*?)```",
)

# CONTRACT §"Run loop" restates the exact prompt `prompt.worker_message` sends a
# fresh worker, with `{id}` standing in for the task id (CBN-EPIC-14.3). The
# posture flag on that line is the whole point of the epic — a silent rot here
# is what let CBN-013's superseded `--fast` claim survive the code it described.
WORKER_PROMPT_SITE = (
    "Run loop worker prompt",
    r"^Worker prompt: `([^`]+)` when there is no open in-progress note",
)

# The second documented prompt shape: what a target whose pin does not recognize
# `--unattended` is sent instead (CBN-108). Guarded alongside the first so the
# degrade path cannot rot the way CBN-013's superseded `--fast` claim did.
DEGRADED_PROMPT_SITE = (
    "Run loop degraded worker prompt",
    r"^Degraded prompt: `([^`]+)` when the target's pinned `/ft-task` does not recognize",
)

# The two continue-prompt shapes (CBN-109). Fenced rather than inline-backticked
# because the prompt itself contains backticks — the single-backtick capture the
# two sites above use cannot express it. `[\s\S]*?` crosses lines without
# needing re.DOTALL, so `_sole_match`'s MULTILINE-only compile is untouched.
CONTINUE_PROMPT_SITE = (
    "Run loop continue prompt",
    r"^Continue prompt:\n\n```text\n([\s\S]*?)\n```",
)
DEGRADED_CONTINUE_PROMPT_SITE = (
    "Run loop degraded continue prompt",
    r"^Degraded continue prompt:\n\n```text\n([\s\S]*?)\n```",
)

# The pinned-skill cue (CBN-291), documented once as a fenced block with a
# `{skill}` placeholder — fresh-launch shapes only, so it composes into the two
# fresh-prompt asserts and not the continue pair.
PINNED_SKILL_SITE = (
    "Run loop pinned-skill cue",
    r"^\*\*Pinned-skill cue \(CBN-291\)\.\*\*[\s\S]*?```text\n([\s\S]*?)\n```",
)

# The foreground-only cue (CBN-199), documented once as a fenced single block
# — it is static and provider-neutral, so unlike the follow-up cue it has no
# degraded twin to pin alongside it.
FOREGROUND_ONLY_SITE = (
    "Run loop foreground-only cue",
    r"^\*\*Foreground-only cue \(CBN-199\)\.\*\*[\s\S]*?```text\n([\s\S]*?)\n```",
)

# The park-check cue (CBN-327), documented once as a fenced single block — static
# like the foreground-only cue, so it has no degraded twin either.
PARK_CHECK_SITE = (
    "Run loop park-check cue",
    r"^\*\*Park-check cue \(CBN-327\)\.\*\*[\s\S]*?```text\n([\s\S]*?)\n```",
)

# The follow-up cue (CBN-138.2), documented once as a fenced pair rather than
# folded into all four prompt shapes. Fenced for the same reason as the continue
# prompts — the cue itself contains backticks. Documenting it once keeps 4 + 2
# strings pinned instead of the 8 full prompts an enumeration would need, and
# the four composition asserts below are what tie the pair back to the code.
FOLLOWUP_CUE_SITE = (
    "Run loop follow-up cue",
    r"^Unprobed since the flaitron v7 hard cut[\s\S]*?```text\n([\s\S]*?)\n```",
)

SPEC_PIN_SITE = (
    "Flaitron reader SPEC version pin",
    r"\*\*pinned at (v[\d][^*]*)\*\* via the `\.flaitron/core` submodule",
)
SPEC_VERSION_SITE = (
    "SPEC.md version declaration",
    r"^\*\*Version:\*\* (v[\d][^\s]*)\s*$",
)

# CONTRACT's run-record enum tables (§Outcomes / §"Escalation reasons" /
# §"Hermes review"), each a single-backticked-first-column table like
# TIER_TABLES, paired with the model.py StrEnum it restates.
ENUM_TABLES = (
    ("Outcomes table", "| Value | When |", [outcome.value for outcome in Outcome]),
    ("Escalation reasons table", "| Reason | When |", [reason.value for reason in Escalation]),
    ("Hermes review table", "| Review | When |", [review.value for review in HermesReview]),
    (
        "Hermes verdict codes table",
        "| Verdict code | Meaning |",
        [code.value for code in HermesVerdictCode],
    ),
    ("Suite verdict table", "| Verdict | When |", [verdict.value for verdict in SuiteVerdict]),
    (
        "External review table",
        "| External review | When |",
        [review.value for review in ExternalReview],
    ),
)

# `0` (success) and `1` (unexpected) are not carried by any exception class,
# so only the error codes are comparable to `errors.*`.
NON_ERROR_EXIT_CODES = {0, 1}


def _error_instances() -> list[errors.UserError]:
    record = RunRecord(
        project="demo",
        task_id="CBN-001",
        outcome=Outcome.FAILED,
        closed_at=utc_now(),
        summary="fixture",
    )
    return [
        errors.UserError("boom"),
        errors.NoTaskError(),
        errors.UnknownProjectError("demo"),
        errors.Unlaunchable("boom"),
        errors.GitUnreadable(Path("demo")),
        errors.HeadLost("boom"),
        errors.WorkerError("boom"),
        errors.ReportError("boom", record),
    ]


def _user_error_classes() -> set[type[errors.UserError]]:
    found = {errors.UserError}
    stack = [errors.UserError]
    while stack:
        for subclass in stack.pop().__subclasses__():
            if subclass not in found:
                found.add(subclass)
                stack.append(subclass)
    return found


@pytest.mark.parametrize(("label", "pattern"), PROVIDER_SITES, ids=[s[0] for s in PROVIDER_SITES])
def test_contract_provider_tokens_match_v1_providers(label: str, pattern: str) -> None:
    tokens = _BACKTICKED.findall(_sole_match(pattern, label))
    assert tokens == EXPECTED_PROVIDERS, (
        f"{CONTRACT_PATH.name} {label} lists {tokens}, "
        f"but usage.V1_PROVIDERS is {EXPECTED_PROVIDERS}"
    )


def test_contract_binaries_match_usage_binaries() -> None:
    label, pattern = BINARY_SITE
    binaries = _BACKTICKED.findall(_sole_match(pattern, label))
    assert binaries == EXPECTED_BINARIES, (
        f"{CONTRACT_PATH.name} {label} lists {binaries}, "
        f"but usage.BINARIES primaries (V1_PROVIDERS order) are {EXPECTED_BINARIES}"
    )


@pytest.mark.parametrize(("label", "header"), TIER_TABLES, ids=[t[0] for t in TIER_TABLES])
def test_contract_tier_names_match_execution_tier(label: str, header: str) -> None:
    tiers = _table_first_column(header, label)
    assert tiers == EXPECTED_TIERS, (
        f"{CONTRACT_PATH.name} {label} lists {tiers}, but models.ExecutionTier is {EXPECTED_TIERS}"
    )


def test_contract_pin_table_body_match_taskmap_models() -> None:
    label = "Task-class model-map pin table body"
    rows = _pin_table_body(PIN_TABLE_HEADER, label)
    assert rows == EXPECTED_PIN_ROWS, (
        f"{CONTRACT_PATH.name} {label} lists {rows}, "
        f"but taskmap.MODELS (TaskClass × V1_PROVIDERS) is {EXPECTED_PIN_ROWS}"  # noqa: RUF001
    )


def test_contract_effort_table_matches_taskmap_effort() -> None:
    """CBN-EPIC-15.3, mirroring the pin-table guard: a rung bump fails here, not in prose.

    Per-provider since CBN-332.
    """
    label = "Task-class effort table"
    rows = _pin_table_body(EFFORT_TABLE_HEADER, label)
    assert rows == EXPECTED_EFFORT_ROWS, (
        f"{CONTRACT_PATH.name} {label} lists {rows}, but taskmap.EFFORT is {EXPECTED_EFFORT_ROWS}"
    )


def test_contract_local_argv_table_matches_build_argv() -> None:
    """CONTRACT §"Worker interface → `local`" argv rows vs. what `build_argv` emits.

    The table rotted once already: CBN-EPIC-15.3 started routing effort and the doc
    kept showing the pre-effort argv for a full task cycle, because the provider-set
    guards deliberately skip this table (see PROVIDER_SITES) and nothing else read it.
    Comparing against `build_argv` output pins flag order and the effort fragment
    together, so a renamed flag or a new recipe row fails here instead of in prose.
    """
    label = "Worker interface local argv table"
    documented = _argv_table_rows(LOCAL_ARGV_TABLE_HEADER, label)
    # The guard payload embeds this machine's interpreter path (CBN-212), so the
    # table shows a placeholder for it, as it does for the routed values.
    expected = {
        provider: " ".join(build_argv("", provider, **ARGV_PLACEHOLDERS)[1:]).replace(
            CLAUDE_GUARD_SETTINGS, "<guard-settings>"
        )
        for provider in RECIPES
    }
    assert documented == expected, (
        f"{CONTRACT_PATH.name} {label} documents {documented}, "
        f"but local.build_argv emits {expected}"
    )


def test_contract_run_record_fields_match_model() -> None:
    label = "Run record field table"
    fields = _table_first_column(RUN_RECORD_FIELD_TABLE_HEADER, label)
    assert fields == EXPECTED_RUN_RECORD_FIELDS, (
        f"{CONTRACT_PATH.name} {label} lists {fields}, "
        f"but RunRecord.model_fields is {EXPECTED_RUN_RECORD_FIELDS}"
    )


def test_contract_suite_verb_fields_match_model() -> None:
    """CONTRACT §"Suite" per-verb field table vs. `models.SuiteVerb` (CBN-EPIC-202.2)."""
    label = "Suite verb field table"
    fields = _table_first_column(SUITE_VERB_FIELD_TABLE_HEADER, label)
    assert fields == EXPECTED_SUITE_VERB_FIELDS, (
        f"{CONTRACT_PATH.name} {label} lists {fields}, "
        f"but SuiteVerb.model_fields is {EXPECTED_SUITE_VERB_FIELDS}"
    )


def test_contract_run_record_example_validates() -> None:
    """CONTRACT §"Run record" → §Example loads as a `RunRecord` at the current version.

    Until CBN-EPIC-202.2 the example was unchecked prose. It is the one place
    the nested `suite` shape is shown in full, so a drifted key there would
    teach the wrong shape to whoever reads the contract before the code.
    """
    label, pattern = RUN_RECORD_EXAMPLE_SITE
    record = RunRecord.model_validate(yaml.safe_load(_sole_match(pattern, label)))
    assert record.schema_version == RunRecord.model_fields["schema_version"].default, (
        f"{CONTRACT_PATH.name} {label} carries schema_version {record.schema_version}, "
        "not the model default"
    )
    assert record.suite is not None
    assert record.suite.verdict is SuiteVerdict.GREEN
    assert [verb.verb for verb in record.suite.verbs] == ["test", "lint", "typecheck"]


@pytest.mark.parametrize(
    ("label", "header", "expected"), ENUM_TABLES, ids=[t[0] for t in ENUM_TABLES]
)
def test_contract_run_record_enum_tables_match_models(
    label: str, header: str, expected: list[str]
) -> None:
    values = _table_first_column(header, label)
    assert values == expected, (
        f"{CONTRACT_PATH.name} {label} lists {values}, but code is {expected}"
    )


DRAIN_TABLE_HEADER = "| Reason | Emitted when | Detail |"
# The reasons are call-site literals — `_echo_drain_exit("<reason>", …)` and the
# `(reason, detail)` tuples `_no_candidate_exit` returns — not an enum: a
# constant only this guard read would be surface for the guard's sake.
_DRAIN_REASON_CALL = re.compile(r'_echo_drain_exit\("(\w+)"')
_DRAIN_REASON_RETURN = re.compile(r'return \(?\s*"(\w+)",')
_COUNT_WORDS = {"three": 3, "four": 4, "five": 5}


def test_contract_drain_reasons_match_loop_exits() -> None:
    """CONTRACT §"Run loop" `drain:` table vs. every reason `loop.py` emits (CBN-EPIC-202.4).

    CBN-134's retirement of `stem-change` showed the tier-table guards earning
    their keep; the drain table had none, so a fifth reason — or a dropped one —
    would leave the closed set open on one side.
    """
    label = "drain reason table"
    documented = _table_first_column(DRAIN_TABLE_HEADER, label)
    source = Path(loop.__file__).read_text(encoding="utf-8")
    emitted = set(_DRAIN_REASON_CALL.findall(source)) | set(_DRAIN_REASON_RETURN.findall(source))
    assert set(documented) == emitted, (
        f"{CONTRACT_PATH.name} {label} lists {documented}, but loop.py emits {sorted(emitted)}"
    )
    count = _sole_match(r"from a closed set of (\w+) reasons:", "drain closed-set sentence")
    assert _COUNT_WORDS[count] == len(documented), (
        f"{CONTRACT_PATH.name} says a closed set of {count} drain reasons, "
        f"but its {label} has {len(documented)} rows"
    )


def test_contract_exit_codes_match_error_classes() -> None:
    label = "Exit codes table"
    documented = {int(code) for code in _table_first_column("| Code | Meaning |", label)}
    raised = {error.exit_code for error in _error_instances()}
    assert documented - NON_ERROR_EXIT_CODES == raised, (
        f"{CONTRACT_PATH.name} {label} documents error codes "
        f"{sorted(documented - NON_ERROR_EXIT_CODES)}, but errors.* raise {sorted(raised)}"
    )
    assert NON_ERROR_EXIT_CODES <= documented, (
        f"{CONTRACT_PATH.name} {label} no longer documents "
        f"{sorted(NON_ERROR_EXIT_CODES - documented)}"
    )


def test_every_user_error_subclass_is_covered() -> None:
    covered = {type(error) for error in _error_instances()}
    missing = _user_error_classes() - covered
    assert not missing, (
        f"errors.py gained {sorted(cls.__name__ for cls in missing)} — add it to "
        "_error_instances() and document its exit code in docs/CONTRACT.md"
    )


def test_contract_worker_prompt_matches_loop() -> None:
    """CONTRACT's §"Run loop" worker prompt vs. what `worker_message` emits.

    The doc writes the task id as the literal `{id}` placeholder; the code
    interpolates a real one, so the comparison substitutes the fixture id back
    out. A flag change on either side fails here (CBN-EPIC-14.3).
    """
    label, pattern = WORKER_PROMPT_SITE
    documented = _sole_match(pattern, label)

    pinned_label, pinned_pattern = PINNED_SKILL_SITE
    pinned = _sole_match(pinned_pattern, pinned_label)
    foreground_label, foreground_pattern = FOREGROUND_ONLY_SITE
    foreground = _sole_match(foreground_pattern, foreground_label)
    park_label, park_pattern = PARK_CHECK_SITE
    park = _sole_match(park_pattern, park_label)
    cue_label, cue_pattern = FOLLOWUP_CUE_SITE
    cue = _sole_match(cue_pattern, cue_label)
    candidate = NextTask(task_id="CBN-001", priority=PlanPriority.HIGH, reason="spec-selection")
    emitted = worker_message(
        candidate,
        unattended=True,
        unattended_module=FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE,
        pinned_skill=FT_TASK_SKILL_DIR_REL / "SKILL.md",
    )
    skill = (FT_TASK_SKILL_DIR_REL / "SKILL.md").as_posix()
    emitted = emitted.replace(skill, "{skill}").replace(candidate.task_id, "{id}")

    assert f"{documented} {pinned} {foreground} {park} {cue}" == emitted, (
        f'{CONTRACT_PATH.name} §"Run loop" documents the worker prompt as '
        f"{documented!r} plus the pinned-skill cue {pinned!r} plus the foreground-only "
        f"cue {foreground!r} plus the park-check cue {park!r} plus the cue {cue!r}, "
        f"but prompt.worker_message emits {emitted!r}"
    )


def test_contract_degraded_worker_prompt_matches_loop() -> None:
    """CONTRACT's degraded prompt vs. what `worker_message` emits for a stale pin.

    Same guard as above, on the CBN-108 fallback. Both prompt shapes are
    documented, so both are pinned to the code — a flag change on either path
    fails here.
    """
    label, pattern = DEGRADED_PROMPT_SITE
    documented = _sole_match(pattern, label)

    pinned_label, pinned_pattern = PINNED_SKILL_SITE
    pinned = _sole_match(pinned_pattern, pinned_label)
    foreground_label, foreground_pattern = FOREGROUND_ONLY_SITE
    foreground = _sole_match(foreground_pattern, foreground_label)
    park_label, park_pattern = PARK_CHECK_SITE
    park = _sole_match(park_pattern, park_label)
    cue_label, cue_pattern = FOLLOWUP_CUE_SITE
    cue = _sole_match(cue_pattern, cue_label)
    candidate = NextTask(task_id="CBN-001", priority=PlanPriority.HIGH, reason="spec-selection")
    emitted = worker_message(
        candidate,
        unattended=False,
        unattended_module=FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE,
        pinned_skill=FT_TASK_SKILL_DIR_REL / "SKILL.md",
    )
    skill = (FT_TASK_SKILL_DIR_REL / "SKILL.md").as_posix()
    emitted = emitted.replace(skill, "{skill}").replace(candidate.task_id, "{id}")

    assert f"{documented} {pinned} {foreground} {park} {cue}" == emitted, (
        f'{CONTRACT_PATH.name} §"Run loop" documents the degraded worker prompt as '
        f"{documented!r} plus the pinned-skill cue {pinned!r} plus the foreground-only "
        f"cue {foreground!r} plus the park-check cue {park!r} plus the cue "
        f"{cue!r}, but prompt.worker_message "
        f"emits {emitted!r}"
    )


def _in_progress_candidate() -> NextTask:
    """A candidate whose open note strands it at `in-progress` (CBN-109)."""
    return NextTask(
        task_id="CBN-001",
        priority=PlanPriority.HIGH,
        reason="in-progress-override",
        tasknote=OpenTasknote(
            task_id="CBN-001",
            path=".flaitron/tasknote/CBN-001.md",
            status="in-progress",
        ),
    )


def _placeholdered(candidate: NextTask, *, unattended: bool) -> str:
    """The emitted continue prompt with the doc's `{id}` / `{path}` / `{module}` put back.

    `{module}` is the probe-resolved `unattended-mode.md` path (CBN-189); the
    pinned adopter layout stands in for it here, the same value the fresh-launch
    guards above pass.
    """
    note = candidate.tasknote
    assert note is not None
    module = FT_TASK_SKILL_DIR_REL / UNATTENDED_MODULE
    return (
        worker_message(
            candidate,
            unattended=unattended,
            unattended_module=module,
            pinned_skill=FT_TASK_SKILL_DIR_REL / "SKILL.md",
        )
        .replace(module.as_posix(), "{module}")
        .replace(note.path, "{path}")
        .replace(candidate.task_id, "{id}")
    )


def test_contract_continue_prompt_matches_loop() -> None:
    """CONTRACT's §"Continue prompt" vs. what `worker_message` emits for a
    stranded `in-progress` note on a pin that honors `--unattended`.

    Same guard as the two fresh-worker prompts, extended to the branch
    CBN-EPIC-14.3 left bare — the third documented prompt shape, and the one
    reached only after a prior session already failed (CBN-109).
    """
    label, pattern = CONTINUE_PROMPT_SITE
    documented = _sole_match(pattern, label)
    foreground_label, foreground_pattern = FOREGROUND_ONLY_SITE
    foreground = _sole_match(foreground_pattern, foreground_label)
    park_label, park_pattern = PARK_CHECK_SITE
    park = _sole_match(park_pattern, park_label)
    cue_label, cue_pattern = FOLLOWUP_CUE_SITE
    cue = _sole_match(cue_pattern, cue_label)
    emitted = _placeholdered(_in_progress_candidate(), unattended=True)

    assert f"{documented} {foreground} {park} {cue}" == emitted, (
        f'{CONTRACT_PATH.name} §"Continue prompt" documents the continue prompt as '
        f"{documented!r} plus the foreground-only cue {foreground!r} plus the "
        f"park-check cue {park!r} plus the cue {cue!r}, but prompt.worker_message emits {emitted!r}"
    )


def test_contract_degraded_continue_prompt_matches_loop() -> None:
    """CONTRACT's degraded continue prompt vs. what `worker_message` emits.

    The fourth documented shape: a stranded note on a pin that does not parse
    `--unattended`, so the prose names `--fast` and points at no fragment.
    """
    label, pattern = DEGRADED_CONTINUE_PROMPT_SITE
    documented = _sole_match(pattern, label)
    foreground_label, foreground_pattern = FOREGROUND_ONLY_SITE
    foreground = _sole_match(foreground_pattern, foreground_label)
    park_label, park_pattern = PARK_CHECK_SITE
    park = _sole_match(park_pattern, park_label)
    cue_label, cue_pattern = FOLLOWUP_CUE_SITE
    cue = _sole_match(cue_pattern, cue_label)
    emitted = _placeholdered(_in_progress_candidate(), unattended=False)

    assert f"{documented} {foreground} {park} {cue}" == emitted, (
        f'{CONTRACT_PATH.name} §"Continue prompt" documents the degraded continue '
        f"prompt as {documented!r} plus the foreground-only cue {foreground!r} "
        f"plus the park-check cue {park!r} plus the cue {cue!r}, "
        f"but prompt.worker_message emits {emitted!r}"
    )


@needs_flaitron
def test_contract_flaitron_spec_pin_matches_submodule() -> None:
    """CONTRACT's parse-contract pin vs. the SPEC version actually vendored.

    Unlike the guards above, the source of truth is a file rather than a
    Python symbol — so this asserts across the submodule boundary. Failing
    here is the re-verify prompt CONTRACT §"Flaitron reader" describes: a
    flaitron bump must not move the pin before someone confirms TASK_LINE
    still covers §"Task-line format".

    Scope (CBN-111): both reads come from the *same* local checkout, so this
    only catches CONTRACT's pin drifting from what is actually vendored — not
    whether that checkout itself is behind flaitron's latest release. A
    never-bumped submodule keeps both reads in agreement and this test green
    forever. Cross-release currency belongs to Fleet HQ's alignment check, not this
    suite's — see CONTRACT §"Parse contract".
    """
    pin_label, pin_pattern = SPEC_PIN_SITE
    cited = _sole_match(pin_pattern, pin_label)

    version_label, version_pattern = SPEC_VERSION_SITE
    spec = SPEC_PATH.read_text(encoding="utf-8")
    declared = re.findall(version_pattern, spec, re.MULTILINE)
    assert len(declared) == 1, (
        f"{SPEC_PATH.name}: expected exactly one {version_label}, "
        f"found {len(declared)}. Re-anchor the pattern in this test."
    )

    assert cited == declared[0], (
        f'{CONTRACT_PATH.name} §"Flaitron reader" pins the task-line grammar at '
        f"{cited}, but {SPEC_PATH.name} declares {declared[0]}. Re-read "
        '§"Task-line format", confirm flaitron.TASK_LINE still covers it, '
        "then move the pin."
    )


def test_run_takes_exactly_one_required_project_argument() -> None:
    """CONTRACT §"Entry points": the batch form is `fleet`, never a widened `run`.

    CBN-124 declined a batch entry point; CBN-312 reopened it as its own verb,
    which spawns one child `caobunga run <slug>` per project. That only holds
    while `run` stays single-project, so pinning its parameter surface pins the
    split. Unlike its neighbours this guard reads a click command rather than a
    CONTRACT table — the claim is about a shape the doc asserts and no
    enumeration restates.
    """
    arguments = [param for param in run_command.params if isinstance(param, click.Argument)]
    options = [param for param in run_command.params if isinstance(param, click.Option)]

    assert [param.name for param in arguments] == ["project"], (
        "`caobunga run` gained or lost a positional argument "
        f"({[param.name for param in arguments]}). "
        f'{CONTRACT_PATH.name} §"Entry points" puts the batch form in `fleet` '
        '(CBN-312) and §"CLI verbs" states `run` requires `<project>`.'
    )
    assert arguments[0].required and arguments[0].nargs == 1, (
        "`caobunga run` no longer takes exactly one required project. "
        f'{CONTRACT_PATH.name} §"Entry points" (CBN-312): batch is `fleet`.'
    )
    assert not options, (
        f"`caobunga run` gained option(s) {[param.name for param in options]}. "
        f'If one of them is a fleet switch, {CONTRACT_PATH.name} §"Entry points" '
        "puts that in `fleet` (CBN-312); if not, extend this guard deliberately."
    )


def test_contract_still_records_why_the_fleet_verb_was_reopened() -> None:
    """The arity guard above is unexplained without CONTRACT's recorded reasoning.

    CBN-124 declined the batch form with a written reopen condition; CBN-312 met
    it and built `fleet`. If that paragraph is deleted, the split between `run`
    and `fleet` becomes a constraint nobody can date or argue with.
    """
    for claim in (
        "The batch entry point is `caobunga fleet` (CBN-312)",
        "**Reopened (CBN-312).**",
        "| Fleet batch verb (`fleet`) | CBN-312 (landed",
    ):
        assert claim in CONTRACT, (
            f"{CONTRACT_PATH.name} no longer carries {claim!r}. "
            "test_run_takes_exactly_one_required_project_argument pins the shape "
            "this text explains — move or restore the reasoning with it."
        )


def test_contract_on_disk_layout_names_the_suite_log_as_store_writes_it() -> None:
    """CONTRACT §"On-disk layout" vs. `Store.suite_file` (CBN-EPIC-202.3)."""
    documented = _sole_match(r"^  (suites/\{id\}/\S+)$", "suites/ layout line")
    written = Store(Path("/h")).suite_file("{id}", "{YYYYMMDDTHHMMSSmmmZ}", "{task_id}")
    assert documented == str(written.relative_to("/h")), (
        f"{CONTRACT_PATH.name} documents {documented!r}, but Store.suite_file writes "
        f"{str(written.relative_to('/h'))!r}"
    )


@needs_flaitron
def test_contract_tasknote_templates_match_readers() -> None:
    """CONTRACT §"Fleet boundary" template read vs. the vendored templates (CBN-235).

    Same cross-submodule shape as the SPEC pin above: the regexes come from
    the production readers, the headings from the templates actually
    vendored, so a flaitron bump that moves a heading fails here before
    `phase_progress()` goes quiet or the External review box misreads. The
    section's prose names the shapes; this asserts them.
    """
    full = FULL_TEMPLATE_PATH.read_text(encoding="utf-8")
    micro = MICRO_TEMPLATE_PATH.read_text(encoding="utf-8")

    phases = [
        (int(m.group(1)), tasknote._phase_slug(m.group(2)))
        for line in full.splitlines()
        if (m := tasknote._PHASE_HEADING.match(line)) is not None
    ]
    assert phases == [
        (1, "discovery"),
        (2, "execution"),
        (3, "testing"),
        (4, "closure"),
    ], (
        f"{FULL_TEMPLATE_PATH.name}: phase headings parse as {phases}; "
        "phase_progress() expects the four `## <emoji> Phase N:` headings "
        'CONTRACT §"Fleet boundary" records. Re-verify the template read.'
    )
    assert phase_progress(full) == "1 discovery"

    assert any(tasknote._NOTES_HEADING.match(line) for line in micro.splitlines()), (
        f"{MICRO_TEMPLATE_PATH.name}: no `## ⚡ Notes` heading matches "
        "_NOTES_HEADING; a micro-tasknote would no longer echo `phase: notes`."
    )
    assert phase_progress(micro) == "notes"

    # CBN-257: the full template's Phase 3 box is the label the classifier reads;
    # the v7 micro template carries an `**External review:**` line instead (CBN-330).
    boxes = [line for line in full.splitlines() if tasknote._EXTERNAL_REVIEW_BOX.match(line)]
    assert len(boxes) == 1, (
        f"{FULL_TEMPLATE_PATH.name}: expected exactly one `**External review**` box "
        f"matching tasknote._EXTERNAL_REVIEW_BOX, found {len(boxes)}."
    )
    assert external_review_box(full) == "missing"
    ticked = full.replace(boxes[0], boxes[0].replace("[ ]", "[x]", 1))
    assert external_review_box(ticked) == "ticked"
    # CBN-337: the below-box `N/A` is read only up to the box's next `##`, so the
    # template's `**Testing Notes:**` must sit in the same section as the box.
    na_below = ticked.replace(
        "**Testing Notes:**", "**Testing Notes:**\n\n- External review: N/A — tiny", 1
    )
    assert na_below != ticked, f"{FULL_TEMPLATE_PATH.name}: no `**Testing Notes:**` line."
    assert external_review_box(na_below) == "na", (
        f"{FULL_TEMPLATE_PATH.name}: `**Testing Notes:**` no longer shares the External "
        "review box's `##` section; the below-box `N/A` read (CBN-337) cannot reach it."
    )
    labels = [line for line in micro.splitlines() if tasknote._EXTERNAL_REVIEW_LABEL.match(line)]
    assert len(labels) == 1, (
        f"{MICRO_TEMPLATE_PATH.name}: expected exactly one `**External review:**` line "
        f"matching tasknote._EXTERNAL_REVIEW_LABEL, found {len(labels)}."
    )
    assert external_review_box(micro) == "missing"
    filled = micro.replace(labels[0], "**External review:** `/code-review medium` clean")
    assert external_review_box(filled) == "ticked"


# CBN-271.N: CONTRACT is the normative rules; the measurement, incident history,
# and rejected-options narrative behind them lives in `docs/decisions/*.md`
# (CBN-EPIC-271 moved it there, 35,636 → 28,746 words). Flaitron's own SPEC.md
# split regrew 3x because it recorded no number, so this one is enforced.
# Raising it is a deliberate edit here, not a way around it.
CONTRACT_WORD_BUDGET = 30_000


def test_contract_word_budget() -> None:
    words = len(CONTRACT.split())
    assert words <= CONTRACT_WORD_BUDGET, (
        f"{CONTRACT_PATH.name} is {words} words, over the {CONTRACT_WORD_BUDGET} budget. "
        "Move measurement / incident / rejected-options prose to docs/decisions/*.md "
        "behind a one-line pointer (CONTRACT preamble, CBN-271.N)."
    )


def test_invocation_contract_fields_and_statuses_match_schema() -> None:
    from caobunga.invocation import InvocationResult, InvocationStatus

    fields = _table_first_column("| Invocation field | Meaning |", "invocation fields")
    statuses = _table_first_column("| Invocation status | Meaning |", "invocation statuses")
    assert fields == list(InvocationResult.model_fields)
    assert statuses == [status.value for status in InvocationStatus]
