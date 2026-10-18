# Caobunga

Multi-project Flaitron orchestrator. Registers approved repos, routes each task to a coding CLI by best-effort leftover usage (today degrades to installed order — no CLI exposes a safe non-interactive usage dump), runs it in a fresh worker session, and reports what committed.

A `caobunga run <slug>` attempt also prints an invocation receipt path and recovery
steps. Receipts include refusals before launch and failed verification after a
committed close. Direct-run exit codes stay compatible, except that a missing
provider or unreadable `git` now exits 5, not 2 (CBN-EPIC-313.5); fleet integrations should
use `loop.execute_invocation(...).successful`, since a red suite after closure can
still exit zero. A receipt a killed run left at `running` becomes `abandoned` on
that slug's next launching run, which reports what landed and what is left.
`observe` stays read-only. See [Invocation results](docs/CONTRACT.md#invocation-results).

## Shared as-is

Caobunga is a personal orchestrator for one operator's repos, published as-is under the [MIT license](LICENSE). It isn't a supported product: expect no roadmap commitments, no stability guarantees, and issues or pull requests answered when there's time. Fork it freely. Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

The `CBN-xxx` and `CBN-EPIC-xxx` citations throughout the code and docs are internal task IDs from the project's own [Flaitron](https://github.com/fakeneuron/flaitron) plan. They aren't public issue numbers.

## Why

Flaitron already keeps one task per context window. The missing piece is the outer loop: which registered project, which next task, which platform still has tokens, which model class that task needs, and a report the operator can read without replaying the worker session.

## Shape

| Layer | Owns |
|-------|------|
| Flaitron (in each repo) | Plan, tasknotes, phases, commit-as-done, next-task cue |
| Worker adapter (local subprocess) | Isolated CLI sessions (Grok Build, Codex, Claude Code, Cursor CLI, …) |
| Caobunga | Registry, best-effort usage routing (degrades to installed order), drain loop, deterministic suite on every close (`just test` / `lint` / `typecheck` → `suite:` on the run record, CBN-EPIC-202.3; `proceed-on-green` continues only on `green`, CBN-EPIC-202.4), run ledger, status dashboard |

Caobunga sits **beside** Fleet HQ, the operator's fleet dashboard and standards repo — not downstream of it. Fleet HQ's facts are about repositories as they exist on disk; caobunga's are about runs it performed. It reads each registered repo's own `.flaitron/PLAN.md`, its archived tasknotes, and its pinned `/ft-task` skill directory directly, and no Fleet HQ surface. Four words mean different things in the two systems — **rollup**, **digest**, **stats**, and **fleet-wide** — so check which one you are reading before merging the numbers: [docs/CONTRACT.md](docs/CONTRACT.md) §"Fleet boundary".

## Status

Flaitron + Fleet HQ overlay landed; active work is tracked in the private repo this snapshot is exported from. Full behavior contract: [docs/CONTRACT.md](docs/CONTRACT.md) — see especially §Run record (schema, escalation/park/eligibility reasons, follow-up capture, new-row detection, suite), §Run loop (observe / gated / proceed-on-green, `--unattended` posture, operator-presence gate), §Worker interface (local-subprocess adapter), §Usage adapters + §Picker (best-effort routing), §Task-class → model map, §Status API + §Status UI, §External review, and §Epic rollup / §Daily fleet digest. Closure ingest (CBN-003): the archived tasknote is the narrative, Caobunga owns the run record.

## Entry points

Two ways in. They differ only in how you name the project — both run one
project at a time, both honor that project's registry tier, and both drive the
same run loop.

| | Use when | Invoke |
|---|---|---|
| **Registry CLI** | You are driving the fleet — any repo, from anywhere, named by its slug | `caobunga run webapp` |
| **In-repo skill** | You are already working in a repo and want caobunga to take the next task *here* | `/caobunga` |

The skill resolves the current git root to a registered slug, tells you the
tier it will honor, and asks before launching anything that is not `observe` —
which is the cautious half of why it exists: typing a slug is deliberate,
invoking a slash command in whatever repo is open is a reflex.

If the repo is not registered, the skill **refuses** and prints the `caobunga
register` command for you to run yourself. It never registers on your behalf.

To run the whole fleet, `caobunga fleet [--jobs N]` runs every enabled
project's `run` side by side, at most N (default 3) at once, each line prefixed
`[slug]`, then prints one receipt. One repo stopping never halts the others, and
there is no resume — run it again and repos with nothing eligible exit at once.
A repo whose `run` exited 0 but whose invocation receipt says it stopped short
(a park, a red suite) reads `incomplete (…)` on the receipt and fails the fleet
(CBN-372). Why it exists now after CBN-124 declined it: [docs/CONTRACT.md](docs/CONTRACT.md)
§"Entry points" (CBN-312).

The registry CLI ships as a console script in `backend/`; `uv sync` puts it in
that project's venv, not on your `PATH`. Install it once so a bare `caobunga`
works from anywhere:

```sh
uv tool install --editable ~/code/caobunga/backend
```

`--editable` is load-bearing: without it the installed CLI drifts from the checkout it orchestrates. `caobunga --version` and `caobunga status` print a `source:` line (and fleet `status` a `hook:` line) so that drift is visible — see [docs/CONTRACT.md](docs/CONTRACT.md) §"Install provenance (CBN-169)".

Skipping the install is fine for a one-off: `uv run --project
~/code/caobunga/backend caobunga <verb>` works from any directory. That is also
the fallback `/caobunga` takes when `command -v caobunga` finds nothing, so it
is the one invocation to reach for rather than the raw
`backend/.venv/bin/caobunga` path.

To use `/caobunga` from another repo, symlink it once:

```sh
ln -s ~/code/caobunga/.claude/skills/caobunga   <repo>/.claude/skills/caobunga
ln -s ~/code/caobunga/.claude/commands/caobunga.md <repo>/.claude/commands/caobunga.md
```

## Intended interface (v1)

```sh
caobunga register <path> [--id SLUG] [--tier TIER] [--replace] [--provider NAME] [--model ID] [--providers LIST]
caobunga projects
caobunga status [project]      # + stale-pin warnings, [unattended] marker coverage, drift, new rows, and target-tree state
caobunga status --seed         # fleet seeding worklist: every repo's obvious unmarked rows + paste-ready sed blocks
caobunga run <project>          # honor registry tier
caobunga fleet [--jobs N]       # run every enabled project, N at a time (default 3), one receipt
caobunga digest                 # daily fleet rollup + follow-ups proposed + PLAN rows filed + model pins
```

The verb set is closed; `/caobunga` adds none — it resolves a slug and calls
`run`.

Dashboard is **status-only** (no start/stop/approve in v1). Pair `just api` + `just dev` locally.

- Read-only HTTP API on `127.0.0.1:8013`: `GET /api/projects`, `GET /api/projects/{id}`, `GET /api/digest`, `GET /api/stats`.
- Vite status UI on `127.0.0.1:5195` (proxies the API): fleet table `/`, project detail `/projects/{id}`, fleet stats `/stats`.

Payload fields, marker coverage, target-tree state, run history, and the Last run panel are specified in [docs/CONTRACT.md](docs/CONTRACT.md) §"Status API" and §"Status UI".

## Local data

Registry, run records, suite logs, and epic rollups live under `~/.caobunga/` (override with `CAOBUNGA_HOME`). Tree and verbs: [docs/CONTRACT.md](docs/CONTRACT.md).

## Defaults

- Explicit registration only
- Execution tier default: `gated` (human in the loop between runs)
- Every launching tier requires the `[unattended]` marker on the PLAN row (CBN-118) — a freshly registered repo with nothing marked dispatches nothing and exits 3 naming each denial; `caobunga status <id>` shows which rows to seed. See [docs/CONTRACT.md](docs/CONTRACT.md) §"Marker coverage"
- Markers can evaporate on a flaitron line rewrite; `caobunga status` reports any row armed then and not dispatching now (CBN-125) — see CONTRACT §"Marker coverage"
- `caobunga status` flags a denied row that says "blocked" in prose the grammar cannot parse (CBN-128), report-only — see CONTRACT §"Marker coverage"
- `[handoff]` after `[model]` declares a mid-run human act (CBN-136); every launching tier denies it, even beside `[unattended]` — see CONTRACT §"Marker coverage"
- Failures escalate to the operator
- Worker platforms (v1): Grok Build, Codex, Claude Code, Cursor CLI
- Review: the worker's own in-run External review (flaitron `CORE-656`), recorded on the run record; the Hermes supervisor retired at CBN-258
- Reports: orchestrator run record (YAML) + archived tasknote as narrative; see [docs/CONTRACT.md](docs/CONTRACT.md)

## Stack

- Backend: Python / uv CLI
- Frontend: Vite status UI
- Execution backend: provider CLIs you already authenticate. `CAOBUNGA_WORKER=local` (one headless subprocess per task) is the only adapter — the `cao` (CLI Agent Orchestrator) adapter was retired in CBN-204 because it could not carry effort
