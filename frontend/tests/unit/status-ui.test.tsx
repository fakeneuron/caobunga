import { cleanup, screen, waitFor } from "@testing-library/react";
import { AxiosError } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import FleetPage from "../../src/pages/FleetPage";
import ProjectDetailPage from "../../src/pages/ProjectDetailPage";
import StatsPage from "../../src/pages/StatsPage";
import FleetTable from "../../src/components/FleetTable";
import type {
  FleetStats,
  ProjectDetail,
  ProjectListItem,
  RunRecord,
  Suite,
} from "../../src/api/types";
import { formatSeconds } from "../../src/lib/duration";
import { renderWithProviders } from "./test-utils";

const sampleProject: ProjectListItem = {
  id: "webapp",
  path: "/Users/dev/webapp",
  tier: "gated",
  enabled: true,
  provider: "grok_cli",
  model: "grok-4.6",
  registered_at: "2026-08-19T00:00:00Z",
  current: {
    task_id: "CBN-041",
    status: "in-progress",
    reason: "open tasknote",
    shortname: "status-ui",
    plan_model: "medium",
  },
  last_run: {
    task_id: "CBN-040",
    outcome: "closed",
    closed_at: "2026-08-19T01:00:00Z",
    summary: "API landed.",
  },
  coverage: { open: 3, marked: 1, denied: 2, unparsed: 0 },
  tree: { readable: true, clean: true, blocking: 0 },
};

const lastRun: RunRecord = {
  schema_version: 1,
  project: "webapp",
  task_id: "CBN-040",
  outcome: "closed",
  commit_sha: "a".repeat(40),
  started_at: "2026-08-19T00:48:00Z",
  closed_at: "2026-08-19T01:00:00Z",
  hermes_review: "reviewed",
  hermes_verdict_code: "accept",
  provider: "grok_cli",
  model: "grok-4.6",
  plan_model: "medium",
  invocation_id: "20260819T004800000Z",
  next_task: "CBN-041",
  tasknote_path: ".flaitron/tasknote/archive/cbn/CBN-040.md",
  summary: "API landed.",
};

/** Same invocation as `lastRun` — the two together are one `caobunga run`. */
const siblingRun: RunRecord = {
  schema_version: 1,
  project: "webapp",
  task_id: "CBN-040b",
  outcome: "closed",
  started_at: "2026-08-19T00:30:00Z",
  closed_at: "2026-08-19T00:47:00Z",
  invocation_id: "20260819T004800000Z",
  summary: "Sibling task in the same drain.",
};

/** Older run that stopped — carries an escalation reason, no start clock, and (being
 *  pre-CBN-EPIC-12.7) no invocation key, so it lands in the ungrouped bucket. */
const failedRun: RunRecord = {
  schema_version: 1,
  project: "webapp",
  task_id: "CBN-039",
  outcome: "failed",
  closed_at: "2026-08-18T09:11:00Z",
  escalation_reason: "timeout",
  hermes_review: "skipped",
  summary: "Worker exceeded the run timeout.",
};

/** Parked run: the worker flipped the note to blocked and said why (CBN-EPIC-14.4). */
const parkedRun: RunRecord = {
  schema_version: 1,
  project: "webapp",
  task_id: "CBN-041",
  outcome: "blocked",
  closed_at: "2026-08-18T08:02:00Z",
  escalation_reason: "worker-parked",
  park_reason: "destructive",
  summary:
    "tasknote parked (status: blocked): needs a git push before the tag can be verified.",
};

const sampleDetail: ProjectDetail = {
  ...sampleProject,
  coverage: {
    open: 4,
    marked: 1,
    denied: 3,
    unparsed: 1,
    denials: [
      {
        task_id: "CBN-042",
        reason: "unmarked — no [unattended] marker on the PLAN line",
        line: 7,
        text: "- [ ] **CBN-042** [medium]🧩 | glyphed — s.",
        seeded_line: "- [ ] **CBN-042** [medium]🧩 [unattended] | glyphed — s.",
        unparsed_blocker: null,
      },
      {
        task_id: "CBN-043",
        reason: "blocked — Blocked by [[CBN-099]]",
        line: 8,
        text: "- [ ] **CBN-043** [heavy] | blocked-row — Blocked by [[CBN-099]].",
        seeded_line: null,
        unparsed_blocker: null,
      },
      {
        // CBN-128: denies as merely unmarked, and would dispatch if seeded —
        // the `Blocked on` shape SPEC's `Blocked by [[ID]]` grammar drops.
        task_id: "CBN-045",
        reason: "unmarked — no [unattended] marker on the PLAN line",
        line: 9,
        text: "- [ ] **CBN-045** [medium]🧩 | blind — Blocked on [[CBN-099]] — do not start.",
        seeded_line: "- [ ] **CBN-045** [medium]🧩 [unattended] | blind — Blocked on …",
        unparsed_blocker: "Blocked on [[CBN-099]] — do not start.",
      },
    ],
    unparsed_rows: [
      {
        line: 12,
        text: "- [ ] **CBN-044** [xheavy]🔭 [unattended] | dropped — glyph then marker.",
      },
    ],
    marked_tasks: ["CBN-041"],
  },
  // CBN-123 F3's live shape: the operator seeded `BE-019`, so the candidate
  // carries no open note, and `DEPLOY-017`'s untracked tasknote — which was
  // exempt until that seeding decision — became the launch blocker.
  tree: {
    readable: true,
    clean: false,
    blocking: 1,
    candidate: "BE-019",
    exempt_task: null,
    clean_for: "DEPLOY-017",
    blocking_lines: [
      {
        text: "?? .flaitron/tasknote/DEPLOY-017.md",
        path: ".flaitron/tasknote/DEPLOY-017.md",
        task_id: "DEPLOY-017",
      },
    ],
  },
  last_run: lastRun,
  runs: [lastRun, siblingRun, failedRun],
  run_counts: {
    closed: 2,
    "de-scoped": 0,
    blocked: 0,
    failed: 1,
    "no-tasknote": 0,
  },
  reports: {
    last_rollup: "# rollup\n",
  },
};

/** Populated fold — two invocations, one legacy record, one unset routing key. */
const sampleStats: FleetStats = {
  projects: 2,
  runs: 5,
  invocations: 2,
  outcomes: {
    closed: 2,
    "de-scoped": 0,
    blocked: 1,
    failed: 2,
    "no-tasknote": 0,
  },
  success_rate: 0.4,
  duration: { counted: 3, median_seconds: 632, max_seconds: 3600 },
  escalations: {
    "note-blocked": 1,
    "worker-parked": 0,
    "worker-launch": 0,
    "worker-error": 0,
    "waiting-user": 0,
    "worker-exited": 0,
    timeout: 1,
  },
  throughput: [
    {
      day: "2026-08-21",
      runs: 3,
      outcomes: {
        closed: 2,
        "de-scoped": 0,
        blocked: 0,
        failed: 1,
        "no-tasknote": 0,
      },
    },
    {
      day: "2026-08-19",
      runs: 2,
      outcomes: {
        closed: 0,
        "de-scoped": 0,
        blocked: 1,
        failed: 1,
        "no-tasknote": 0,
      },
    },
  ],
  routing: {
    provider: [
      {
        key: "grok_cli",
        runs: 3,
        outcomes: {
          closed: 2,
          "de-scoped": 0,
          blocked: 0,
          failed: 1,
          "no-tasknote": 0,
        },
        duration: { counted: 2, median_seconds: 2100, max_seconds: 3600 },
      },
      {
        key: null,
        runs: 2,
        outcomes: {
          closed: 0,
          "de-scoped": 0,
          blocked: 1,
          failed: 1,
          "no-tasknote": 0,
        },
        duration: { counted: 0, median_seconds: null, max_seconds: null },
      },
    ],
    model: [],
    plan_model: [],
  },
};

/**
 * One project, one parked run, fleet-wide. The run record itself never
 * appears on `/stats` — only its `blocked` outcome and `worker-parked`
 * escalation tallies do (CBN-EPIC-16.5: the audit's "parked-run fixture"
 * finding had no dedicated Stats coverage; the parked run's own detail
 * page rendering was already covered by CBN-EPIC-14.4).
 */
const parkedStats: FleetStats = {
  projects: 1,
  runs: 1,
  invocations: 0,
  outcomes: {
    closed: 0,
    "de-scoped": 0,
    blocked: 1,
    failed: 0,
    "no-tasknote": 0,
  },
  success_rate: 0,
  duration: { counted: 0, median_seconds: null, max_seconds: null },
  escalations: {
    "note-blocked": 0,
    "worker-parked": 1,
    "worker-launch": 0,
    "worker-error": 0,
    "waiting-user": 0,
    "worker-exited": 0,
    timeout: 0,
  },
  throughput: [
    {
      day: "2026-08-27",
      runs: 1,
      outcomes: {
        closed: 0,
        "de-scoped": 0,
        blocked: 1,
        failed: 0,
        "no-tasknote": 0,
      },
    },
  ],
  routing: { provider: [], model: [], plan_model: [] },
};

/** The contract's zeroed shape for an empty registry — never `null`, never an error. */
const emptyStats: FleetStats = {
  projects: 0,
  runs: 0,
  invocations: 0,
  outcomes: {
    closed: 0,
    "de-scoped": 0,
    blocked: 0,
    failed: 0,
    "no-tasknote": 0,
  },
  success_rate: null,
  duration: { counted: 0, median_seconds: null, max_seconds: null },
  escalations: {
    "note-blocked": 0,
    "worker-parked": 0,
    "worker-launch": 0,
    "worker-error": 0,
    "waiting-user": 0,
    "worker-exited": 0,
    timeout: 0,
  },
  throughput: [],
  routing: { provider: [], model: [], plan_model: [] },
};

vi.mock("../../src/api/client", () => ({
  fetchProjects: vi.fn(),
  fetchProject: vi.fn(),
  fetchStats: vi.fn(),
}));

import { fetchProject, fetchProjects, fetchStats } from "../../src/api/client";

const mockedFetchProjects = vi.mocked(fetchProjects);
const mockedFetchProject = vi.mocked(fetchProject);
const mockedFetchStats = vi.mocked(fetchStats);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("FleetTable", () => {
  it("renders project rows and links to detail", () => {
    renderWithProviders(<FleetTable projects={[sampleProject]} />);

    expect(screen.getByRole("link", { name: "webapp" })).toHaveAttribute(
      "href",
      "/projects/webapp",
    );
    expect(screen.getByText("CBN-041 (status-ui)")).toBeInTheDocument();
    expect(screen.getByText("API landed.")).toBeInTheDocument();
  });

  it("renders marker coverage as marked/open, and a dash when unreadable", () => {
    // `marked/open` rather than a percentage: the operator seeds a countable
    // number of PLAN lines, and a ratio hides how much work that is.
    renderWithProviders(
      <FleetTable
        projects={[
          sampleProject,
          { ...sampleProject, id: "wiki", coverage: null },
        ]}
      />,
    );

    expect(screen.getByText("1/3")).toBeInTheDocument();
    expect(
      screen.getByTitle("1 marked, 2 denied of 3 open"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("flags unparsed rows in the coverage cell only when any exist", () => {
    // The affix warns the tally is missing grammar-dropped rows (CBN-115);
    // the common clean repo keeps the compact cell.
    renderWithProviders(
      <FleetTable
        projects={[
          {
            ...sampleProject,
            id: "wiki",
            coverage: { open: 2, marked: 2, denied: 0, unparsed: 1 },
          },
        ]}
      />,
    );

    expect(screen.getByText("⚠1")).toBeInTheDocument();
    expect(
      screen.getByTitle(
        "2 marked, 0 denied of 2 open, 1 unparsed row dropped by the grammar",
      ),
    ).toBeInTheDocument();
  });

  it("reports target-tree state beside coverage — clean, dirty, unreadable, dash", () => {
    // CBN-130: before this column a repo could read fully covered here and
    // still refuse its next launch at exit 2, with nothing on the fleet view
    // saying so.
    renderWithProviders(
      <FleetTable
        projects={[
          sampleProject,
          {
            ...sampleProject,
            id: "toolkit",
            tree: { readable: true, clean: false, blocking: 2 },
          },
          {
            ...sampleProject,
            id: "wiki",
            tree: { readable: false, clean: false, blocking: 0 },
          },
          { ...sampleProject, id: "portfolio", tree: null },
        ]}
      />,
    );

    expect(screen.getByText("clean")).toBeInTheDocument();
    expect(screen.getByText("dirty (2)")).toBeInTheDocument();
    expect(
      screen.getByTitle("2 uncommitted changes would refuse the next launch"),
    ).toBeInTheDocument();
    expect(screen.getByText("unreadable")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("shows empty state when no projects", () => {
    renderWithProviders(<FleetTable projects={[]} />);
    expect(screen.getByText(/no registered projects/i)).toBeInTheDocument();
  });
});

describe("FleetPage", () => {
  it("loads and displays the fleet table", async () => {
    mockedFetchProjects.mockResolvedValue({ projects: [sampleProject] });

    renderWithProviders(<FleetPage />, { route: "/", path: "/" });

    expect(
      await screen.findByRole("heading", { name: "Fleet" }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "webapp" })).toBeInTheDocument();
  });
});

describe("ProjectDetailPage", () => {
  it("renders registry, current, last run, and reports", async () => {
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "webapp" }),
    ).toBeInTheDocument();
    expect(screen.getByText("/Users/dev/webapp")).toBeInTheDocument();
    expect(screen.getByText("status-ui")).toBeInTheDocument();
    // Twice by design: the Last run panel and history row 0 are the same record.
    expect(screen.getAllByText("API landed.")).toHaveLength(2);
    expect(screen.queryByText("Last Hermes brief")).not.toBeInTheDocument();
    expect(screen.getByText("# rollup")).toBeInTheDocument();
  });

  it("names every denied task in the marker-coverage section", async () => {
    // Counts alone would still send the operator to the target PLAN to find
    // out which lines to seed — the ids are the point of the detail form.
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Marker coverage" }),
    ).toBeInTheDocument();
    expect(screen.getByText("CBN-042")).toBeInTheDocument();
    expect(screen.getByText("CBN-043")).toBeInTheDocument();
  });

  it("names every unparsed row with its line number and raw text", async () => {
    // The id never parsed, so the PLAN line number + raw text is the only
    // handle the operator gets (CBN-115).
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Marker coverage" }),
    ).toBeInTheDocument();
    expect(screen.getByText("line 12")).toBeInTheDocument();
    expect(
      screen.getByText(
        "- [ ] **CBN-044** [xheavy]🔭 [unattended] | dropped — glyph then marker.",
      ),
    ).toBeInTheDocument();
    // The unparsed row is rendered from its own block, never as a denial —
    // so the reason appears once per *denial* that carries it, and no more.
    const unmarked = sampleDetail.coverage!.denials.filter((d) =>
      d.reason.startsWith("unmarked — "),
    );
    expect(
      screen.getAllByText("unmarked — no [unattended] marker on the PLAN line"),
    ).toHaveLength(unmarked.length);
  });

  it("shows each denied row where it lives and what it should say", async () => {
    // The report is a seeding worklist (CBN-119.3): the line number saves the
    // search, and the suggestion shows `[unattended]` landing after the glyph —
    // flaitron's own placement (CBN-343).
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Marker coverage" }),
    ).toBeInTheDocument();
    expect(screen.getByText("line 7")).toBeInTheDocument();
    expect(
      screen.getByText(
        "- [ ] **CBN-042** [medium]🧩 [unattended] | glyphed — s.",
      ),
    ).toBeInTheDocument();
  });

  it("warns when a row reads as blocked but parsed no clause", async () => {
    // CBN-128: CBN-045 denies as merely `unmarked` and carries a paste-ready
    // seed line, so the worklist would send an operator to arm a row whose own
    // description says not to start it. CBN-123 did exactly that, three times.
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Marker coverage" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Blocked on [[CBN-099]] — do not start."),
    ).toBeInTheDocument();
    expect(screen.getByText(/check before seeding/)).toBeInTheDocument();
  });

  it("warns on no row the grammar already reports as blocked", async () => {
    // CBN-043's `reason` already says `blocked` — a second signal would repeat
    // it, and the exclusion is structural: a parsed clause fills `blocked_by`.
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Marker coverage" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/check before seeding/)).toHaveLength(1);
  });

  it("offers no seeded line for a denial seeding cannot fix", async () => {
    // CBN-043 is blocked, not unmarked — its fix is removing the clause, and
    // suggesting a marker would be the misdirection CBN-114 closed elsewhere.
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Marker coverage" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("blocked — Blocked by [[CBN-099]]"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/\*\*CBN-043\*\*.*\[unattended\]/),
    ).not.toBeInTheDocument();
  });

  it("names the coupling in the target-tree section — labeled line plus counterfactual", async () => {
    // The whole point of CBN-130: `DEPLOY-017.md` is not unexplained dirt, it
    // is dirt *because* the operator seeded `BE-019`. Both halves render — the
    // label on the line, and the candidate the gate would read clean for.
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Target tree" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("?? .flaitron/tasknote/DEPLOY-017.md"),
    ).toBeInTheDocument();
    expect(screen.getByText(/dirt\s+only because the candidate is/)).toBeInTheDocument();
    expect(
      screen.getByText(/gate reads clean only for a dispatch of/),
    ).toBeInTheDocument();
  });

  it("renders a clean target tree without the coupling block", async () => {
    mockedFetchProject.mockResolvedValue({
      ...sampleDetail,
      tree: {
        readable: true,
        clean: true,
        blocking: 0,
        candidate: "BE-019",
        exempt_task: null,
        clean_for: null,
        blocking_lines: [],
      },
    });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Target tree" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/nothing would refuse a dispatch of/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/gate reads clean only for/),
    ).not.toBeInTheDocument();
  });

  it("distinguishes an unreadable git status from a clean tree", async () => {
    // CBN-125's rule: a guard that renders nothing when it cannot answer is
    // indistinguishable from one that checked and found nothing.
    mockedFetchProject.mockResolvedValue({
      ...sampleDetail,
      tree: {
        readable: false,
        clean: false,
        blocking: 0,
        candidate: "BE-019",
        exempt_task: null,
        clean_for: null,
        blocking_lines: [],
      },
    });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Target tree" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/git could not report. A launch would refuse/),
    ).toBeInTheDocument();
  });

  it("renders the coverage null state when the project path is unreadable", async () => {
    mockedFetchProject.mockResolvedValue({ ...sampleDetail, coverage: null });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Marker coverage" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Project path unreadable — no Flaitron PLAN to read."),
    ).toBeInTheDocument();
  });

  it("renders every run in the history with its signals", async () => {
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByRole("heading", { name: "Run history" }),
    ).toBeInTheDocument();
    // runs[0] is the same record the Last run panel shows, so CBN-040 appears twice.
    expect(screen.getAllByText("CBN-040")).toHaveLength(2);
    expect(screen.getByText("CBN-039")).toBeInTheDocument();
    expect(
      screen.getByText("Worker exceeded the run timeout."),
    ).toBeInTheDocument();
    // Duration is derived, not served: 00:48 -> 01:00.
    expect(screen.getAllByText("12m").length).toBeGreaterThan(0);
    expect(screen.getByText("timeout")).toBeInTheDocument();
    expect(screen.getByText("review: skipped")).toBeInTheDocument();
    // The parsed code rides the chip as a tag (the digest's idiom), and the
    // Last run panel spells it out under its own label.
    expect(screen.getByText("review: reviewed [accept]")).toBeInTheDocument();
    expect(screen.getByText("Hermes verdict code")).toBeInTheDocument();
    expect(screen.getByText("accept")).toBeInTheDocument();
  });

  it("renders a degraded review with the servers the launch lacked", async () => {
    // CBN-180: the review ran, but without every review server — the record
    // must never read like a full review, on the chip or in the panel.
    const degradedRun: RunRecord = {
      ...lastRun,
      hermes_review: "degraded",
      hermes_verdict_code: "inconclusive",
      hermes_missing_servers: ["code_git", "code_suite"],
    };
    mockedFetchProject.mockResolvedValue({
      ...sampleDetail,
      last_run: degradedRun,
      runs: [degradedRun],
    });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByText("review: degraded [inconclusive]"),
    ).toBeInTheDocument();
    expect(screen.getByText("Hermes missing servers")).toBeInTheDocument();
    expect(screen.getByText("code_git, code_suite")).toBeInTheDocument();
  });

  it("renders the deterministic suite verdict as a Last-run row and a history chip", async () => {
    // CBN-EPIC-202.5: `suite.verdict` gets the same two surfaces `hermes_verdict_code`
    // already has — a labelled row, and a tinted chip beside the review chip.
    const redSuite: Suite = {
      verdict: "red",
      verbs: [
        { verb: "test", exit_code: 1, duration: 1.0, timed_out: false, tree_changed: false },
        { verb: "lint", exit_code: 0, duration: 0.1, timed_out: false, tree_changed: false },
        {
          verb: "typecheck",
          exit_code: 0,
          duration: 0.2,
          timed_out: false,
          tree_changed: false,
        },
      ],
    };
    const redRun: RunRecord = { ...lastRun, suite: redSuite };
    mockedFetchProject.mockResolvedValue({
      ...sampleDetail,
      last_run: redRun,
      runs: [redRun],
    });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(await screen.findByText("Suite verdict")).toBeInTheDocument();
    expect(screen.getByText("red")).toBeInTheDocument();
    expect(screen.getByText("suite: red")).toBeInTheDocument();
  });

  it("renders a green suite chip without the red tint", async () => {
    const greenSuite: Suite = {
      verdict: "green",
      verbs: [
        { verb: "test", exit_code: 0, duration: 1.0, timed_out: false, tree_changed: false },
        { verb: "lint", exit_code: 0, duration: 0.1, timed_out: false, tree_changed: false },
        {
          verb: "typecheck",
          exit_code: 0,
          duration: 0.2,
          timed_out: false,
          tree_changed: false,
        },
      ],
    };
    const greenRun: RunRecord = { ...lastRun, suite: greenSuite };
    mockedFetchProject.mockResolvedValue({
      ...sampleDetail,
      last_run: greenRun,
      runs: [greenRun],
    });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    const chip = await screen.findByText("suite: green");
    expect(chip.className).toContain("text-slate-400");
    expect(chip.className).not.toContain("text-rose-700");
  });

  it("renders both halves of a park: what stopped the run, and why it parked", async () => {
    // Its own detail rather than a fourth record in `sampleDetail`, so the
    // invocation-grouping cases above keep their exact three-run arrangement.
    mockedFetchProject.mockResolvedValue({
      ...sampleDetail,
      last_run: parkedRun,
      runs: [parkedRun],
      run_counts: {
        closed: 0,
        "de-scoped": 0,
        blocked: 1,
        failed: 0,
        "no-tasknote": 0,
      },
    });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(await screen.findAllByText("worker-parked")).toHaveLength(2);
    expect(screen.getByText("park: destructive")).toBeInTheDocument();
    // The Last run panel spells the code out under its own label.
    expect(screen.getByText("Park reason")).toBeInTheDocument();
    expect(screen.getByText("destructive")).toBeInTheDocument();
  });

  it("renders what a run proposed and what it filed, proposals first", async () => {
    // A drain-parked run: the worker proposed follow-ups and filed one row before
    // it stopped. Both fields are narrative — nothing on the page acts on them.
    const filingRun: RunRecord = {
      ...parkedRun,
      follow_ups: [
        "Bump the pin so `--unattended` reaches `/ft-file-followup`.",
        "Rotate `## Completed` past 150 rows.",
      ],
      new_rows: [
        { task_id: "CBN-014", unattended: false },
        { task_id: "CBN-015", unattended: true },
      ],
    };
    mockedFetchProject.mockResolvedValue({
      ...sampleDetail,
      last_run: filingRun,
      runs: [filingRun],
      run_counts: {
        closed: 0,
        "de-scoped": 0,
        blocked: 1,
        failed: 0,
        "no-tasknote": 0,
      },
    });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(await screen.findByText("Follow-ups")).toBeInTheDocument();
    expect(
      screen.getByText("Rotate `## Completed` past 150 rows."),
    ).toBeInTheDocument();

    // Each row states its own marker state: a bare id would leave the reader
    // inferring it from a count, which fails the moment a run files a mix.
    expect(screen.getByText("New rows")).toBeInTheDocument();
    expect(screen.getByText("CBN-014")).toBeInTheDocument();
    expect(screen.getByText("unmarked")).toBeInTheDocument();
    expect(screen.getByText("CBN-015")).toBeInTheDocument();
    expect(screen.getByText("marked")).toBeInTheDocument();
  });

  it("omits both filing rows entirely when a run carried neither", async () => {
    // The backend normalizes an empty list to `null`, so absence means "proposed
    // none" — an empty labelled row would say neither that nor "not measured".
    mockedFetchProject.mockResolvedValue({
      ...sampleDetail,
      last_run: parkedRun,
      runs: [parkedRun],
    });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(await screen.findByText("Park reason")).toBeInTheDocument();
    expect(screen.queryByText("Follow-ups")).not.toBeInTheDocument();
    expect(screen.queryByText("New rows")).not.toBeInTheDocument();
  });

  it("renders the full five-chip outcome tally including zeros", async () => {
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    // The project-wide row seeds all five keys; group headers show only non-zero
    // chips, so the zeros below appear exactly once each.
    expect((await screen.findAllByText("closed · 2")).length).toBeGreaterThan(
      0,
    );
    expect(screen.getAllByText("failed · 1").length).toBeGreaterThan(0);
    expect(screen.getByText("de-scoped · 0")).toBeInTheDocument();
    expect(screen.getByText("blocked · 0")).toBeInTheDocument();
    expect(screen.getByText("no-tasknote · 0")).toBeInTheDocument();
  });

  it("groups the run history by invocation, legacy records last", async () => {
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    // The invocation stamp heads its group; the two runs it wrote sit under it.
    // Twice by design: the Last run panel also names the record's invocation.
    expect(await screen.findAllByText("20260819T004800000Z")).toHaveLength(2);
    expect(screen.getByText("2 runs")).toBeInTheDocument();

    // CBN-EPIC-12.7 minted the key, so pre-existing records carry none and must
    // still render — under a named bucket rather than being dropped.
    expect(
      screen.getByText(/ungrouped \(no invocation key\)/),
    ).toBeInTheDocument();
    expect(screen.getByText("1 run")).toBeInTheDocument();
    expect(screen.getByText("CBN-039")).toBeInTheDocument();
  });

  it("emits the ungrouped bucket last even when its records sort first", async () => {
    // Records are served newest-first, so a legacy record can lead the list.
    // It must still fall to the bottom rather than splitting the keyed groups.
    const recentLegacy: RunRecord = {
      ...failedRun,
      task_id: "CBN-044",
      closed_at: "2026-08-20T12:00:00Z",
      summary: "Legacy record closed after the keyed ones.",
    };
    mockedFetchProject.mockResolvedValue({
      ...sampleDetail,
      runs: [recentLegacy, lastRun, siblingRun],
      run_counts: {
        closed: 2,
        "de-scoped": 0,
        blocked: 0,
        failed: 1,
        "no-tasknote": 0,
      },
    });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    await screen.findByRole("heading", { name: "Run history" });
    const history = screen.getByRole("heading", {
      name: "Run history",
    }).parentElement!;
    const headers = [
      ...history.querySelectorAll(":scope > section > div > span:first-child"),
    ].map((node) => node.textContent);
    expect(headers).toEqual([
      "20260819T004800000Z",
      "ungrouped (no invocation key)",
    ]);
  });

  it("drops no run when grouping", async () => {
    mockedFetchProject.mockResolvedValue(sampleDetail);

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    await screen.findByRole("heading", { name: "Run history" });
    for (const run of sampleDetail.runs) {
      expect(screen.getAllByText(run.task_id).length).toBeGreaterThan(0);
    }
  });

  it("shows an empty history without dropping the chip row", async () => {
    mockedFetchProject.mockResolvedValue({
      ...sampleDetail,
      last_run: null,
      runs: [],
      run_counts: {
        closed: 0,
        "de-scoped": 0,
        blocked: 0,
        failed: 0,
        "no-tasknote": 0,
      },
    });

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/webapp",
      path: "/projects/:id",
    });

    expect(
      await screen.findByText(/no runs recorded yet/i),
    ).toBeInTheDocument();
    expect(screen.getByText("No run record yet.")).toBeInTheDocument();
    expect(screen.getByText("closed · 0")).toBeInTheDocument();
  });

  it("shows not-found for unknown project", async () => {
    mockedFetchProject.mockRejectedValue(
      new AxiosError("Not Found", "ERR_BAD_REQUEST", undefined, undefined, {
        status: 404,
        statusText: "Not Found",
        data: { detail: "unknown project: missing" },
        headers: {},
        config: {} as never,
      }),
    );

    renderWithProviders(<ProjectDetailPage />, {
      route: "/projects/missing",
      path: "/projects/:id",
    });

    await waitFor(() => {
      expect(screen.getByText(/unknown project: missing/i)).toBeInTheDocument();
    });
  });
});

describe("formatSeconds", () => {
  // CBN-EPIC-12.6 F5: the 1–60m band is where worker runs land, and whole-minute
  // rounding dropped up to 30s there — enough that a fleet median and the rows
  // it aggregates could disagree.
  it("keeps second precision below the hour", () => {
    expect(formatSeconds(172)).toBe("2m 52s");
    expect(formatSeconds(218)).toBe("3m 38s");
  });

  it("omits a zero seconds remainder", () => {
    expect(formatSeconds(720)).toBe("12m");
  });

  it("keeps seconds under a minute and minutes above an hour", () => {
    expect(formatSeconds(45)).toBe("45s");
    expect(formatSeconds(4320)).toBe("1h 12m");
  });
});

describe("StatsPage", () => {
  it("renders fleet totals, chips, and the closed rate", async () => {
    mockedFetchStats.mockResolvedValue(sampleStats);

    renderWithProviders(<StatsPage />, { route: "/stats", path: "/stats" });

    expect(
      await screen.findByRole("heading", { name: "Stats" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Invocations")).toBeInTheDocument();
    // success_rate is served as a 3-place fraction and read as a percentage.
    expect(screen.getByText("40%")).toBeInTheDocument();
    // The fleet chip row seeds all five keys, zeros included.
    expect(screen.getAllByText("blocked · 1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("no-tasknote · 0").length).toBeGreaterThan(0);
    // Fleet duration reuses the shared formatter: 632s -> 10m 32s.
    expect(screen.getByText("10m 32s")).toBeInTheDocument();
    expect(screen.getByText("(3 of 5 counted)")).toBeInTheDocument();
  });

  it("renders all seven escalation chips including zeros", async () => {
    mockedFetchStats.mockResolvedValue(sampleStats);

    renderWithProviders(<StatsPage />, { route: "/stats", path: "/stats" });

    expect(await screen.findByText("note-blocked · 1")).toBeInTheDocument();
    expect(screen.getByText("timeout · 1")).toBeInTheDocument();
    for (const zeroed of [
      "worker-parked",
      "worker-launch",
      "worker-error",
      "waiting-user",
      "worker-exited",
    ]) {
      expect(screen.getByText(`${zeroed} · 0`)).toBeInTheDocument();
    }
  });

  it("renders throughput newest-day-first and every routing dimension", async () => {
    mockedFetchStats.mockResolvedValue(sampleStats);

    renderWithProviders(<StatsPage />, { route: "/stats", path: "/stats" });

    expect(await screen.findByText("2026-08-21")).toBeInTheDocument();
    expect(screen.getByText("2026-08-19")).toBeInTheDocument();
    expect(screen.getByText("grok_cli")).toBeInTheDocument();
    // All three dimensions get a section even when a dimension has no rows.
    expect(
      screen.getByRole("heading", { name: "Routing — provider" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Routing — model" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Routing — plan model" }),
    ).toBeInTheDocument();
    // A routing row whose duration counted nothing shows the dash, never `0s`.
    expect(screen.queryByText("0s")).not.toBeInTheDocument();
  });

  it("renders a parked-run fixture — worker-parked escalation and blocked outcome both surface nonzero", async () => {
    mockedFetchStats.mockResolvedValue(parkedStats);

    renderWithProviders(<StatsPage />, { route: "/stats", path: "/stats" });

    expect(await screen.findByText("worker-parked · 1")).toBeInTheDocument();
    // The fleet chip row and the one throughput day both show the same tally.
    expect(screen.getAllByText("blocked · 1").length).toBeGreaterThan(0);
  });

  it("renders the zeroed empty-fleet body without crashing", async () => {
    mockedFetchStats.mockResolvedValue(emptyStats);

    renderWithProviders(<StatsPage />, { route: "/stats", path: "/stats" });

    expect(
      await screen.findByRole("heading", { name: "Stats" }),
    ).toBeInTheDocument();
    // null success_rate and null durations render as the dash idiom, not `0`.
    expect(screen.getByText("(0 of 0 counted)")).toBeInTheDocument();
    expect(screen.getByText("closed · 0")).toBeInTheDocument();
    expect(screen.getByText("timeout · 0")).toBeInTheDocument();
    // Empty throughput and all three empty routing dimensions: four null states.
    expect(screen.getAllByText(/no runs recorded yet/i)).toHaveLength(4);
  });

  it("surfaces a fetch failure as an error state", async () => {
    mockedFetchStats.mockRejectedValue(new Error("Network Error"));

    renderWithProviders(<StatsPage />, { route: "/stats", path: "/stats" });

    await waitFor(() => {
      expect(screen.getByText(/network error/i)).toBeInTheDocument();
    });
  });
});
