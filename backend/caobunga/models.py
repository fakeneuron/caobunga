"""Pydantic store types. Field names match docs/CONTRACT.md."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


class ExecutionTier(StrEnum):
    OBSERVE = "observe"
    GATED = "gated"
    PROCEED_ON_GREEN = "proceed-on-green"


# The two dispatch dimensions, deliberately adjacent because they were one set
# until CBN-118 and their containment is the thing to keep true.
#
# Every tier that launches dispatches a *headless* worker — `worker_message`
# sends `--unattended` / `--fast` whatever the tier — so every launching tier
# consults the task-level `[unattended]` marker, `gated` included. `gated`'s stop
# is after the worker's commit, not before it, so "the operator is present"
# (CBN-EPIC-17.2's assumption, overturned at CBN-118) was never true of the
# dispatch. `observe` launches nothing and so asks nothing.
#
# They live beside the enum rather than in `loop.py` (their home until CBN-130)
# because they are facts *about the tier*, and three modules now read them: the
# dispatcher, to pick its posture; `presence.py`, to report the tree state that
# posture would produce; and through it `api.py`, which must not import the
# dispatcher to learn what a tier means.
LAUNCHING_TIERS = frozenset({ExecutionTier.GATED, ExecutionTier.PROCEED_ON_GREEN})
# Tiers where nobody is watching the *sequence*, so the in-progress override is
# dropped rather than racing the operator's own open note (CBN-EPIC-17.3). A
# strict subset of the above: a drain is headless, but not every headless
# dispatch is a drain. One member since CBN-134 retired `plow`: the per-row
# `[unattended]` marker is the blast-radius bound on a drain, so a second bound
# derived from the task id's epic stem was a coarser layer on top of the
# operator's explicit one — and the one that produced false refusals.
DRAIN_TIERS = frozenset({ExecutionTier.PROCEED_ON_GREEN})


class Outcome(StrEnum):
    CLOSED = "closed"
    DE_SCOPED = "de-scoped"
    BLOCKED = "blocked"
    FAILED = "failed"
    NO_TASKNOTE = "no-tasknote"


# Outcomes that carry a closure commit: `close.inspect_close` sets `commit_sha`
# to `HEAD` on all three, unconditionally, so "a closure commit exists" and
# "the outcome is one of these" are the same predicate at the writer. This is
# the set `suite` is required on at `schema_version` 2 (CBN-EPIC-202.2): the
# loop-side suite measures a committed close, and a park or a failure leaves
# no closed tree to measure.
CLOSURE_OUTCOMES = frozenset({Outcome.CLOSED, Outcome.DE_SCOPED, Outcome.NO_TASKNOTE})


class HermesReview(StrEnum):
    """Whether this close got a Hermes review (CBN-EPIC-12.2). Legacy since CBN-258.

    `failed` is not a fourth value: CONTRACT §"Run loop" holds that a Hermes
    launch failure does not rewrite the run record, so it could never be
    persisted. Absence therefore covers both legacy records and runs whose
    review step did not complete — and the latter is never silent, since it
    always coincides with exit 4. `degraded` (CBN-180) is a different case
    from the one that rule excludes: the launch completed and returned
    output, so it is exactly as persistable as `reviewed` — it names a review
    that ran with fewer tools than the retired `hermes.REVIEW_MCP_SERVERS` granted (the
    launch profile did not carry one or more of them), parsed from Hermes's
    own `Unknown toolsets: ...` warning. `hermes_missing_servers` names which.
    """

    REVIEWED = "reviewed"
    SKIPPED = "skipped"
    DEGRADED = "degraded"


class HermesVerdictCode(StrEnum):
    """The reviewer's fixed first-line verdict, parsed out of `hermes_verdict` (CBN-160).

    Legacy since CBN-258: read on old records, never written.

    Skip-not-fail: an unparseable first line records `inconclusive` rather than
    leaving the field absent, so a reviewer that ignored the format still counts
    as *something* an operator can sort or tally on — the raw prose survives
    unabridged in `hermes_verdict` either way. No dispatch gate reads this field
    (CBN-160's PLAN line): Hermes judges the brief, not the repo.
    """

    ACCEPT = "accept"
    REJECT = "reject"
    INCONCLUSIVE = "inconclusive"


class SuiteVerdict(StrEnum):
    """What the loop-side deterministic suite said about a close (CBN-EPIC-202.2).

    The one key a dispatch decision reads — CBN-EPIC-202.4 continues a
    `proceed-on-green` drain only on `closed` **and** `green` — which is what
    separates it from every Hermes field: those are narrative, this is a fold
    over three exit codes. `Suite.fold` is the fold; precedence is `red` over
    `timeout` over `refused` over `green`, so a suite that both failed a verb
    and could not run another names the failure, and `refused` — nothing ran,
    or `just` was not there to run it — is a stop in its own right, never
    "not measured" (CBN-EPIC-202.1 D5).
    """

    GREEN = "green"
    RED = "red"
    TIMEOUT = "timeout"
    REFUSED = "refused"


class ExternalReview(StrEnum):
    """How the archived note's Phase 3 External review box closed (CBN-257).

    Mirrors `flaitron.external_review_box`'s vocabulary on the record side, the
    boundary `park_reason` draws. `missing` is the signal: the runner closed a
    full-shape note without the in-run review (CBN-242's replacement for the
    Hermes post-commit review). Narrative, not a decision — no gate reads it.
    """

    TICKED = "ticked"
    NA = "na"
    MISSING = "missing"


class Escalation(StrEnum):
    """What stopped a run. Set only when `outcome` is `blocked` or `failed`."""

    NOTE_BLOCKED = "note-blocked"
    WORKER_PARKED = "worker-parked"
    WORKER_LAUNCH = "worker-launch"
    WORKER_ERROR = "worker-error"
    WAITING_USER = "waiting-user"
    WORKER_EXITED = "worker-exited"
    TIMEOUT = "timeout"


def utc_now() -> datetime:
    return datetime.now(UTC)


def compact_utc(value: datetime) -> str:
    """UTC compact stamp for run/suite/rollup filenames (`YYYYMMDDTHHMMSSmmmZ`).

    Milliseconds keep filename lexicographic order aligned with wall time when
    two closes share a UTC second (CBN-098).
    """
    value = value.astimezone(UTC)
    ms = value.microsecond // 1000
    return f"{value.strftime('%Y%m%dT%H%M%S')}{ms:03d}Z"


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RegistryRecord(_ContractModel):
    schema_version: int = 1
    id: str
    path: str
    tier: ExecutionTier = ExecutionTier.GATED
    provider: str | None = None
    model: str | None = None
    providers: list[str] | None = None
    registered_at: datetime = Field(default_factory=utc_now)
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def _id_slug(cls, value: str) -> str:
        if not ID_PATTERN.fullmatch(value):
            msg = f"invalid id {value!r}: must match [a-z][a-z0-9-]*"
            raise ValueError(msg)
        return value

    @field_validator("path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        if not value.startswith("/"):
            msg = "path must be absolute"
            raise ValueError(msg)
        return value

    @field_validator("providers")
    @classmethod
    def _normalize_providers(cls, value: list[str] | None) -> list[str] | None:
        """Strip, drop blanks, dedupe keeping first position. Declared order is semantic."""
        if value is None:
            return None
        tokens: list[str] = []
        for raw in value:
            token = raw.strip()
            if token and token not in tokens:
                tokens.append(token)
        # An empty declaration is undeclared, not "no worker is eligible".
        return tokens or None

    @field_serializer("registered_at")
    def _ser_registered_at(self, value: datetime) -> str:
        return iso_z(value)


class NewRow(_ContractModel):
    """A PLAN row that appeared between a run's launch read and its close (CBN-138.3).

    Lives here rather than in `flaitron.py` on the boundary `RunRecord.park_reason`
    already draws: the record layer *mirrors* a producer's vocabulary instead of
    importing the producer reader, so a `models -> flaitron` edge never forms.
    `close.py` imports both and does the mapping.

    `unattended` is the literal `[unattended]` marker off `PlanTask`, deliberately
    **not** the `eligibility` predicate `coverage` and `marker_drift` use. Those
    make a *dispatch* claim about the current queue; this is a record of what one
    past run produced, and the live dispatch question about these same rows is
    answered by `format_coverage` on the next status read. Re-deriving it here
    would duplicate that on a subset, and would label a `handoff` or `blocked` row
    "unmarked" when a marker is not its fix — the misdirection CBN-114 and CBN-136
    each closed by checking the more specific dimension first.
    """

    task_id: str
    unattended: bool = False


class ForeignCommit(_ContractModel):
    """A commit in `start_sha..HEAD` this run does not itself account for (CBN-172).

    Report-only, the same posture as `NewRow`: it neither blocks nor discards the
    worker's work, it just names what landed in a tree caobunga was dispatching
    into. `sha` is the full commit hash — short hashes are not stable identifiers
    across a rewritten history — `author` is `git log`'s `%an`, and `subject` is
    the first commit-message line, clipped like every other narrative field.
    """

    sha: str
    author: str
    subject: str


class SuiteVerb(_ContractModel):
    """One fleet verb's result on the loop-side suite (CBN-EPIC-202.2).

    `verb` is the fleet verb — `test` / `lint` / `typecheck`, the `just` recipe
    Fleet HQ's `STACK-TENDENCIES.md` §"Command interface" names — not the MCP
    tool name `suite_verbs.VERBS` keys on. A string, not an enum, on the
    `park_reason` boundary: the verb set is the fleet's to change, and this
    module cannot import `suite_verbs` to pin it without closing a cycle
    (`suite_verbs` → `store` → here). `exit_code` is `None` exactly when the
    verb never returned one — `timed_out` or `refused` says which, and the
    validator below rejects a verb with neither a code nor a reason, so nothing
    folds to `green` by silence. `detail` is the last non-blank output line
    that is not a script echo (npm's `> tsc -b`, a recipe's U+203A-led line),
    clipped like `summary`; the full rendered output is the `suites/` log's
    job (CBN-EPIC-202.3), never this field's — the deny-list forbids raw CLI
    stdout on every report surface. `tree_changed` is report-only: the next
    launch's dirty-tree refusal is the enforcement.
    """

    verb: str
    exit_code: int | None = None
    duration: float
    timed_out: bool = False
    refused: str | None = None
    tree_changed: bool = False
    detail: str | None = None

    @field_validator("refused", "detail")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        """A blank string is undeclared — the rule `hermes_verdict` follows."""
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _exit_code_absent_iff_not_returned(self) -> Self:
        returned = self.exit_code is not None
        not_returned = self.timed_out or self.refused is not None
        if returned == not_returned:
            msg = (
                f"suite verb {self.verb!r}: exit_code must be absent exactly when "
                "timed_out or refused"
            )
            raise ValueError(msg)
        return self


class Suite(_ContractModel):
    """The loop-side deterministic suite's result on one close (CBN-EPIC-202.2).

    `verdict` is stored, not derived at read: it is the one key the drain gate
    (CBN-EPIC-202.4) and every report surface (CBN-EPIC-202.5) read, and a
    reader that had to refold `verbs` to learn it would be a second copy of
    the rule. `_verdict_matches_verbs` keeps the stored value honest against
    `fold`, so a runner bug cannot write `green` over a red verb. `verbs` is
    non-empty in every state — a refusal still records one refused entry per
    fleet verb — but the exact verb set is the fleet's, not asserted here.
    """

    verdict: SuiteVerdict
    verbs: list[SuiteVerb] = Field(min_length=1)

    @staticmethod
    def fold(verbs: Sequence[SuiteVerb]) -> SuiteVerdict:
        """`red` over `timeout` over `refused` over `green` — see `SuiteVerdict`."""
        if any(verb.exit_code is not None and verb.exit_code != 0 for verb in verbs):
            return SuiteVerdict.RED
        if any(verb.timed_out for verb in verbs):
            return SuiteVerdict.TIMEOUT
        if any(verb.refused is not None for verb in verbs):
            return SuiteVerdict.REFUSED
        return SuiteVerdict.GREEN

    @classmethod
    def from_verbs(cls, verbs: Sequence[SuiteVerb]) -> Self:
        """The constructor a runner uses: the fold lives once, here."""
        return cls(verdict=cls.fold(verbs), verbs=list(verbs))

    @model_validator(mode="after")
    def _verdict_matches_verbs(self) -> Self:
        expected = self.fold(self.verbs)
        if self.verdict is not expected:
            msg = (
                f"suite verdict {self.verdict.value!r} does not fold from its verbs "
                f"(expected {expected.value!r})"
            )
            raise ValueError(msg)
        return self


class RunRecord(_ContractModel):
    """One closed run. `invocation_id` groups the records one `caobunga run` wrote.

    An *invocation* is a single `execute_run` call: `gated` writes one record,
    `proceed-on-green` writes one per drained task. Nothing else in the
    record separates those — reading a flat history meant doing arithmetic on
    `closed_at` gaps to guess where one drain ended (CBN-EPIC-12.6 F3). The
    *cohort* half of that finding needs no field: `flaitron.epic_stem` derives it
    from `task_id`.

    `park_reason` is deliberately a plain string, not an enum. It carries the
    code half of flaitron's `park-reason:` frontmatter — flaitron's vocabulary,
    recorded on caobunga's surface rather than imported into its contract, so a
    change to that closed set is not automatically a `schema_version` question
    here (CBN-EPIC-13.2 fleet boundary). It annotates a park; it does not
    classify the stop, which stays `escalation_reason`'s job.
    """

    schema_version: int = 2
    project: str
    task_id: str
    invocation_id: str | None = None
    outcome: Outcome
    commit_sha: str | None = None
    started_at: datetime | None = None
    closed_at: datetime
    escalation_reason: Escalation | None = None
    park_reason: str | None = None
    # Why the picked task was eligible for this run, in the `<code> — <prose>`
    # shape (CBN-EPIC-17.2). Optional at schema_version 1 per §Compatibility.
    # Distinct from the picker why-log, which stays off the record: this is the
    # task-eligibility decision, not the provider choice.
    eligibility_reason: str | None = None
    # The four `hermes_*` fields below are read-only history since CBN-258
    # retired the Hermes review: kept so records written before it load and
    # render (digest, status UI), never written by the loop.
    hermes_review: HermesReview | None = None
    # What the reviewer said, clipped like `summary` (CBN-148). Optional at
    # schema_version 1 per §Compatibility. Narrative like `follow_ups`, not a
    # decision: nothing routes on it. `hermes_review` says whether a review ran;
    # this is the excerpt of its answer, and the full stdout sits under
    # `reviews/` beside the brief it answered. Before this field the reviewer's
    # verdict — and the checks it named it could not perform (CBN-147) — reached
    # stdout only, so a drain's review survived only if an operator was watching.
    hermes_verdict: str | None = None
    # The fixed-first-line verdict `hermes_verdict` parses to (CBN-160). Optional
    # at schema_version 1 per §Compatibility. Independent of `hermes_verdict`
    # the same way that field is independent of `hermes_review`: `None` covers
    # both "no review launched" and "review launched, said nothing" — the same
    # two cases `hermes_verdict` itself collapses to `None` for. A review that
    # said something but skipped the fixed first line still gets a code
    # (`inconclusive`), never `None` — skip-not-fail is a parse outcome, not an
    # absence. Narrative-adjacent, not a decision: no dispatch gate reads it.
    hermes_verdict_code: HermesVerdictCode | None = None
    # The retired `hermes.REVIEW_MCP_SERVERS` names Hermes reported unknown on this
    # launch (CBN-180), parsed from its `Unknown toolsets: ...` warning.
    # Optional at `schema_version` 1 per §Compatibility. Set exactly when
    # `hermes_review` is `degraded`; `None`/omitted otherwise, the same
    # absence rule `hermes_verdict_code` uses. Narrative-adjacent, not a
    # decision: nothing dispatches, denies, or routes on it — it exists so a
    # rubber-stamp review (CBN-178: a fresh shell's `default` profile has no
    # MCP grant) names what it was missing instead of reading as a clean
    # `reviewed`.
    hermes_missing_servers: list[str] | None = None
    provider: str | None = None
    model: str | None = None
    plan_model: str | None = None
    next_task: str | None = None
    tasknote_path: str | None = None
    summary: str
    # Follow-up lines the closing worker proposed, read from its archived
    # tasknote (CBN-121). Optional at schema_version 1 per §Compatibility.
    # A *narrative* field like `summary`, not a decision: nothing dispatches,
    # denies, or routes on it — it exists so a discovery made with nobody
    # watching reaches the digest instead of the archive alone.
    follow_ups: list[str] | None = None
    # The `[unattended]`-marked set this run's **pre-flight** read saw (CBN-125).
    # Optional at schema_version 1 per §Compatibility. The baseline
    # `flaitron.marker_drift` diffs a later read against, so a cohort disarmed by
    # an unrelated line edit surfaces before the next drain rather than after.
    #
    # This is not the cached classification CBN-EPIC-17.1 rules out. That rule
    # governs whatever answers the *current* question, and current coverage is
    # still derived per read; this records what one past run observed, which is
    # the same category as the record it rides on. `eligibility_reason` is the
    # precedent — persisted at CBN-EPIC-17.2 over the same constitution.
    marked_tasks: list[str] | None = None
    # PLAN rows that appeared while this run was in flight (CBN-138.3), diffed
    # against the ids its pre-flight read saw. Optional at schema_version 1 per
    # §Compatibility. Narrative like `follow_ups`, not a decision: nothing
    # dispatches, denies, or seeds on it — it exists so a drain's own discoveries
    # reach the digest and `caobunga status` before the picker denies them
    # for want of a marker nobody knew to seed.
    new_rows: list[NewRow] | None = None
    # Where this run's worker stdout/stderr was teed (CBN-162). Optional at
    # schema_version 1 per §Compatibility. A path, never contents — the
    # deny-list already forbids "worker transcripts, raw CLI stdout" from every
    # report surface, and this field does not carry either; it lets an operator
    # find the capped/rotated tee file on disk. `None` for a `local` run whose
    # worker never reached `send`, or an adapter that keeps no tee.
    log_path: str | None = None
    # Commits in `start_sha..HEAD` this run does not itself account for (CBN-172).
    # Optional at schema_version 1 per §Compatibility. Report-only like `tree`
    # and `unparsed`: nothing dispatches, denies, or discards the worker's work
    # on it — it exists so a commit that lands in the target repo from anywhere
    # else during a dispatch reaches the record instead of a hand `git log`.
    foreign_commits: list[ForeignCommit] | None = None
    # The loop-side deterministic suite's result on this close (CBN-EPIC-202.2).
    # Required at `schema_version` 2 exactly when `outcome` is in
    # `CLOSURE_OUTCOMES`, forbidden otherwise — the same conditionally-required
    # class as `commit_sha`, and the first field a dispatch decision reads
    # (CBN-EPIC-202.4). The bump is what makes absence unambiguous: on a v2
    # close, no `suite` is a record that never got written, never "not
    # measured". v1 records load unchanged — the required direction keys on the
    # version, the forbidden one holds at every version — and a reader that
    # needs a v1 record's suite treats absence as unknown, never as `green`.
    suite: Suite | None = None
    # How the archived tasknote's External review box closed (CBN-257). Optional
    # at `schema_version` 2 per §Compatibility — added without a bump, so absence
    # is "not read" (a park, a failure, a `no-tasknote` close, or a record
    # written before the field), never `missing`.
    external_review: ExternalReview | None = None
    # Providers a usage-limit refusal ruled out for this invocation (CBN-286),
    # sorted. Optional at `schema_version` 2 per §Compatibility. Report-only:
    # nothing routes on it. Before it, the CBN-213 re-pick showed only in the
    # terminal echo, so a record whose `provider` was the fallback gave no sign
    # the first-choice provider had been refused. `None` when no re-pick ran.
    exhausted_providers: list[str] | None = None

    @field_validator("project")
    @classmethod
    def _project_slug(cls, value: str) -> str:
        if not ID_PATTERN.fullmatch(value):
            msg = f"invalid project id {value!r}: must match [a-z][a-z0-9-]*"
            raise ValueError(msg)
        return value

    @field_validator("task_id")
    @classmethod
    def _task_id_safe(cls, value: str) -> str:
        if not value or "/" in value or "\\" in value:
            msg = f"invalid task_id {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("follow_ups", "marked_tasks", "exhausted_providers")
    @classmethod
    def _normalize_optional_list(cls, value: list[str] | None) -> list[str] | None:
        """Strip and drop blanks; an empty result is undeclared, not "none of these".

        Same shape as `RegistryRecord.providers` for the same reason: `None` is
        what `exclude_none` needs to keep the key off records that have nothing
        to say, and `follow_ups: []` on every record ever written is the outcome
        a plain list default produces. `marked_tasks` shares the rule so a record
        cannot distinguish "recorded an empty marked set" from "recorded none"
        — only one of those is a state `marker_drift` can act on.
        """
        if value is None:
            return None
        lines = [line.strip() for line in value]
        return [line for line in lines if line] or None

    @field_validator("hermes_verdict")
    @classmethod
    def _blank_verdict_is_none(cls, value: str | None) -> str | None:
        """A blank verdict is undeclared — the string form of the list rule above."""
        if value is None:
            return None
        return value.strip() or None

    @field_validator("new_rows")
    @classmethod
    def _empty_new_rows_is_none(cls, value: list[NewRow] | None) -> list[NewRow] | None:
        """An empty list is undeclared, the same rule the string lists above follow.

        Kept separate rather than folded into `_normalize_optional_list`: that one
        strips and drops blank *strings*, which a `NewRow` has no analogue for.
        Sharing it would mean widening a text normalizer to a shape it cannot
        normalize, for one line of reuse.
        """
        return value or None

    @field_validator("foreign_commits")
    @classmethod
    def _empty_foreign_commits_is_none(
        cls, value: list[ForeignCommit] | None
    ) -> list[ForeignCommit] | None:
        """An empty list is undeclared, the same rule `new_rows` follows above."""
        return value or None

    @model_validator(mode="after")
    def _commit_sha_when_closed(self) -> Self:
        if self.outcome is Outcome.CLOSED and not self.commit_sha:
            msg = "commit_sha is required when outcome is closed"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _suite_iff_closure(self) -> Self:
        """Both directions, keyed on the version so v1 records still load."""
        closure = self.outcome in CLOSURE_OUTCOMES
        if self.suite is not None and not closure:
            msg = (
                "suite is only valid when outcome carries a closure commit, "
                f"not {self.outcome.value}"
            )
            raise ValueError(msg)
        if self.schema_version >= 2 and closure and self.suite is None:
            msg = (
                f"suite is required at schema_version {self.schema_version} "
                f"when outcome is {self.outcome.value}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _escalation_only_when_stopped(self) -> Self:
        """Assert the forbidden direction only.

        The mirror rule — an escalation is *required* when the run stopped —
        is enforced at the write sites and by their tests, deliberately not
        here: run records already on disk carry `failed` / `blocked` with no
        `escalation_reason`, and rejecting them would break `Store.list_runs`.
        `_commit_sha_when_closed` can assert both directions only because it
        has existed since `schema_version` 1; `_suite_iff_closure` can because
        it keys its required direction on the version bump that introduced it.
        """
        stopped = (Outcome.BLOCKED, Outcome.FAILED)
        if self.escalation_reason is not None and self.outcome not in stopped:
            msg = (
                "escalation_reason is only valid when outcome is blocked or "
                f"failed, not {self.outcome.value}"
            )
            raise ValueError(msg)
        return self

    @field_serializer("closed_at")
    def _ser_closed_at(self, value: datetime) -> str:
        return iso_z(value)

    @field_serializer("started_at")
    def _ser_started_at(self, value: datetime | None) -> str | None:
        return iso_z(value) if value is not None else None
