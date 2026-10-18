import { describe, expect, it } from "vitest";

import {
  ESCALATIONS,
  OUTCOMES,
  type CurrentTask,
  type Denial,
  type DirtLine,
  type EscalationReason,
  type ExecutionTier,
  type ExternalReview,
  type ForeignCommit,
  type HermesReview,
  type HermesVerdictCode,
  type NewRow,
  type Outcome,
  type ProjectDetail,
  type ProjectReports,
  type RunRecord,
  type Suite,
  type SuiteVerb,
  type SuiteVerdict,
  type TriageClass,
  type UnparsedRow,
} from "../../src/api/types";
// Shared with backend/tests/test_api_payload_keys.py (CBN-EPIC-231.3): both
// suites derive a key list from their own side and diff it against this file,
// so a field added to one side and not the other fails whichever suite reads
// it next. A plain JSON import (no Node `fs`/`path` — `tests/unit` type-checks
// against the DOM lib, not `@types/node`) resolves via Vite at build/test time.
import rawFixture from "../../../fixtures/api-payload-keys.json";

const fixture = rawFixture as unknown as Record<string, string[]> & {
  enum_values: Record<string, string[]>;
};

/**
 * `Record<keyof T, true>` forces this literal to name exactly `keyof T` — TS
 * rejects a missing key (the mapped type requires it) and an excess one
 * (object-literal excess-property checking), so `Object.keys(...)` below is a
 * compiler-enforced mirror of the type, not a hand-kept copy that can drift
 * from it silently.
 */
function keysOf<T>(literal: Record<keyof T, true>): string[] {
  return Object.keys(literal).sort();
}

/** `keysOf`'s twin for a string-literal union: the literal must name every member, and only members. */
function valuesOf<U extends string>(literal: Record<U, true>): string[] {
  return Object.keys(literal).sort();
}

// Derived straight off `ProjectDetail`'s own nested field types (indexed
// access + `NonNullable`) rather than re-declared as a standalone shape, so
// there is exactly one place — `src/api/types.ts` — that says what the
// detail variant of `coverage` / `tree` looks like.
type ProjectDetailCoverage = NonNullable<ProjectDetail["coverage"]>;
type ProjectDetailTree = NonNullable<ProjectDetail["tree"]>;

const PROJECT_DETAIL_KEYS = keysOf<ProjectDetail>({
  schema_version: true,
  id: true,
  path: true,
  tier: true,
  provider: true,
  model: true,
  providers: true,
  registered_at: true,
  enabled: true,
  last_run: true,
  current: true,
  coverage: true,
  tree: true,
  runs: true,
  run_counts: true,
  reports: true,
});

const COVERAGE_DETAIL_KEYS = keysOf<ProjectDetailCoverage>({
  open: true,
  marked: true,
  denied: true,
  unparsed: true,
  denials: true,
  unparsed_rows: true,
  marked_tasks: true,
});

const TREE_DETAIL_KEYS = keysOf<ProjectDetailTree>({
  readable: true,
  clean: true,
  blocking: true,
  candidate: true,
  exempt_task: true,
  clean_for: true,
  blocking_lines: true,
});

const CURRENT_TASK_KEYS = keysOf<CurrentTask>({
  task_id: true,
  status: true,
  reason: true,
  plan_model: true,
  shortname: true,
});

const REPORTS_KEYS = keysOf<ProjectReports>({
  last_rollup: true,
});

const RUN_RECORD_KEYS = keysOf<RunRecord>({
  schema_version: true,
  project: true,
  task_id: true,
  outcome: true,
  commit_sha: true,
  started_at: true,
  closed_at: true,
  escalation_reason: true,
  park_reason: true,
  eligibility_reason: true,
  hermes_review: true,
  hermes_verdict: true,
  hermes_verdict_code: true,
  hermes_missing_servers: true,
  provider: true,
  model: true,
  plan_model: true,
  invocation_id: true,
  next_task: true,
  tasknote_path: true,
  summary: true,
  follow_ups: true,
  new_rows: true,
  marked_tasks: true,
  log_path: true,
  foreign_commits: true,
  suite: true,
  external_review: true,
  exhausted_providers: true,
});

// Element shapes of the arrays / objects nested inside the shapes above
// (CBN-234): `coverage.denials[]` / `unparsed_rows[]`, `tree.blocking_lines[]`,
// `runs[].new_rows[]` / `foreign_commits[]` / `suite` / `suite.verbs[]`.
const DENIAL_KEYS = keysOf<Denial>({
  task_id: true,
  reason: true,
  line: true,
  text: true,
  seeded_line: true,
  unparsed_blocker: true,
  triage: true,
  triage_reason: true,
});

const UNPARSED_ROW_KEYS = keysOf<UnparsedRow>({
  line: true,
  text: true,
});

const DIRT_LINE_KEYS = keysOf<DirtLine>({
  text: true,
  path: true,
  task_id: true,
});

const NEW_ROW_KEYS = keysOf<NewRow>({
  task_id: true,
  unattended: true,
});

const FOREIGN_COMMIT_KEYS = keysOf<ForeignCommit>({
  sha: true,
  author: true,
  subject: true,
});

const SUITE_KEYS = keysOf<Suite>({
  verdict: true,
  verbs: true,
});

const SUITE_VERB_KEYS = keysOf<SuiteVerb>({
  verb: true,
  exit_code: true,
  duration: true,
  timed_out: true,
  refused: true,
  tree_changed: true,
  detail: true,
});

// Value vocabulary of each string-literal union the payload carries (CBN-263),
// mirrored from the backend StrEnums through the fixture's `enum_values` block.
const ENUM_VALUES: [string, string[]][] = [
  ["execution_tier", valuesOf<ExecutionTier>({ observe: true, gated: true, "proceed-on-green": true })],
  [
    "outcome",
    valuesOf<Outcome>({
      closed: true,
      "de-scoped": true,
      blocked: true,
      failed: true,
      "no-tasknote": true,
    }),
  ],
  [
    "escalation_reason",
    valuesOf<EscalationReason>({
      "note-blocked": true,
      "worker-parked": true,
      "worker-launch": true,
      "worker-error": true,
      "waiting-user": true,
      "worker-exited": true,
      timeout: true,
    }),
  ],
  ["hermes_review", valuesOf<HermesReview>({ reviewed: true, skipped: true, degraded: true })],
  [
    "hermes_verdict_code",
    valuesOf<HermesVerdictCode>({ accept: true, reject: true, inconclusive: true }),
  ],
  [
    "suite_verdict",
    valuesOf<SuiteVerdict>({ green: true, red: true, timeout: true, refused: true }),
  ],
  ["external_review", valuesOf<ExternalReview>({ ticked: true, na: true, missing: true })],
  ["triage_class", valuesOf<TriageClass>({ obvious: true, "needs-judgment": true })],
];

describe("frontend/src/api/types.ts unions vs. the fixture's enum_values block", () => {
  it.each(ENUM_VALUES)("%s values match the fixture", (fixtureKey, unionValues) => {
    const declared = fixture.enum_values[fixtureKey];
    if (declared === undefined) {
      throw new Error(`fixtures/api-payload-keys.json enum_values has no "${fixtureKey}" entry`);
    }
    expect(unionValues).toEqual([...declared].sort());
  });

  it("covers every enum_values entry", () => {
    expect(ENUM_VALUES.map(([key]) => key).sort()).toEqual(Object.keys(fixture.enum_values).sort());
  });

  // The runtime chip-set arrays must name the same members as their unions.
  it.each([
    ["outcome", [...OUTCOMES]],
    ["escalation_reason", [...ESCALATIONS]],
  ])("%s chip set matches the fixture", (fixtureKey, chips) => {
    expect(chips.sort()).toEqual([...(fixture.enum_values[fixtureKey] ?? [])].sort());
  });
});

describe("frontend/src/api/types.ts vs. the checked-in payload-key fixture", () => {
  it.each([
    ["project_detail", PROJECT_DETAIL_KEYS],
    ["coverage_detail", COVERAGE_DETAIL_KEYS],
    ["tree_detail", TREE_DETAIL_KEYS],
    ["current_task", CURRENT_TASK_KEYS],
    ["reports", REPORTS_KEYS],
    ["run_record", RUN_RECORD_KEYS],
    ["denial", DENIAL_KEYS],
    ["unparsed_row", UNPARSED_ROW_KEYS],
    ["dirt_line", DIRT_LINE_KEYS],
    ["new_row", NEW_ROW_KEYS],
    ["foreign_commit", FOREIGN_COMMIT_KEYS],
    ["suite", SUITE_KEYS],
    ["suite_verb", SUITE_VERB_KEYS],
  ])("%s keys match the fixture", (fixtureKey, typeKeys) => {
    const declared = fixture[fixtureKey];
    if (declared === undefined) {
      throw new Error(`fixtures/api-payload-keys.json has no "${fixtureKey}" entry`);
    }
    expect(typeKeys).toEqual([...declared].sort());
  });
});
