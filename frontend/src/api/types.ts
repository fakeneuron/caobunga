/** Types matching docs/CONTRACT.md Status API (CBN-040). */

export type ExecutionTier = "observe" | "gated" | "proceed-on-green";

export type Outcome =
  "closed" | "de-scoped" | "blocked" | "failed" | "no-tasknote";

/** Fixed order for the outcome chip set; `run_counts` always carries all five. */
export const OUTCOMES: readonly Outcome[] = [
  "closed",
  "de-scoped",
  "blocked",
  "failed",
  "no-tasknote",
];

export type EscalationReason =
  | "note-blocked"
  | "worker-parked"
  | "worker-launch"
  | "worker-error"
  | "waiting-user"
  | "worker-exited"
  | "timeout";

/** Fixed order for the escalation chip set; `escalations` always carries all seven. */
export const ESCALATIONS: readonly EscalationReason[] = [
  "note-blocked",
  "worker-parked",
  "worker-launch",
  "worker-error",
  "waiting-user",
  "worker-exited",
  "timeout",
];

export type HermesReview = "reviewed" | "skipped" | "degraded";

/** The reviewer's parsed first line. See docs/CONTRACT.md §"Hermes review". */
export type HermesVerdictCode = "accept" | "reject" | "inconclusive";

/**
 * The loop-side deterministic suite's fold over its verbs — `red` over
 * `timeout` over `refused` over `green`. The one record key a dispatch
 * decision reads. See docs/CONTRACT.md §"Suite".
 */
export type SuiteVerdict = "green" | "red" | "timeout" | "refused";

/** How the archived note's External review box closed. See docs/CONTRACT.md §"External review". */
export type ExternalReview = "ticked" | "na" | "missing";

/**
 * One fleet verb's result (`test` / `lint` / `typecheck`). `exit_code` is
 * absent exactly when `timed_out` or `refused`; `refused` and `detail` are
 * absent — never `""` — when they have nothing to say.
 */
export type SuiteVerb = {
  verb: string;
  exit_code?: number | null;
  duration: number;
  timed_out: boolean;
  refused?: string | null;
  tree_changed: boolean;
  detail?: string | null;
};

export type Suite = {
  verdict: SuiteVerdict;
  /** One entry per fleet verb, in the fixed order test → lint → typecheck. */
  verbs: SuiteVerb[];
};

export type LastRunSummary = {
  task_id: string;
  outcome: string;
  closed_at: string;
  summary: string;
};

export type CurrentTask = {
  task_id: string;
  status: string;
  reason: string;
  plan_model?: string;
  shortname?: string;
};

/**
 * Split of an `unmarked` denial into "safe to seed mechanically" vs "needs an
 * operator's read" (CBN-157). See docs/CONTRACT.md §"Marker coverage".
 */
export type TriageClass = "obvious" | "needs-judgment";

/**
 * One open task an unattended pick would deny, with its `<code> — <prose>`
 * reason and what it takes to act on it (CBN-119.3): where the PLAN line is,
 * what it says now, and — when a marker is the fix — what it should say.
 */
export type Denial = {
  task_id: string;
  reason: string;
  /** 1-based PLAN line; `0` on records written before CBN-119.3. */
  line: number;
  /** The row verbatim, as the operator would edit it. */
  text: string;
  /**
   * The row with `[unattended]` placed after `[model]` and any suggestion
   * glyph (CBN-343). `null` when seeding is not the fix (a `blocked` or
   * `exploratory` denial) or when no safe insertion exists. Caobunga never
   * applies it — seeding stays a manual operator act.
   */
  seeded_line: string | null;
  /**
   * A blocker the `Blocked by [[ID]]` grammar cannot see (CBN-128) — the row
   * says "blocked" and no clause parsed, so it denies as merely unmarked and
   * offers a `seeded_line` that would make it dispatch. The fragment from the
   * mention to the end of the description. `null` when the row has a parsed
   * clause (its `reason` already says `blocked`) or says nothing about being
   * blocked. Report-only: nothing denies on it.
   */
  unparsed_blocker: string | null;
  /**
   * Set on an `unmarked` denial only (CBN-157); `null` for every other denial
   * code, since the mirrored predicate never judged those.
   */
  triage?: TriageClass | null;
  /** `<code> — <prose>` clause a `needs-judgment` triage names; `null` when `triage` is `obvious` or absent. */
  triage_reason?: string | null;
};

/**
 * An open-looking PLAN row the task-line grammar dropped (CBN-115) — the id is
 * unrecoverable, so the line number + raw text is the only handle.
 */
export type UnparsedRow = {
  line: number;
  text: string;
};

/**
 * `[unattended]` marker coverage over a project's open launch queue
 * (docs/CONTRACT.md §"Flaitron reader"). `marked + denied === open` always;
 * `unparsed` counts dropped rows beside the tally, never inside `open`.
 * Computed as-if-unattended whatever the project's tier, so a `gated` repo
 * awaiting promotion reports a real figure. `denials` / `unparsed_rows` are
 * detail-only — the list endpoint carries the counts alone. `denials` is in
 * dispatch order (CBN-119.3), so it reads top-down as the order to seed in.
 */
export type Coverage = {
  open: number;
  marked: number;
  denied: number;
  unparsed: number;
  denials?: Denial[];
  unparsed_rows?: UnparsedRow[];
  /** The ids `marked` tallies, in the same dispatch order `denials` uses. Detail-only, like `denials`. */
  marked_tasks?: string[];
};

/**
 * One uncommitted line in the target tree, classified (CBN-130). `task_id` is
 * set when the line is an open tasknote — the class of dirt whose blocking-ness
 * depends on which row is the candidate. `path` is `null` when the porcelain
 * path could not be recovered (git C-quotes unusual paths); such a line is
 * always blocking, never silently exempt.
 */
export type DirtLine = {
  text: string;
  path: string | null;
  task_id: string | null;
};

/**
 * Whether the operator-presence gate (docs/CONTRACT.md §"Run loop") would refuse
 * a launch into this target, served before a refusal has to reveal it (CBN-130).
 * Computed at the project's **own tier** posture, because `drain` moves the
 * candidate and the candidate decides which open tasknote is exempt.
 *
 * The list endpoint carries the verdict alone; `candidate` / `exempt_task` /
 * `clean_for` / `blocking_lines` are detail-only, the same split `Coverage` uses.
 */
export type TreeState = {
  /** `false` when git could not report. Not "clean" — a launch would refuse. */
  readable: boolean;
  clean: boolean;
  /** How many lines would refuse the launch, after the candidate's exemption. */
  blocking: number;
  candidate?: string | null;
  /** The open tasknote this candidate exempts, when it has one. */
  exempt_task?: string | null;
  /**
   * The one candidate whose exemption would clear every blocking line — the
   * counterfactual that makes the seed↔dirt coupling legible. `null` when no
   * single candidate choice would clear the tree.
   */
  clean_for?: string | null;
  blocking_lines?: DirtLine[];
};

export type ProjectListItem = {
  schema_version?: number;
  id: string;
  path: string;
  tier: ExecutionTier;
  provider?: string | null;
  model?: string | null;
  /** Allow-list the picker may choose from, best-first. Absent = every installed worker is eligible. */
  providers?: string[] | null;
  registered_at?: string;
  enabled: boolean;
  last_run: LastRunSummary | null;
  current: CurrentTask | null;
  /** `null` when the registered path is missing or holds no Flaitron project. */
  coverage: Coverage | null;
  /** `null` on the same unreadable-path condition as `coverage`. */
  tree: TreeState | null;
};

/**
 * A PLAN row that appeared between a run's launch read and its close.
 * `unattended` is the literal `[unattended]` marker as the row carried it — not
 * the eligibility predicate `Coverage` uses — so an unmarked row here is a
 * statement about what that run produced, not about what dispatches now.
 * See docs/CONTRACT.md §"New-row detection".
 */
export type NewRow = {
  task_id: string;
  unattended: boolean;
};

/** A commit in `start_sha..HEAD` this run does not itself account for. */
export type ForeignCommit = {
  sha: string;
  author: string;
  subject: string;
};

export type RunRecord = {
  schema_version?: number;
  project: string;
  task_id: string;
  outcome: Outcome;
  commit_sha?: string | null;
  started_at?: string | null;
  closed_at: string;
  escalation_reason?: EscalationReason | null;
  /**
   * Why the task parked — the code half of Flaitron's `park-reason:`.
   * A plain string, not a union: Flaitron owns that closed set, so an
   * unknown code is data rather than an error. See docs/CONTRACT.md
   * §"Park reason".
   */
  park_reason?: string | null;
  /** Why the picked task was eligible for this run, `<code> — <prose>` shape. */
  eligibility_reason?: string | null;
  hermes_review?: HermesReview | null;
  /**
   * What the reviewer said, clipped like `summary`; the full stdout sits under
   * `$CAOBUNGA_HOME/reviews/`. Absent when no review launched or it said
   * nothing. See docs/CONTRACT.md §"Hermes review".
   */
  hermes_verdict?: string | null;
  /** Absent on the same rule as `hermes_verdict`: no review launched, or it said nothing. */
  hermes_verdict_code?: HermesVerdictCode | null;
  /** Set exactly when `hermes_review` is `degraded` — which review servers the launch lacked. */
  hermes_missing_servers?: string[] | null;
  provider?: string | null;
  model?: string | null;
  plan_model?: string | null;
  /** Groups every record one `caobunga run` wrote. Absent on pre-CBN-EPIC-12.7 records. */
  invocation_id?: string | null;
  next_task?: string | null;
  tasknote_path?: string | null;
  summary: string;
  /**
   * Follow-up lines the closing worker proposed but nobody filed, read from its
   * tasknote. Absent — never `[]` — when the note proposed none, so the panel
   * can distinguish "proposed nothing" from a record written before the field.
   * See docs/CONTRACT.md §"Follow-up capture".
   */
  follow_ups?: string[] | null;
  /** PLAN rows filed while this run was in flight. Absent when none, or unmeasured. */
  new_rows?: NewRow[] | null;
  /** The `[unattended]`-marked set this run's pre-flight read saw. Absent when none, or unmeasured. */
  marked_tasks?: string[] | null;
  /** Where this run's worker stdout/stderr was teed. A path, never contents. */
  log_path?: string | null;
  /** Commits in `start_sha..HEAD` this run does not itself account for. */
  foreign_commits?: ForeignCommit[] | null;
  /**
   * The deterministic suite's result on this close. Present on every
   * `closed` / `de-scoped` / `no-tasknote` record at `schema_version` 2;
   * absent on `blocked` / `failed`, and on v1 records — where absence is
   * unknown, never green. See docs/CONTRACT.md §"Suite".
   */
  suite?: Suite | null;
  /**
   * How the archived tasknote's External review box closed. `missing` is a
   * full-shape note closed without the in-run review. Absent when not read
   * (park, failure, `no-tasknote`, or a pre-CBN-257 record). See
   * docs/CONTRACT.md §"External review".
   */
  external_review?: ExternalReview | null;
  /**
   * Providers a usage-limit refusal ruled out for this invocation, before the
   * run's `provider` took over. Absent when no re-pick ran. See
   * docs/CONTRACT.md §"Run loop" → "Usage-limit re-pick".
   */
  exhausted_providers?: string[] | null;
};

/** Tally by outcome. All five keys always present (zero when unseen). */
export type RunCounts = Record<Outcome, number>;

export type ProjectReports = {
  last_rollup: string | null;
};

export type ProjectDetail = ProjectListItem & {
  /** Detail always carries `denials` + `unparsed_rows` + `marked_tasks`; the list form never does. */
  coverage:
    | (Coverage & { denials: Denial[]; unparsed_rows: UnparsedRow[]; marked_tasks: string[] })
    | null;
  /** Detail always carries the classified lines and the counterfactual. */
  tree:
    | (TreeState & {
        candidate: string | null;
        exempt_task: string | null;
        clean_for: string | null;
        blocking_lines: DirtLine[];
      })
    | null;
  last_run: RunRecord | null;
  runs: RunRecord[];
  run_counts: RunCounts;
  reports: ProjectReports;
};

export type ProjectsResponse = {
  projects: ProjectListItem[];
};

/** Tally by escalation reason. All seven keys always present (zero when unseen). */
export type EscalationCounts = Record<EscalationReason, number>;

/**
 * Derived wall-clock aggregate. `counted` names the contributing sample —
 * records with no `started_at` are excluded, never counted as zero, so the
 * medians are `null` rather than `0` when nothing counted.
 */
export type DurationStats = {
  counted: number;
  median_seconds: number | null;
  max_seconds: number | null;
};

/** One local calendar day that saw runs. Days with none are absent, not zero-filled. */
export type ThroughputDay = {
  day: string;
  runs: number;
  outcomes: RunCounts;
};

/** One routing bucket. `key` is `null` for runs whose field was unset — never dropped. */
export type RoutingRow = {
  key: string | null;
  runs: number;
  outcomes: RunCounts;
  duration: DurationStats;
};

/** Fleet-wide fold over every project's run records (CBN-EPIC-12.7 `GET /api/stats`). */
export type FleetStats = {
  projects: number;
  runs: number;
  /** Distinct `(project, invocation_id)` pairs — a shared stamp across projects counts twice. */
  invocations: number;
  outcomes: RunCounts;
  /** Strictly `outcomes.closed / runs`; `null` when the fleet has no runs. */
  success_rate: number | null;
  duration: DurationStats;
  escalations: EscalationCounts;
  throughput: ThroughputDay[];
  routing: Record<RoutingDimension, RoutingRow[]>;
};

/** Fixed render order for the routing tables, matching docs/CONTRACT.md. */
export const ROUTING_DIMENSIONS = ["provider", "model", "plan_model"] as const;

export type RoutingDimension = (typeof ROUTING_DIMENSIONS)[number];
