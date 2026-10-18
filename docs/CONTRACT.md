# Caobunga — v1 product contract

**Status:** Stable — see `git log -- docs/CONTRACT.md` for field/feature provenance (the inline task-ID enumeration this line used to carry was retired at CBN-141: hand-maintained, guarded by no test, and chronically stale — see CBN-138.N Finding 2). Implement against this file; do not re-open field names in later epics without a `schema_version` bump.

This is the typed *surface* (fields, enums, verbs) plus the on-disk tree under `~/.caobunga/` (CBN-010). The Hermes brief and review (CBN-004 / CBN-030) retired at CBN-258. Last-run card is CBN-033 (caller-side since CBN-188.2). Status API is CBN-040.

See [VISION.md](../VISION.md) for purpose and non-goals.

**Size budget (CBN-271.N).** This file states the rules; the measurement, incident history, and rejected options behind them live in `docs/decisions/*.md`, reached by a one-line pointer beside the rule they back. `backend/tests/test_contract.py` fails once this file passes **30,000 words** (28,746 after CBN-EPIC-271's split, down from 35,636). When an edit trips it, move the argument to a decisions file rather than raising the ceiling.

## Non-negotiables

- Explicit registration only. No auto-scan of `~/code`.
- One task, one fresh worker session. Worker transcripts are disposable and never stored on a report surface. A usage/session-limit refusal that left a clean tree at an unmoved HEAD is a picker signal, not a session that ran the task (CBN-213); the eventual launch is still one worker session per close.
- v1 dashboard is read-only (no start / stop / approve).
- v1 failure policy: escalate to the operator. No automatic retries. Carve-out (CBN-213): on a clean-tree, HEAD-unchanged (CBN-260) `worker-error` / `worker-exited` whose stdout/stderr tail (CBN-282) carries a usage/session-limit refusal, treat that provider as `leftover=exhausted` **for this invocation**, re-pick the next allow-list member, and only then escalate. A hard `register --provider` pin does not pivot. Same-worker relaunch on a dirty tree stays forbidden (CBN-212). Leftover-dump wiring (`usage.DUMP_ARGV`) is a different seam and is not this overlay.
- First-ship tiers that must work: `observe` and `gated`. `proceed-on-green` runs as of CBN-050; `plow` ran from CBN-050 to CBN-134, which retired it (§"Execution tiers").
- Flaitron stays the work SSOT. Caobunga does not grow a second task system.

## Execution tiers

| Value | Meaning | v1 |
|---|---|---|
| `observe` | Read plan + last run record; do not launch workers | yes |
| `gated` | One next task → write run record → stop for the operator | yes |
| `proceed-on-green` | Continue while outcomes are `closed` **and** each close's suite is `green` (CBN-EPIC-202.4) | yes (CBN-050) |

Default on `register`: `gated`.

**Every launching tier requires the `[unattended]` marker (CBN-118).** The rows are a ladder for *how much* runs before the loop stops, and nothing else. They are not a ladder for *what may be picked*: `gated` and `proceed-on-green` both consult the marker (§"Flaitron reader"), because both dispatch a **headless** worker — `worker_message` sends `--unattended` / `--fast` at every tier (§"Run loop"), and `gated`'s stop lands *after* the worker's closure commit, not before it. `observe` asks nothing because it launches nothing.

A freshly registered repo with no markers therefore dispatches nothing at any tier and exits 3 naming every denial; `caobunga status <id>`'s coverage block (§"Marker coverage") names exactly the rows to seed. That refusal is the intended first-contact experience (bias-to-refuse). The default stays `gated` — the fix for the *pick* is seeding, not a different default. Why, what CBN-EPIC-18.4 measured, and the two rejected alternatives: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Execution tiers — marker on every launching tier".

The enum is closed. Unknown values are a user error. Failures escalate to the operator; no automatic retries (CBN-213's in-invocation usage-limit re-pick is the one carve-out §"Non-negotiables" names).

**`plow` was retired (CBN-134).** What it was, and what was lost: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Execution tiers — `plow` retirement (CBN-134)". **Migration:** a registry record still carrying `tier: plow` fails to load (registry reads are deliberately strict, §"On-disk tree"); re-register it with `caobunga register <path> --tier proceed-on-green`, which updates `tier` in place and keeps pins and `registered_at` (§"Registry"). Scoping a drain to one cohort is done by marking only that cohort's rows.

## Registry

One record per registered project. Identity is a slug `id`, not the path.

### Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | int | yes | `1` |
| `id` | string | yes | `[a-z][a-z0-9-]*`. From the directory basename, lowercased, unless `--id` is passed. |
| `path` | string | yes | Absolute filesystem path at register time. Must exist and be a directory. |
| `tier` | enum | yes | See tiers. Default `gated`. |
| `provider` | string or omit | no | Provider-token pin (`register --provider`; §"Usage adapters" for the vocabulary). Omit = picker decides. |
| `model` | string or omit | no | Runtime CLI name pin (`register --model`, requires provider). Omit = taskmap. |
| `providers` | list of string or omit | no | Allow-list the picker may choose from, best-first (`register --providers`). Omit = every installed worker is eligible. Conflicts with `provider`. |
| `registered_at` | ISO-8601 UTC | yes | Set at register. |
| `enabled` | bool | yes | Default `true`. Disabled projects are listed but never launched. |

`id` is unique across the registry. Re-registering the same `id` with a different `path` is a user error unless `--replace` is passed. Re-registering the same `path` under a new `id` is allowed. Same `id` and same `path` updates `tier` (and `provider` / `model` / `providers` when those flags are passed) without `--replace` and keeps `registered_at`. A `--tier`-only re-register does not wipe existing pins or an existing allow-list. `--model` without `--provider` is a usage error, and so is `--provider` together with `--providers` (CBN-091): a pin beats even an exhausted worker, an allow-list is a preference inside an eligibility filter, and ranking one over the other would be a guess.

`providers` tokens are validated against the v1 provider set at register time; an unknown token is a usage error. The stored list is stripped, blank-free, and deduped keeping first position — declared order is semantic. An empty declaration normalizes to omitted, so a blank flag value cannot brick a project into "no eligible worker".

That validation set is **worker-mode-neutral by design** (CBN-097): `register` never narrows the allow-list to what one adapter happens to support. A declared token with no `RECIPES` row (§Worker interface → `local`) registers cleanly and then fails at launch with a `UserError` (exit 2) naming the providers the adapter can run. Why: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Registry — worker-mode-neutral provider validation (CBN-097)".

### Register identity

```text
caobunga register ~/code/webapp              →  id: webapp
caobunga register ~/code/webapp --id charts  →  id: charts
caobunga register ~/code/webapp-v2 --id webapp --replace
```

Basename is taken after resolving symlinks. If the basename is not a legal `id`, `--id` is required.

Re-registering the same path merges into the existing record: `--tier` always applies, `registered_at` is preserved, and the provider pin / allow-list follow the mutual-exclusion rule in §Picker — each clears the other. **"Same path" is compared case-insensitively**, the rule §"Entry points" already states for the in-repo skill ([decisions/registry-run-record.md](decisions/registry-run-record.md) §"Registry — case-insensitive path match (CBN-131)"). The stored spelling is the one first registered; a case-only re-register updates the record, it does not rewrite the path. `--replace` is only for taking an `id` that is registered at a *genuinely different* path — it rewrites `registered_at` and drops the pins, so it is never the remedy for a case difference.

### On-disk layout

Default home is `~/.caobunga/`. Override with `CAOBUNGA_HOME` (tests and alternate roots). The directory is created on first write. Do not auto-scan `~/code`.

```text
$CAOBUNGA_HOME/
  lock
  run-locks/{repo_key}.lock
  suite.lock
  projects/{id}.yaml
  invocations/{YYYYMMDDTHHMMSSmmmZ}-{random}.yaml
  runs/{id}/{YYYYMMDDTHHMMSSmmmZ}-{task_id}.yaml
  suites/{id}/{YYYYMMDDTHHMMSSmmmZ}-{task_id}.log
  rollups/{id}/{YYYYMMDDTHHMMSSmmmZ}-{epic_key}.md
  status/{id}.md
  logs/{id}/{invocation_id}-{task_id}.log[.1]
```

`lock` is an `fcntl.flock` exclusive lock covering registry, run, suite-log, rollup, and status writes. `run-locks/{repo_key}.lock` is its per-repo sibling — `repo_key` is the sha256 hex of a resolved, case-folded path (case-folded per the `_same_repo` rule, CBN-131). The path hashed is the record path's git toplevel (`git rev-parse --show-toplevel`), or the record path itself when git cannot name one, so two ids registered at one repo, or at nested paths of one checkout, share it (CBN-EPIC-313.2, CBN-EPIC-313.6). Registration identity still compares record paths, not toplevels. The run lock is a **non-blocking** `flock` held across one whole `run` invocation (see Run loop), so contention fails fast instead of queuing behind an hours-long worker session; a crashed run leaves no stale lock because `flock` dies with the process. `suite.lock` is the third: **blocking** and global like `lock`, but held around each verb's spawn in one close's deterministic suite (§"Run loop" → "Deterministic suite", CBN-308, CBN-310), so its minutes never stall a write. Each `projects/{id}.yaml` is one registry record. Run-record filenames use UTC-compact `closed_at` so they sort by time. Epic rollup artifacts (CBN-031) land under `rollups/` when a PLAN epic cohort completes. A home that predates CBN-258 may still hold `briefs/{id}/` and `reviews/{id}/` — the retired Hermes brief and the reviewer's stdout, paired by the run's stamp (CBN-148). Nothing writes or reads them now, and Caobunga does not delete its own history; they are the operator's to remove. `status/{id}.md` is the one-per-project last-run card (§"Last-run card") — no timestamp in the name, since each gated close overwrites it rather than appending.

`logs/{id}/{invocation_id}-{task_id}.log` (CBN-162) is a `local` worker's stdout/stderr tee, written only by `LocalWorker`. Not one of `os.replace`'s temp-then-swap writes like the rest of this tree: it is appended to live, by a background thread, while the worker runs, and capped at `local.LOG_MAX_BYTES` with a single `.1` rotation on overflow rather than replaced atomically. Diagnostics only — a `worker-exited` or `timeout` escalation's `summary` already carries the last stderr and stdout lines (§"Escalation reasons"; stdout added CBN-200) that matter from it, and no reader (digest, dashboard) opens the file itself; the run record's `log_path` (when set) is a pointer for an operator, not a second report surface.

`suites/{id}/{YYYYMMDDTHHMMSSmmmZ}-{task_id}.log` (CBN-EPIC-202.3) is the loop-side deterministic suite's full rendered output for one close — each fleet verb's argv, exit, duration, tree delta, and output tail, rendered by `SuiteResult.render` — under the run's own `closed_at` stamp and name, so `runs/` and `suites/` pair by filename on the CBN-148 rule with no pointer field. The record's `suite` field (§"Suite") carries the fold and one clipped line per verb; this file carries the rest, because the deny-list forbids raw CLI stdout on every report surface and no reader (digest, dashboard) opens it. Written after the record, since the stamp is minted there; it exists only when a suite ran, which is exactly when the record carries `suite`.

Writers use a temp file plus `os.replace`. Run records and the last-run card stay out of the worker repo: `Store.save_status` (CBN-033; caller-side since CBN-188.2) overwrites `status/{id}.md` (last-run markdown, not the YAML ledger) beside `runs/`. No git step and nothing lands in the target repo.

## Run record

Written by the orchestrator at Flaitron 🏁 (after the closure commit exists). The archived tasknote is the narrative; this record is the machine ingest (CBN-003).

One YAML file per closed run. Do not append to a ledger in v1. Do not commit run-record YAML into the worker repo — nothing of caobunga's lands there (§"Last-run card").

Path: `$CAOBUNGA_HOME/runs/{project}/{YYYYMMDDTHHMMSSmmmZ}-{task_id}.yaml` (see On-disk layout).

**Reading is skip-not-fail.** A file under `runs/{project}/` that cannot be read as a record — unparseable YAML, valid YAML that is not a mapping, a mapping that fails the schema, or bytes that are not UTF-8 or cannot be opened (CBN-EPIC-314.3) — is treated as *absent* rather than raised (CBN-106). One unreadable file degrades a read by one row; it does not fail it. Same policy the Flaitron reader applies to malformed tasknote frontmatter (CBN-EPIC-7.4).

The skip is silent: nothing reports which file was dropped. Registry reads are deliberately **not** lenient in the same way — a corrupt registry file still raises ([decisions/registry-run-record.md](decisions/registry-run-record.md) §"Run record — skip-not-fail, and why the registry is strict (CBN-106)").

### Fields

| Field | Type | Required | Source | Why |
|---|---|---|---|---|
| `schema_version` | int | yes | orchestrator | `2` since CBN-EPIC-202.2 (`suite` required on every closure outcome); `1` on records written before it, which load unchanged. See §"Compatibility" |
| `project` | string | yes | registry `id` | Project identity is not in the tasknote |
| `task_id` | string | yes | PLAN / worker close | Flaitron ID (`CBN-003`, `CORE-034`, …) |
| `invocation_id` | string | no | orchestrator | Groups every record one `caobunga run` wrote. See Invocation below |
| `outcome` | enum | yes | orchestrator | See outcomes below |
| `commit_sha` | string | if `outcome` is `closed` | 🏁 | SHA does not exist at archive time |
| `started_at` | ISO-8601 UTC | no | orchestrator | Clock when the run began. Duration is `closed_at - started_at`, derived by readers rather than stored — storing both endpoints keeps the start queryable and cannot disagree with itself |
| `closed_at` | ISO-8601 UTC | yes | orchestrator | Clock at write |
| `escalation_reason` | enum | if `outcome` is `failed` / `blocked` | orchestrator | Which stop site ended the run. See escalation reasons below |
| `park_reason` | string | no | parked tasknote frontmatter | *Why the task parked*, when the worker said. The code half of Flaitron's `park-reason:`. See park reason below |
| `eligibility_reason` | string | no | orchestrator | *Why the picked task was eligible* for this run, `<code> — <prose>`. See eligibility reason below |
| `hermes_review` | enum | no | orchestrator | **Legacy (CBN-258): read, never written.** Whether this close got a Hermes review. See Hermes review below |
| `hermes_verdict` | string | no | Hermes stdout | **Legacy (CBN-258).** *What the reviewer said* — its stdout whitespace-collapsed and clipped like `summary`. The full answer is the `reviews/` copy. See Hermes review below. Omit when no review launched or it said nothing |
| `hermes_verdict_code` | enum | no | Hermes stdout, parsed | **Legacy (CBN-258).** `accept` / `reject` / `inconclusive` — the reviewer's fixed first line, parsed. See Hermes review below. Omit on the same rule as `hermes_verdict`: no review launched, or it said nothing |
| `hermes_missing_servers` | list of string | no | Hermes stdout, parsed | **Legacy (CBN-258).** The `REVIEW_MCP_SERVERS` names Hermes reported unknown on this launch. See Hermes review below. Set exactly when `hermes_review` is `degraded`; omit otherwise |
| `provider` | string | if a worker launched | orchestrator | What the worker launched |
| `model` | string | if a worker launched | orchestrator | Runtime model, not PLAN `[model]` |
| `plan_model` | string | no | PLAN.md `[model]` token | Routing stats; omit if the line had none |
| `next_task` | string | no | validated `**Suggested next task:**`, else the candidate | Read at *this run's* dispatch posture (CBN-117). Omit when that posture has nothing eligible left |
| `tasknote_path` | string | no | repo-relative | Omit when `outcome` is `no-tasknote` |
| `summary` | string | yes | Final Summary lede, or PLAN stub | Operator-readable one-liner (a leading `Doc-drift…` paragraph is skipped); never a transcript. Capped at 500 chars, truncated on a word boundary with a trailing `…` |
| `follow_ups` | list of strings | no | archived tasknote `### Follow-up…` section | *What this run proposed doing next that nobody filed.* See follow-up capture below. Omit when the note proposed none |
| `marked_tasks` | list of strings | no | orchestrator | *Which rows were armed when this run was dispatched* — the `[unattended]`-marked set the run's pre-flight read saw. The baseline `caobunga status` diffs a later read against. See marked-set baseline below |
| `new_rows` | list of `{task_id, unattended}` | no | orchestrator | *Which PLAN rows appeared while this run was in flight* — the ids at close minus the ids its pre-flight read saw, each with its literal `[unattended]` marker state. See new-row detection below. Omit when nothing appeared or no baseline was passed |
| `log_path` | string | no | orchestrator | Where this run's worker stdout/stderr was teed (§Worker interface → `local`). A pointer, not a transcript — omit when no `local` worker ever reached `send` |
| `foreign_commits` | list of `{sha, author, subject}` | no | orchestrator | *Which commits landed in `start_sha..HEAD` this run does not itself account for* — everything but the closure/park commit. See foreign-commit detection below. Omit when nothing else landed in the window |
| `suite` | `{verdict, verbs}` | if `outcome` is `closed` / `de-scoped` / `no-tasknote` (at `schema_version` 2) | orchestrator | *What the deterministic suite said about this close* — `just test` / `lint` / `typecheck`, folded to one `verdict`. See Suite below. Forbidden on `blocked` / `failed`; absent on v1 records, where absence is unknown rather than green |
| `external_review` | enum | no | archived tasknote `**External review**` box | *Whether this close ran the in-run External review* — `ticked` / `na` / `missing`. See External review below. Omit when no archived note was read |
| `exhausted_providers` | list of string | no | orchestrator | *Providers a usage-limit refusal ruled out* (CBN-286), sorted — the record half of the CBN-213 re-pick. Report-only. Omit when no re-pick ran |

### Follow-up capture

`follow_ups` surfaces work a worker proposed in its own archived tasknote, which no other surface reads ([decisions/registry-run-record.md](decisions/registry-run-record.md) §"Follow-up capture — why the field exists (CBN-120.2)").

**Read at close, from the archived note — and, under a drain, from a parked one (CBN-138.4).** `close.inspect_close` already opens the archived tasknote for the `summary` lede; the follow-up read rides that same open, on the same path. `drain` decides the parked case: `read_parked_follow_ups` reads the **open** note in place when the dispatch posture was a drain, and an attended close is unchanged. An unreadable note — missing, unopenable, or not UTF-8 — degrades to `None` rather than raising. Why a drain differs from `gated`: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Follow-up capture — parked notes under a drain (CBN-138.4)".

**Heading match is a prefix family, not one spelling.** Any `###` heading whose text begins with `Follow-up` / `Follow-ups`, case-insensitive, opens a section; the next `#`/`##`/`###` closes it. **`### Follow-ups proposed` is the canonical spelling to write**, but not the only one read: requiring one spelling would be caobunga defining a producer's format (§"Fleet boundary"). The spellings measured in the archive: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Follow-up capture — heading family, not one spelling". The heading must be section-initial, so `### The cross-repo follow-up could not be filed from here` is prose about a follow-up rather than a section of them.

**The unit is a list item** (`-` / `*` / `+` / `N.`), each collapsed to one line and clipped at the same 500 chars `summary` gets. Items **inside fenced blocks count** — the load-bearing case, not an edge one ([decisions/registry-run-record.md](decisions/registry-run-record.md) §"Follow-up capture — heading family, not one spelling"). Fence state is tracked only so a `###` inside a fence cannot open a section and a delimiter cannot close one; `flaitron.fence_mask` is shared with the PLAN reader so the two cannot disagree about where a block ends. Whole list capped at 10.

**Table rows are a documented non-target.** Why, and its measured cost: same decisions section as above.

**A prose-only section correctly reports nothing.** A `**None.**` body reports zero; the section body is never captured.

**Narrative, not a decision.** Nothing dispatches, denies, routes, or refuses on this field, and Caobunga never files a follow-up into a PLAN — filing stays the operator's act, the same boundary §"Marker coverage" draws around marker seeding. `null` rather than `[]` when a note proposed none, so `exclude_none` keeps the key off records with nothing to say; the normalizer follows `RegistryRecord.providers` (an empty declaration is undeclared). Optional at `schema_version` 1 per §"Compatibility" — records written before it load unchanged.

**Asking is not requiring, and the prompt now asks (CBN-138.2).** `worker_message` cues the worker to file follow-ups via `/ft-file-task --unattended` (unprobed since the flaitron v7 hard cut, CBN-330); the read side is unchanged by it — `_FOLLOWUP_HEADING` is still the tolerant prefix family, and a worker that ignores the cue is captured exactly as before. `### Follow-ups proposed` remains a caobunga-originated spelling — one caobunga may *ask its own dispatched worker* for (the pre-v7 degraded cue did, until CBN-330 retired it), and may not *require* of any producer. Why asking is not a boundary violation: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Follow-up capture — asking is not requiring (CBN-138.2)". The cue rides every prompt shape (§"Run loop", §"Continue prompt") because every launching tier dispatches headless (CBN-118).

### Marked-set baseline

The `[unattended]`-marked set the run's **pre-flight** read saw — the queue state this dispatch was decided from, the same read that produced the candidate and its `eligibility_reason`. It is `Coverage.marked_tasks` (§"Marker coverage") copied onto the record, and it exists for exactly one consumer: `caobunga status`'s marker-drift diff (CBN-125). Under `proceed-on-green` each close writes its own record, so "the last run's marked set" is the most recent iteration's, not the invocation's first.

**This is not the cached classification the constitution rules out.** Current coverage is still derived per read on every surface (§"Marker coverage"); this field records what **one past run observed**, read as history by exactly one caller. Why that distinction holds: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Marked-set baseline — not a cached classification".

`null` rather than `[]` when nothing was recorded, so `exclude_none` keeps the key off records with nothing to say and a reader cannot confuse "recorded an empty marked set" with "recorded none" — only one of those is a state the diff can act on. It shares `follow_ups`' normalizer for that reason. Optional at `schema_version` 1 per §"Compatibility"; records written before it load unchanged and simply have no baseline. `observe` writes no run record at all, so an `observe` project never acquires one.

### New-row detection

A worker cued to file a follow-up (§"Follow-up capture" → *Asking is not requiring*) writes it into the target's own `PLAN.md`. §"Follow-up capture" closed the half where a discovery was *proposed* in a tasknote nobody reads; this closes the half where it was actually **filed** and still nothing said so. `close.inspect_close` diffs the task ids its close-side `read_project` returns against `baseline` — the id set the run's **pre-flight** read saw — and the difference rides the record as `new_rows` (CBN-138.3).

**The baseline is passed in, never re-read.** It comes off the same `read_project` call that produced the candidate, its `eligibility_reason`, and `marked_tasks`, so the diff is against the queue this dispatch was actually decided from rather than a second read that could disagree with it. `null` for an absent or empty baseline — *not measured*, which is a different state from *measured and empty*, the same distinction §"Marked-set baseline" draws.

**Every parsed row counts, not just the open launch queue** — a row filed into `## Future Opportunities` is still a discovery. State it, refuse nothing. Rows appear in `parse_plan` document order rather than dispatch order, because these are rows to read, not a worklist to seed down.

**`unattended` is the literal marker, not the `eligibility` predicate.** The live dispatch question about the same rows is answered by `coverage` on the next read. Why both choices differ from `marker_drift` / `coverage`: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"New-row detection — scope and marker choice (CBN-138.3)".

**Expect `unmarked` to equal the count** — a worker-filed row is unmarked by construction, unless the worker wrote the marker itself, which the drain pin (CBN-246) refuses to dispatch in the same invocation ([decisions/registry-run-record.md](decisions/registry-run-record.md) §"New-row detection — why `unmarked` equals the count (CBN-138.3, CBN-246)").

**Carried on a park too.** A worker that filed a row and then parked still filed it, so the `worker-parked` snapshot carries the same diff.

**Narrative, not a decision.** Nothing dispatches, denies, routes, refuses, or seeds on this field, and Caobunga still files nothing into a PLAN. Optional at `schema_version` 1 per §"Compatibility"; records written before it load unchanged. `NewRow` lives in `models.py` rather than `flaitron/` on the boundary `park_reason` already draws — the record layer mirrors a producer's vocabulary instead of importing the producer reader.

### Foreign-commit detection

`loop.py` takes `start_sha` before launch and `close.py` compares `HEAD` against it to find the run's own closure or park commit. At the same point `inspect_close` resolves `commit_sha` (the closure commit on a completed run, the commit that moved `HEAD` on a park), `git.list_foreign_commits` lists every other commit in `start_sha..HEAD`, and the difference rides the record as `foreign_commits` (CBN-172). Motivating incident: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Foreign-commit detection — motivating incident (CBN-172)".

**Two exclusions (CBN-299).** The commit this run already attributes to itself is excluded by SHA. A *filing* is excluded by shape: a window commit that changes only `.flaitron/PLAN.md` and adds only task rows, every one in `new_rows` (§"New-row detection"), is already accounted for. It leaves `foreign_commits` and is echoed `filed: <sha> <author> <subject>` (`close.split_window_commits`, `finish._echo_filed_commits`) — snapshot-only, not a record field. Anything the match cannot place, or any window with no baseline, stays foreign. Neither exclusion inspects *who* committed.

**Report-only, the same posture as `tree` (`git.tree_after`) and `unparsed` (§"Marker coverage").** State it, refuse nothing — a foreign commit is evidence for the operator's post-mortem, not a reason to discard the worker's work. Echoed one `foreign: <sha> <author> <subject>` line per commit (`finish._echo_foreign_commits`), the same shape `unparsed` rides.

**`null` for an empty range or an unreadable one.** No foreign commits are reported when `HEAD` never moved off `start_sha`, or when `git log` itself failed — the same "not measured" shape §"New-row detection" draws for an absent baseline. `ForeignCommit` lives in `models.py` beside `NewRow` on the same boundary.

### Invocation

An **invocation** is one `caobunga run <project>` — one `execute_run` call. `gated` writes one record; `proceed-on-green` writes one per drained task. Why: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Invocation — why the grouping key (CBN-EPIC-12.6)".

`invocation_id` is a `compact_utc` stamp (`YYYYMMDDTHHMMSSmmmZ`) minted once at invocation start — the same vocabulary as the run / suite / rollup filenames, so it reads as a clock and sorts before every record it groups. It is a grouping key, not a uniqueness constraint. `observe` writes no record and mints nothing. Every record a single invocation writes carries the same value, including the second same-path write that records `hermes_review`.

Optional at `schema_version` 1 per §"Compatibility": records written before it load unchanged, and `exclude_none` keeps the key invisible until populated. Readers must treat an absent key as *unknown*, never as its own group.

No field stores a worker transcript, a session dump, or a usage payload. Usage stays in the adapters (CBN-020, landed).

### Outcomes

| Value | When |
|---|---|
| `closed` | Flaitron closed the task and a closure commit landed |
| `de-scoped` | Relevance was De-scope and Phase 4 closed without the original work |
| `blocked` | Tasknote parked (`status: blocked`) |
| `failed` | Worker or orchestrator failed; escalate |
| `no-tasknote` | PLAN row closed with no tasknote (CBN-001 shape) |

`commit_sha` is required only for `closed`. Other outcomes may include a SHA if one exists; omit rather than invent.

### Escalation reasons

Set when — and only when — `outcome` is `failed` or `blocked`. `summary` keeps the human-readable detail; this field is the machine-readable half.

| Reason | When |
|---|---|
| `note-blocked` | The candidate's open tasknote was already `status: blocked`; nothing launched. **No longer written since CBN-252** — the headless pick denies a parked row as `parked` (§"Flaitron reader") — kept so historical records still load and `/stats` still counts them |
| `worker-parked` | The worker flipped the tasknote to `status: blocked` mid-run |
| `worker-launch` | `Worker.launch` or `Worker.send` failed |
| `worker-error` | The worker reported `error` (also the fallback for an unclassified orchestrator failure) |
| `waiting-user` | The worker is waiting for a user answer. **No longer written since CBN-265** — the retired `cao` adapter was the only source of the underlying worker status; kept so historical records still load |
| `worker-exited` | The worker exited without closing the task |
| `timeout` | `CAOBUNGA_RUN_TIMEOUT` elapsed while waiting for the close |

The record model asserts only that the field is **absent** unless the run stopped. The mirror rule — present whenever it stopped — is enforced at the write sites, deliberately not by the model: run records written before this field existed carry `failed` / `blocked` with no reason, and rejecting them at load would break `Store.list_runs`.

**`worker-exited`, `timeout`, and `closed` fold a tree snapshot into `summary` (CBN-171, CBN-184).** `git.tree_after` appends one `tree: N file(s) left (…)` line to `summary`, reusing the `git status --porcelain -uall` read and `DIRT_SAMPLE` clip, plus whether the candidate's own `.flaitron/tasknote/{task_id}.md` is still open. The same fold applies to a `closed` (and `de-scoped`) outcome's summary. The line is the tree as it stands once the worker is stopped: the worker is shut down after settle and suite, and the line is read again just before the record is written. Anything the suite's own verbs left counts too, since it is the same dirt the next dispatch refuses on (CBN-352). Why: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Escalation reasons — tree snapshot fold (CBN-171, CBN-184)". Report-only in all three cases: the *next* invocation's dirty-tree refusal stays `_refuse_if_dirty`'s own fresh read, unaffected by this field. The other four escalation reasons carry no tree line. A `worker-exited` summary also names a mismatch (CBN-328) when the candidate's open tasknote carries a `park-reason:` but its `status:` is not `blocked` — `park-reason present but status in-progress, not blocked`, placed ahead of the tree line. Report-only: the exit stays `failed` / `worker-exited`, never a park.

### Park reason

Flaitron's `--unattended` posture parks a tasknote instead of firing a gate into
an empty session (§"Run loop"), writing `park-reason: <code> — <prose>` into the
note's frontmatter. `park_reason` records the **code** half of that value; the
prose folds into `summary`, the same division §"Escalation reasons" draws.

The two fields answer different questions and neither replaces the other.
`escalation_reason` is Caobunga's answer to *what stopped this run* — still
`worker-parked` (or, on records written before CBN-252, `note-blocked`), unchanged by this field. `park_reason` is
Flaitron's answer to *why the task parked*, quoted rather than reinterpreted.

The type is **string, not enum, deliberately**. Flaitron's codes at the pinned
SPEC version are `drift`, `destructive`, `prerequisite`, `model-mismatch`,
`input-needed`, `dependency`, and `interrupted`, but that set is Flaitron's to
change ([decisions/registry-run-record.md](decisions/registry-run-record.md) §"Park reason — why a string, and the one park site"). Readers must not assume the set above is exhaustive, and an unknown code is data,
not an error.

Set on the mid-run classification of a note that came back `blocked`; records from before CBN-252 may also carry it from the retired pre-launch refusal. Reading is
skip-not-fail like the rest of the frontmatter (§"Flaitron reader") — a missing
or non-string value costs the reason, never the candidate, and prose that breaks
the note's YAML (a `: ` in an unquoted scalar) is read line-wise rather than
costing the park (CBN-186). A value with no separator is a bare code.

```yaml
schema_version: 1
project: caobunga
task_id: CBN-042
invocation_id: 20260826T094500000Z
outcome: blocked
started_at: 2026-08-26T09:45:00Z
closed_at: 2026-08-26T09:58:12Z
escalation_reason: worker-parked
park_reason: destructive
tasknote_path: .flaitron/tasknote/CBN-042.md
summary: >-
  tasknote parked (status: blocked): needs a `git push` to the public remote
  before the tag can be verified
```

### Eligibility reason

Why the task this record covers was eligible for the pick that dispatched it
(CBN-EPIC-17.2). Same `<code> — <prose>` shape as `park_reason`, split the same
way. Codes at this version: `marked` (carried the `[unattended]` marker under an
unattended-tier pick) and `attended` (the tier does not consult the marker —
`gated`). The selection preference (`in-progress-override` / `spec-selection`)
is deliberately not folded in: that is the reader candidate's `reason` field,
which stays off the record unchanged.

**Denies never reach a run record** — a denied task never launches, so it has no
record to carry one. Deny reasons are echoed at each pick (`eligibility: {id}
deny: unmarked — …`) and enumerated in the exit-3 message when a drain has
nothing eligible to start (§"Run loop"). Every decision therefore lands on
exactly one surface: allows on the record they produced, denies on the
invocation output that skipped them.

This is **not** the picker why-log, which stays off every report surface
(§"Picker"; [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Eligibility reason — not the picker why-log"). Optional at
`schema_version` 1 per §"Compatibility"; records written before it load
unchanged.

### Hermes review

**Retired (CBN-258, 2026-09-24).** The loop writes none of the four `hermes_*` fields; they stay on the model as optional legacy keys so old records still load, and the digest (§"Daily fleet digest") and status UI render them where present. No schema bump (§"Compatibility"). The Hermes review was replaced by §"External review"; for the verdict and its measurement, see [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Hermes review — retire verdict (CBN-242, CBN-258)".

What the legacy values meant, for reading old records:

| Review | When |
|---|---|
| `reviewed` | The Hermes review launched for this close and returned, with every reviewer MCP server registered |
| `skipped` | The later-tier observation window was not due (`CAOBUNGA_HERMES_EVERY_N` / `CAOBUNGA_HERMES_EVERY_SECONDS`); a drain-end flush could later upgrade it to `reviewed` |
| `degraded` | The review launched and returned, but the launch profile lacked one or more reviewer MCP servers; `hermes_missing_servers` names which (CBN-180) |

`hermes_verdict` is the reviewer's stdout, clipped like `summary` (CBN-148); the full stdout sat under `reviews/`. `hermes_verdict_code` is its fixed first line `VERDICT: accept | reject | inconclusive — <one sentence>`, parsed (CBN-160):

| Verdict code | Meaning |
|---|---|
| `accept` | The reviewer's first line named `accept` |
| `reject` | The reviewer's first line named `reject` |
| `inconclusive` | The reviewer's first line named `inconclusive`, **or** did not match the fixed shape at all — skip-not-fail |

None of the four ever gated dispatch.

### Suite

The loop-side deterministic suite's result on one close (CBN-EPIC-202.2). The fleet verbs — `just test`, `just lint`, `just typecheck`, the three `suite_verbs.VERBS`, under `suite_verbs.TIMEOUTS` — are run against the committed close and folded to one `verdict`. Filled by `suite.run_suite` (§"Run loop" → "Deterministic suite"), it is the first record field a dispatch decision reads (§"Run loop" → "Suite gate"). Why structured, and its rollout: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Suite — why a structured field (CBN-EPIC-202.2)".

**Required exactly when a closure commit exists, forbidden otherwise.** `suite` is present on `closed` / `de-scoped` / `no-tasknote` and absent on `blocked` / `failed` — a park or a failure leaves no closed tree to measure. `close.inspect_close` sets `commit_sha` on all three closure outcomes unconditionally (the closure commit, §"Run loop" → "Continue prompt" close-detection steps), so the outcome set and "a closure commit exists" are one predicate (`models.CLOSURE_OUTCOMES`). The model asserts **both** directions at `schema_version` 2, which is what the bump buys (§"Compatibility"): on a v2 close, a missing `suite` is a record that was never written, never "not measured". Records at `schema_version` 1 predate the field and load unchanged; a reader that needs a v1 record's suite treats absence as *unknown*, never as `green`. It rides the **single** write — the suite runs after the worker settles and before the record is written (§"Run loop" → "Deterministic suite", CBN-EPIC-202.3) — so no rewrite adds it later.

| Verdict | When |
|---|---|
| `green` | Every verb returned exit `0` |
| `red` | At least one verb returned a nonzero exit — named in `verbs`. The fold stops here even if another verb timed out or was refused |
| `timeout` | No verb was red, and at least one hit its `suite_verbs.TIMEOUTS` ceiling |
| `refused` | No verb was red or timed out, and at least one could not run — no `justfile`, `just` off `PATH`, a path outside the registry, or a justfile present but missing that verb's recipe (CBN-211: `just` exits 1 with `justfile does not contain recipe`, and `run_verb` reclassifies that one exit as `refused` — a fleet-layer gap, not a code failure; see [decisions/registry-run-record.md](decisions/registry-run-record.md) §"Suite — missing recipe reclassified as `refused` (CBN-211)"). Only just's own error naming the verb's recipe reclassifies; a recipe that exists and fails by calling a missing sub-recipe stays `red`. A drain that cannot measure its suite stops on this like a red one (CBN-EPIC-202.1 D5) |

Precedence is `red` → `timeout` → `refused` → `green`. `Suite.fold` is the one fold, `Suite.from_verbs` the constructor a runner uses, and the model rejects a stored `verdict` that does not match its `verbs` — a runner bug cannot write `green` over a red verb. All three verbs always run, no short-circuit on a red `test`, so the record names every red verb; the extra cost is bounded by the `lint` + `typecheck` ceilings.

`verbs` carries one entry per fleet verb, in the fixed order `test` → `lint` → `typecheck`. `exclude_none` keeps `exit_code`, `refused`, and `detail` off the record when they have nothing to say, the rule every optional field follows:

| Verb field | Type | Required | Why |
|---|---|---|---|
| `verb` | string | yes | The fleet verb (`test` / `lint` / `typecheck` — Fleet HQ's `STACK-TENDENCIES.md` §"Command interface"), not the MCP tool name. A string, not an enum: the verb set is the fleet's to change (§"Fleet boundary") |
| `exit_code` | int | if the verb returned one | Omitted exactly when `timed_out` or `refused`. The model rejects a verb with neither a code nor a reason, so nothing folds to `green` by silence |
| `duration` | float | yes | Seconds, wall clock |
| `timed_out` | bool | yes | The verb hit its ceiling and was killed |
| `refused` | string | if refused | Why the verb could not run, verbatim from the runner. Omitted otherwise; a blank string is omitted too |
| `tree_changed` | bool | yes | The porcelain differed before and after the verb. Report-only: the next launch's dirty-tree refusal is the enforcement |
| `detail` | string | no | The last non-blank output line that is not a `>` / `›` script echo (npm's `> tsc --noEmit`, a recipe's `› frontend: tsc`), clipped like `summary` — pytest's and ruff's summaries trail. A swift-testing `Test run with 0 tests …` trailer yields to the last XCTest `Executed N tests` line above it, and stands only when there is none (CBN-354); a trailer with a non-zero count is a result and stays. Omitted when every line is blank or an echo. Never the tail: the full rendered output is the `suites/` log's job (CBN-EPIC-202.3), and the deny-list forbids raw CLI stdout on every report surface |

### Suite fitness preflight (CBN-215)

`register` and `observe` (§"CLI verbs", §"Run loop") each print one `suite-fitness: …` line — echo-only, like `eligibility:` / `next:`, never a run-record field and never a refusal. `suite_verbs.probe_fitness` checks the same two structural facts `run_verb`'s `_refusal` checks (a justfile among `JUSTFILE_NAMES`, `just` on `PATH`) and, when both hold, runs `just --summary` (read-only, `SUMMARY_TIMEOUT` 10s — never `just test` / `lint` / `typecheck` themselves) to list recipe names and check `FLEET_VERBS` (`test`, `lint`, `typecheck`, derived from `suite_verbs.VERBS`) against them by exact name, no aliases:

| Line | When |
|---|---|
| `suite-fitness: refused — no justfile at {path}` | No `justfile` / `Justfile` / `.justfile` at the repo root |
| `suite-fitness: refused — \`just\` is not on PATH` | No `justfile` check passed, but `just` is not installed |
| `suite-fitness: refused — \`just --summary\` exited {n}: {detail}` | A justfile exists, `just` is installed, but `--summary` itself failed (a syntax error, for instance) |
| `suite-fitness: unreachable — missing recipe(s): {names} (has: {names})` | `--summary` succeeded but one or more of `test` / `lint` / `typecheck` is absent |
| `suite-fitness: fit — test/lint/typecheck recipes present` | All three recipes are present; whether they pass is a different question the actual suite run answers |

This is a structural snapshot at register/observe time, not a guarantee — a `fit` repo can still close `red` ([decisions/registry-run-record.md](decisions/registry-run-record.md) §"Suite fitness preflight — why (CBN-205, CBN-211, CBN-215)").

### External review

Flaitron's Phase 3 `**External review**` box (`CORE-656`) is where a runner records that a context other than the author graded the diff. `external_review` records how the box closed (CBN-257), so a runner that skips the review shows up as a value rather than as silence. Why the field exists: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"External review — why the field exists (CBN-257)".

**Read at close, from the archived note**, on the same `git show` that `summary` and `follow_ups` ride (§"Follow-up capture"). `flaitron.external_review_box` classifies it:

| External review | When |
|---|---|
| `ticked` | The box is ticked and its disposition does not lead with `N/A`, nor does the first `External review:` line below the box in its section — a review ran, whatever it found — or a micro-tasknote's `**External review:**` line carries a filled disposition that does not lead with `N/A` |
| `na` | The box is ticked and its disposition leads with `N/A`, after the label or before it (`- [x] N/A (De-scope) — **External review** …`; bold or italic `N/A` counts) (the SPEC's "diff too small to grade"), **or** the box is ticked and the first unfenced `External review:` line below the box, in the same `##` section (Testing Notes), leads with `N/A` (CBN-337) — `External review: N/A — …` opening a line (list item or bold label) or a sentence, never quoted in inline code, **or** a full note with no box has, as its first unfenced `External review:` line inside a `## … Phase 3` section, one that leads with `N/A` (CBN-341), **or** a micro-tasknote's `**External review:**` line (flaitron v7, CBN-330) leads with `N/A`, **or** the note is a micro-tasknote (`## ⚡ Notes`, no phase headings) with no such line — pre-v7 micro and retired `/ft-micro-task` `## ✅ Recap` notes — which carries no box by design |
| `missing` | The box is unticked, or a full-shape note carries no box at all and no boxless Phase 3 `N/A` line (CBN-341), or a micro-tasknote's `**External review:**` line is empty or still the template's `<…>` placeholder |

`na` keys on the disposition's *leading* token, after the label and its separator (`— N/A: …`, with or without backticks around the `N/A`) ([decisions/registry-run-record.md](decisions/registry-run-record.md) §"External review — classification choices (CBN-257)"). The first box outside a code fence wins. A box whose text *opens* with an unbolded `External review` label (`- [x] External review — N/A: …`) counts as the box only inside a `## … Phase 3` section, so a Subtasks checklist line is never the box (CBN-341). A `de-scoped` close whose box reads `missing` records `na` instead. An archived note that `git show` cannot read records nothing. Set only on `closed` / `de-scoped`; a park, a failure, and a `no-tasknote` close have no archived note, and absence there is "not read", never `missing`.

**Dispatch never starts a micro note (CBN-338).** Every prompt shape in §"Run loop" carries `--unattended` or `--fast` and never `--micro`, and no PLAN tag maps to it. Flaitron makes `--micro` an operator's explicit opt-in, and `[light]` is a model tier, not a ceremony tier. A micro note reaches this reader only after an operator opted in. That means a hand run, or a continue prompt on an `in-progress` micro note the operator started. A parked one is denied at pick (`parked`, CBN-252), so dispatch never resumes it. Why: [decisions/registry-run-record.md](decisions/registry-run-record.md) §"External review — micro notes and dispatch (CBN-338)".

**Narrative, not a decision.** Nothing dispatches, denies, or routes on it. Optional at `schema_version` 2 per §"Compatibility" — added without a bump, so records written before it load unchanged and read as not measured. Surfaces: a `review` column on the `status` fleet table (§"CLI verbs"), a `[review: missing]` digest tag (§"Daily fleet digest"), and the key on `/api/projects/{id}`'s `last_run` / `runs` (§"Status API"). The web panel does not render it yet.

### Example

```yaml
schema_version: 2
project: caobunga
task_id: CBN-003
invocation_id: 20260818T234107000Z
outcome: closed
commit_sha: 0123456789abcdef0123456789abcdef01234567
started_at: 2026-08-18T23:41:07Z
closed_at: 2026-08-19T00:00:00Z
provider: grok
model: grok-4.6
plan_model: heavy
next_task: CBN-002
tasknote_path: .flaitron/tasknote/archive/cbn/CBN-003.md
summary: >-
  The archived Flaitron tasknote is the narrative report, not the machine ingest.
suite:
  verdict: green
  verbs:
    - verb: test
      exit_code: 0
      duration: 98.4
      timed_out: false
      tree_changed: false
      detail: 769 passed in 95.9s
    - verb: lint
      exit_code: 0
      duration: 3.1
      timed_out: false
      tree_changed: false
      detail: All checks passed!
    - verb: typecheck
      exit_code: 0
      duration: 6.6
      timed_out: false
      tree_changed: false
      detail: 0 errors, 0 warnings, 0 informations
```

## Entry points

Caobunga has two entry points. They differ in **how a project is named**, and
in nothing else — both run one project at a time, both honor that project's
registry `tier`, and both drive the same run loop (§"Run loop").

| Entry point | Project addressed by | Invoked from | Surface |
|---|---|---|---|
| Registry CLI | a registry slug — `caobunga run webapp` | anywhere | `caobunga` on `PATH` once installed, else `uv run --project <caobunga>/backend caobunga` (§"CLI verbs", and below) |
| In-repo skill | the current working directory | inside the repo | `/caobunga` (CBN-EPIC-12.5) |

**`caobunga` reaches `PATH` by an install step, not by checkout (CBN-132).**
`[project.scripts]` in `backend/pyproject.toml` puts the console script in that
project's venv; `uv tool install --editable <caobunga>/backend` is what puts it
on `PATH`, and `README.md` §"Entry points" carries the command. Until that is
run, every verb in §"CLI verbs" is reached as `uv run --project
<caobunga>/backend caobunga <verb>` — the same fallback `/caobunga` Step 0
takes, and the invocation to prefer over the raw `backend/.venv/bin/` path. `--editable` is
part of the step ([decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Entry points — install step and the in-repo skill's refusal (CBN-132, CBN-EPIC-12.5)").

The in-repo skill resolves the current git root against `projects/{id}.yaml`
(§"On-disk layout"), read-only, comparing resolved real paths
case-insensitively (CBN-131). The slug
comes from the matched filename, never from the directory basename, because
`--id` may differ from it (§"Register identity"). On a hit the skill reports
the tier, confirms with the operator for any tier other than `observe`, and
shells out to `caobunga run <slug>`.

**On a miss it refuses.** It reports the repo as unregistered, prints the
`caobunga register` command, and stops. It does not register, and does not
offer to — "explicit registration only" (§"Non-negotiables") governs the write.

**No verb was added for this.** The skill calls the existing `run`, so the
closed v1 verb set below is unchanged.

"Entry point" is not a tier (either entry point honors any tier), a worker mode (`CAOBUNGA_WORKER`), or a Surface: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Entry points — what an entry point is not".

The batch entry point is `caobunga fleet` (CBN-312), a verb of the registry CLI,
not a widened `run`: `run` still requires exactly one `<project>`, as §"CLI
verbs" states. `fleet` spawns one child `caobunga run <slug>` per **enabled**
registered project, in registry (id) order, at most `--jobs` (default 3) alive at
once. Each child is an ordinary `run` — its own per-repo lock, tier, drain, and
exit code — so `fleet` reads no PLAN, dispatches nothing itself, and derives no
scope over the `[unattended]` marker. Serial within a repo is `run`'s own drain.
One child's exit never halts or skips another; there is no resume state — a
re-run spawns again and `run`'s exit 3 skips a repo with nothing eligible. Child
output streams live, one line at a time, prefixed `[slug] `; child stdin is
closed. After every child exits, one receipt — a row per project (`id`, `exit`,
`meaning`, `duration`) and a tally line — and the fold in §"Exit codes".
A child that exits `0` is checked against the last invocation receipt it names on
its `invocation-record:` line (§"Invocation results"), loaded by id from this
home's store. When that receipt reads `successful: false` (a park, a red suite),
the row's meaning is the receipt's `<status> (<stop_reason>)`, e.g. `incomplete
(outcome)`. A receipt that will not load reads `receipt unreadable`. A child that
names no receipt (`observe`) keeps its exit-code meaning (CBN-372).
Ctrl-C cancels the projects not yet started, never spawning them, and waits for
the running children, which got the same interrupt; no receipt prints.
A leg whose read of its child's output raises kills and reaps that child before
the error becomes the leg's `unexpected` row (CBN-EPIC-314.4).

**Reopened (CBN-312).** CBN-124 declined a batch entry point and recorded its
reopen condition: two or more repos dispatchable at the same time, as `caobunga
status` reports. On 2026-10-08 it did — several clean repos with marked rows —
and CBN-308 / CBN-310 had already serialized concurrent slugs' suite verbs on
`suite.lock`. Evidence, the shape choices, and how each CBN-124 ground is
answered: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Entry points — batch entry point reopened (CBN-312)".

### Self-drive

Caobunga's own repository is registered like any other project — explicitly,
with `caobunga register`, at a path the registry stores without knowing what
lives there. Resolution, the run loop, and both entry points treat it as an
ordinary record. There is no `--self` flag, no bootstrap hook, and no
auto-registration on first run: that would put a registry write inside the tool,
which "explicit registration only" (§"Non-negotiables") forbids regardless of
which repo is being written.

What *is* contractual is not the record's existence — registry contents are
per-machine operator state — but the one constraint that applies to this record
and to no other: **its tier is `gated`.**

`gated` is the stance here, not merely the `register` default — a drain would
keep running modules a worker has since edited. Mechanism: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md)
§"Self-drive — why `gated`, and why no guard".

**Recorded, not enforced.** `register` does not special-case this path, and v1
adds no check. Why no guard: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md)
§"Self-drive — why `gated`, and why no guard".

## CLI verbs

v1 verb set is closed. New verbs are a contract change — `fleet` is one (CBN-312).

| Verb | Args | Effect |
|---|---|---|
| `register` | `<path> [--id SLUG] [--tier TIER] [--replace] [--provider NAME] [--model ID] [--providers LIST]` | Write a registry record. Default tier `gated`. `--replace` overwrites the same `id` at a new `path`. `--provider` / `--model` pin the picker; omit = picker decides. `--providers` is a comma-separated allow-list, best-first, and conflicts with `--provider`. Same id+path keeps existing pins and allow-list unless these flags are passed. Always prints one `suite-fitness: …` line after (§"Suite fitness preflight"). |
| `projects` | (none) | List registered projects (`id`, `path`, `tier`, `enabled`). |
| `status` | `[project]` | One project: last run record + tier + enabled + a `pins:` block flagging stale `taskmap.MODELS` ids per installed provider (§"Task-class → model map" → "Stale-pin guard (CBN-166)") + marker coverage (§"Marker coverage"), the denied rows named with their PLAN line and correctly-placed marker, in seeding order, one `drift:` line diffing the current marked set against the last run's baseline, one `new rows:` block naming the PLAN rows the last run filed and their marker state (§"New-row detection"), and one `tree:` block carrying the operator-presence verdict (§"Target-tree report"). No arg: one `source:` line (§"Install provenance", CBN-169) — path plus `hook: present` / `hook: missing` (CBN-216) — above the fleet table, then one row per project with a `marked/open` coverage column, affixed with unparsed and drifted counts when nonzero, a `tree` column (`clean` / `dirty (N)` / `unreadable`), a `suite` column — the last run's deterministic suite verdict (`green` / `red` / `timeout` / `refused`, dash idiom when absent, §"Suite", CBN-EPIC-202.5) — and a `review` column — how the last run's External review box closed (`ticked` / `na` / `missing`, dash idiom when absent, §"External review", CBN-257). The fleet table carries no pins column. `status --seed` (fleet form only; with a project arg it is a usage error, exit 2) prints the `source:` line and then, in place of the table, the fleet seeding worklist (§"Marker coverage", CBN-348). Read-only. |
| `run` | `<project>` | Honor that project's `tier`. `observe`: refresh status, do not launch. `gated`: one next task in a fresh worker session, write a run record, last-run card, stop. `proceed-on-green`: same per-task runner in a loop (see Run loop). |
| `fleet` | `[--jobs N]` | `run` every enabled project as a child process, at most N (default 3) at once, output prefixed `[slug]`; one receipt after (§"Entry points", CBN-312). |
| `digest` | (none) | Daily fleet rollup over run records for the current local calendar day (§"Window"), then a `## Model pins` footer carrying the stale-pin guard's `pins:` lines (CBN-256). Print-only markdown. |

`run` requires `<project>`; the batch form is `fleet`, which runs every enabled project rather than picking one. Fleet-wide “pick any project with leftover usage” is out of v1.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (including `observe` status-only and `gated` after a written run record) |
| `2` | Usage / validation (bad id, unknown project, unknown tier, missing `--id`, a concurrent run on the same repo, a dirty target repo, a `HEAD` lost after the worker launched — `head-lost`, CBN-322) |
| `3` | No next task (PLAN exhausted or nothing eligible) |
| `4` | Worker or orchestrator failure — escalate |
| `5` | Unlaunchable — this machine cannot launch a worker: no installed or pickable provider, or a target whose `git` hung or is missing (CBN-EPIC-313.5) |
| `1` | Unexpected |

`fleet` folds its children's codes: `4` when any child exited `4`, `5`, `1`, or anything else outside {`0`, `2`, `3`}, or exited `0` over an unsuccessful or unreadable receipt (§"Entry points", CBN-372); otherwise `0`. A child's `2` (dirty tree, held lock, disabled) or `3` (nothing eligible) is a skip — the receipt names it, the fleet does not fail on it. A child's `5` is not: the receipt names it `unlaunchable`, because a missing provider or `git` is the machine's state, not the repo's, and would otherwise read as a green fleet ([decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Entry points — environment failures are not skips (CBN-EPIC-313.5)"). A bad `--jobs` is `fleet`'s own `2`.

### Output

Human-readable on a TTY (rich tables are fine). `--json` is not required in v1. After a gated `run`, print the written run record as YAML (`exclude_none`). `digest` prints the fleet rollup, not a worker log.

### Install provenance (CBN-169)

`caobunga --version` prints the version, then one `source:` line naming where the running package resolves from: `editable <path>` when the package directory sits under a git checkout at its expected `backend/caobunga` layout, else `snapshot <path>` — the classification `install_provenance()` (`caobunga/__init__.py`) makes by walking up from the package directory for a `.git` entry whose relative layout matches. The incident and the CBN-173 false positive: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Install provenance — why the `source:` line exists (CBN-169)". `status` with no arg prints the same line above the fleet table, affixes `hook: present` or `hook: missing` after the path (whether this install can run CBN-212's `python -m caobunga.hooks` PreToolUse command — CBN-216), and on `snapshot` appends the one-line fix: `reinstall: uv tool install --editable backend --force (from the caobunga repo root)`. `--version` omits both the hook affix and the reinstall hint.

## Surfaces

| Surface | Reads | Must not see |
|---|---|---|
| CLI | run record + registry | worker transcript |
| Last-run card (CBN-033, caller-side since CBN-188.2) | last-run markdown card at `$CAOBUNGA_HOME/status/{project}.md` | worker transcript; run-record YAML |
| Dashboard (CBN-040 / CBN-041) | projects, current/last run, last reports | start/stop/approve; transcripts |

Ingest split (CBN-003): parse `**Final Summary:**` … `**Archived:**` from the archived note when a recap is needed. Do not treat `git log --grep` as a SHA index.

## Fleet boundary

Caobunga is not the only system reading this fleet. The operator's fleet standards and dashboard repo is the **Fleet HQ** — it discovers every repository on disk and publishes catalogs, rollups, and stats over them. Its own `docs/FLEET-ARCHITECTURE.md` assigns caobunga the **Actuator** tier, and four of the words in this document mean something else over there.

**Caobunga reads producers directly and reads no Fleet HQ surface.** Each registered project's own `.flaitron/PLAN.md`, its archived tasknotes, and one skill directory — `/ft-task` (the posture probe, CBN-108) — under `.flaitron/core/` when the submodule exists, or under `claude/skills/` when it does not (CBN-156, flaitron-self) — and nothing else (§"Flaitron reader"). Not Fleet HQ's catalog, not its stats, not its pulse, not a key in an emitted `data/fleet-*.json`. No adapter or reader here may join on a Fleet HQ shape, and the registry (§"Registry") may disagree with Fleet HQ's inventory without that being drift ([decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Fleet boundary — reads no Fleet HQ surface").

Caobunga's own code writes nothing into a producer; the one write that lands there is the task commits its worker session makes — work, not a fleet-level claim, which the actuator carve-out in Fleet HQ's `FLEET-ARCHITECTURE.md` §"One-way fact flow" sanctions. Fleet HQ's rule that it never writes back into a producer is unchanged, and the carve-out is not transferable: it licenses no fleet-level fact written into a producer. The last-run card is not a second sanctioned write: it is a caller-side file (§"Last-run card"), never committed into the worker repository. The retired commit that once carried it: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Fleet boundary — the retired write outward (CBN-033 → CBN-188)".

**Fleet HQ reads this store, and this paragraph is the producer's half of that record.** Since Fleet HQ `NAT-293` (2026-09-19), its `scripts/fleet-runs.sh` sweeps `$CAOBUNGA_HOME/runs/{project}/*.yaml` — the §"On-disk layout" path and the §"Run record" shape — into a tenth fleet surface, `docs/FLEET-RUNS.md`, and its `data/fleet-runs.json`. It joins on six fields and nothing else: `outcome`, `closed_at`, `task_id`, `provider`, `hermes_verdict_code`, and `suite.verdict`, plus the length of `marked_tasks`. Since CBN-258 `hermes_verdict_code` is legacy — no longer written, so absent on every newer record; its successor signal is `external_review` (§"External review"); it reads the files, not `GET /api/stats`, so a running server is not part of its input. Renaming, retyping, or moving any of those is a change to that sweep's input; why the note lives on both ends, and why the two folds over this ledger stay independent: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Fleet boundary — Fleet HQ reads the run store (NAT-293)". Its reader skips-not-fails on the same rule §"Run record" sets (CBN-106), and reads a schema-1 record's missing `suite` as *unmeasured*, never green.

**Fleet HQ may drive `register`, and this paragraph is the owner's half of that record (Fleet HQ `NAT-326`).** Its `docs/FLEET-ARCHITECTURE.md` §"One-way fact flow" names an *owner-CLI batch* as one form of its operator-confirmed fleet recipe: on one operator confirm of the repo list, Fleet HQ may call `caobunga register <path>` once per repo. It is not a batch `register` verb (none is added — `fleet`, CBN-312, batches `run` only); `register` stays the only writer and validator of the registry (§"Register identity"), the operator's confirm is the opt-in, and "Do not auto-scan `~/code`" still binds this side. Why this fits CBN-124: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Fleet boundary — Fleet HQ drives `register` (NAT-326)".

**Caobunga reads flaitron's tasknote template, and this paragraph is the reader's half of that record (CBN-235).** Two readers here parse the body of a tasknote, not just its frontmatter, and both hook headings or labels `docs/EXTERNAL-AGENTS.md` §"Stable surfaces for callers" now lists (v5.33.0). `phase_progress()` in `caobunga/flaitron/tasknote.py` (§"Run loop", CBN-214) relies on the four `## <emoji> Phase N: <Name>` headings of `templates/tasknote-template.md` — an optional single leading token, the literal `Phase N:`, and a name whose first word is the slug it echoes — on `- [x]` / `- [ ]` boxes at column 0 outside a fenced block (CBN-346) beneath each one up to the next `## ` outside a fenced block (CBN-344), and on the micro template's `## ⚡ Notes` heading. `external_review_box()` (§"External review", CBN-257) relies on the full template's Phase 3 `- [ ] **External review**` box — the bolded label matched literally, its disposition read from whatever follows, then from the first `External review:` line below the box in the same section (CBN-337); an unbolded label opening a box, and a boxless Phase 3 `N/A` line, are tolerated drift reads (CBN-341) — and on the same phase-vs-`## ⚡ Notes` headings to tell a micro note (no box by design) from a full note that dropped one. Moving, renaming, or renumbering any of those is a change to this side's input. Caobunga reads the producer's format and defines none of it: a heading the template does not write is not a shape this side invents. The re-verify point is the pin bump (§"Parse contract"): `backend/tests/test_contract.py` asserts the vendored templates still match the reader regexes, so a template move fails the suite the way a `**Version:**` move does. The retired third reader and the `CORE-655` landing: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Fleet boundary — tasknote-template read history (CBN-235)".

### Name collisions

Caobunga's facts are about **runs it performed**. Fleet HQ's are about **repositories as they exist on disk**. The two never answer the same question, so a reader who merges a pair gets a wrong answer rather than a vague one.

| Term | Here (`caobunga`) | Fleet HQ |
|---|---|---|
| **rollup** | `rollups/{project}/…` — one per-project epic rollup, folded from this orchestrator's run records when a PLAN cohort completes (§"Epic rollup") | `FLEET-ROLLUP.md` — what closed across every discovered repository in a rolling 30-day window, read from archived tasknote `**Final Summary:**` ledes |
| **digest** | `caobunga digest` / `GET /api/digest` — one local calendar day of run records, across registered projects (§"Daily fleet digest") | `PROJECT-CATALOG.md` §"Fleet digest" — a four-column view over the catalog's own entries |
| **stats** | `GET /api/stats` and the `/stats` page — outcomes, throughput, routing, durations, escalations, folded over run records (§"Status API") | `FLEET-STATS.md` — per-repository code metrics: size, language mix, comment density, activity, test ratio |
| **fleet-wide** | every project registered with `caobunga register` | every repository Fleet HQ's `discover-fleet.sh` finds on disk |

**Nothing is renamed, on either side.** This table is the reciprocal of Fleet HQ's record, not a rename, and goes stale only if a sense itself changes. Why: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Name collisions — why nothing is renamed".

## Status API

Read-only HTTP surface for the v1 dashboard (CBN-040). The UI is CBN-041. No start / stop / approve. Not a CLI verb — serve with `just api` / `python backend/main.py`. The Status API adds no registry or run-record field and bumps no `schema_version`.

Bind: `127.0.0.1:8013`. CORS is scoped (CBN-EPIC-7.3) to the two Status UI origins — `http://127.0.0.1:5195` and `http://localhost:5195` (`UI_ORIGINS` in `caobunga/api.py`), GET only. Both loopback spellings are listed because a browser treats them as distinct origins. The shipped UI is same-origin via the Vite proxy and does not depend on this; the allow-list covers a direct browser open. Non-browser clients send no `Origin` and are unaffected. `TrustedHostMiddleware` (CBN-248) additionally rejects any request whose `Host` header isn't `127.0.0.1` or `localhost` (`ALLOWED_HOSTS` in `caobunga/api.py`) — a loopback bind alone doesn't stop DNS rebinding, since a browser page can resolve an attacker-controlled name to `127.0.0.1` and send an untrusted `Host`.

`uvicorn --reload` is opt-in via `CAOBUNGA_API_RELOAD` (default off; `1`/`true`/`yes`/`on` enables it) — a dev convenience, not something an unattended launch of `backend/main.py` should pick up unasked.

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{ "status": "ok" }` |
| `GET` | `/api/projects` | `{ "projects": [ … ] }` — registry fields plus `last_run` summary, `current`, counts-only `coverage`, and verdict-only `tree` |
| `GET` | `/api/projects/{id}` | One project: registry fields, `current`, `coverage` with `denials`, `tree` with `blocking_lines`, full `last_run` (`exclude_none`), `runs` + `run_counts`, `reports` |
| `GET` | `/api/digest` | `{ "markdown": "…" }` — same local-day fleet body as `caobunga digest`, without the CLI's `## Model pins` footer (CBN-256) |
| `GET` | `/api/stats` | Fleet-wide fold over every project's run records — outcome rollup, local-day throughput, routing, duration / escalation aggregates |

Unknown `{id}` → `404` `{ "detail": "unknown project: {id}" }`, mapped from the typed `UnknownProjectError` `Store.get` raises (CBN-263), never from the message text; any other `UserError` → `400`. Other methods on these paths → `405`.

Each route declares a pydantic `response_model=` in `caobunga/api.py` (CBN-263) — `ProjectsResponse`, `ProjectDetail`, `DigestResponse`, `FleetStats` — describing the shape below rather than changing it. Every route serializes with `response_model_exclude_unset=True`: a key the handler omits stays absent and an explicit `null` stays `null`, so the `exclude_none` run-record objects and the list-vs-detail `coverage` / `tree` split survive unchanged. `backend/tests/test_api_payload_keys.py` pins that: each live body equals the dict its handler builds.

`current` is the in-progress Flaitron task, read from `read_project`'s **`in_flight`** field (§"Flaitron reader"): `task_id`, `status`, `reason`, plus `plan_model` / `shortname` when set. Otherwise `null`. A missing or unreadable worker path is `null`, not an error.

It reads `in_flight` rather than the chosen `candidate` (CBN-EPIC-17.3). Why: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Status API — why `current` reads `in_flight` (CBN-EPIC-17.3)".

`coverage` is the project's `[unattended]` marker coverage (CBN-EPIC-18.3), read off `read_project`'s **`coverage`** field (§"Marker coverage"): `open`, `marked`, `denied`, and `unparsed` (CBN-115 — grammar-dropped rows, counted beside the tally, never inside `open`) on both endpoints, plus the detail-only lists — `denials` (per denied task: `task_id`, `<code> — <prose>` `reason`, the PLAN `line` + raw `text`, a `seeded_line` when a marker is the fix — CBN-119.3, in dispatch order — and, on an `unmarked` denial only, `triage` (`obvious` / `needs-judgment`) plus a `triage_reason` when the latter — CBN-157; `null`/absent on every other denial code), `unparsed_rows` (`line` + raw `text` per dropped row; the id never parsed, so the line is the only handle), and `marked_tasks` (CBN-125 — the ids behind the `marked` count, same dispatch order). Marker *drift* is not served here: it needs a baseline from run records, and §"Marker coverage" records why that join stays on the CLI. Same aggregate-vs-detail split as `run_counts` / `runs`: the fleet view answers "which repos need seeding", the project view answers "which lines". A missing or unreadable worker path is `null`, not an error — the same degradation `current` makes, and for the same reason.

`tree` is the operator-presence verdict (CBN-130), read off `presence.read_tree` (§"Target-tree report"): `readable`, `clean`, and the `blocking` count on both endpoints, plus the detail-only `candidate`, `exempt_task`, `clean_for`, and `blocking_lines` (per line: the raw porcelain `text`, the `path` it names or `null`, and the `task_id` when that path is an open tasknote). Same aggregate-vs-detail split `coverage` uses: the fleet view answers "which repos are ready", the project view answers "why not". `null` on an unreadable path, exactly as `current` and `coverage` degrade — distinct from `readable: false`, which means the path parsed as a Flaitron project but git could not report on it.

All three fields come from **one** read per project per request. `current`, `coverage`, and `tree` are three questions about the same `ProjectRead`, so the endpoints call `flaitron.try_read_project` once and derive all of them. `tree` does add one `git status --porcelain -uall` per project per request — accepted deliberately ([decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Status API — one read, and the `tree` cost (CBN-130)").

List `last_run` is a summary: `task_id`, `outcome`, `closed_at`, `summary`. Detail `last_run` is the run-record object with CONTRACT field names. No last run → `null`.

Detail `runs` is that project's full run history (CBN-EPIC-12.3) — every readable record under `runs/{id}/` (§"Run record" — unreadable files are skipped, not raised), newest-first by `closed_at`, each the same `exclude_none` run-record object as detail `last_run`, so the §"Run record" signals (`started_at`, `escalation_reason`, `hermes_review`) surface here too. Ordering is an explicit sort on this surface, not the runs-directory filename order. Unbounded in v1: no pagination, no cap. `runs[0]` is detail `last_run`; `last_run` stays for the list/detail symmetry the UI already reads.

Detail `run_counts` tallies those runs by outcome. **All five** §"Outcomes" values are always present, zero when unseen, so a consumer renders a fixed chip set without guarding for missing keys. `sum(run_counts.values())` equals `len(runs)`.

No runs → `runs: []` and an all-zero `run_counts` — never `null`. Both fields are detail-only: `GET /api/projects` keeps its `last_run` summary and adds neither. Neither field bumps `schema_version`.

`reports.last_rollup` is the latest markdown body under `rollups/{id}/`, or `null` — none written, or the latest one unreadable or not UTF-8 (CBN-318). `reports.last_brief` left with the Hermes brief (CBN-258).

`/api/stats` (CBN-EPIC-12.7) folds **every** registered project's run records into one read — run records only, never tasknote phase logs or worker transcripts. Unbounded in v1 like detail `runs`: no pagination, no cap, no window parameter. Body:

```json
{
  "projects": 2,
  "runs": 5,
  "invocations": 2,
  "outcomes": { "closed": 2, "de-scoped": 0, "blocked": 1, "failed": 2, "no-tasknote": 0 },
  "success_rate": 0.4,
  "duration": { "counted": 3, "median_seconds": 600, "max_seconds": 3600 },
  "escalations": { "note-blocked": 1, "worker-parked": 0, "…": 0, "timeout": 1 },
  "throughput": [ { "day": "2026-08-21", "runs": 3, "outcomes": { … } } ],
  "routing": {
    "provider":   [ { "key": "grok_cli", "runs": 3, "outcomes": { … }, "duration": { … } } ],
    "model":      [ … ],
    "plan_model": [ … ]
  }
}
```

Field rules:

- `invocations` counts **distinct `(project, invocation_id)` pairs** (CBN-EPIC-313.4). A bare id is not enough: parallel fleet legs can share a millisecond stamp, and one project's drain under one id still counts as one. Records predating the field carry none and are not counted — this is "invocations we can name", not a claim about fleet history.
- `success_rate` is strictly `outcomes.closed / runs`, rounded to 3 places, and `null` when `runs` is `0`. `de-scoped` is a legitimate Flaitron closure but not work landed, so it counts against the rate; the raw tallies are there for any other reading.
- `duration` (fleet-level and per routing row) derives each span as `closed_at - started_at` per §"Run record". Records with no `started_at`, and any negative span, are **excluded** rather than counted as zero. `counted` names the contributing sample; `median_seconds` / `max_seconds` are `null` — never `0` — when nothing counted.
- `escalations` seeds all seven §"Escalation reasons" keys. `sum()` may fall **short** of `outcomes.failed + outcomes.blocked`: records written before the field stopped without a reason.
- `throughput` buckets on the **local** calendar day of `closed_at` — the same boundary as §"Daily fleet digest" §Window, not a second one. One row per day that saw runs; days with none are absent, not zero-filled.
- `routing` breaks the same records out by `provider`, `model`, and PLAN `plan_model`. Runs whose field is unset group under `"key": null` rather than being dropped, so `sum(row.runs)` equals `runs` for **every** dimension.
- Every outcome tally on this endpoint — fleet, per day, per routing row — carries all five §"Outcomes" keys, zero when unseen, matching detail `run_counts`.

Ordering is an explicit sort on this endpoint, never inherited from filesystem order: `throughput` newest-day-first; `routing` rows by `runs` descending, then `key` ascending, with `null` last.

An empty registry or a fleet with no runs returns the same shape zeroed — `runs: 0`, all-zero tallies, empty lists, `null` rate and durations. Never `null` for the body, never an error. `/api/stats` adds no field to any record and does not change `schema_version`.

**The detail payload's key vocabulary is guarded on both sides (CBN-EPIC-231.3).** `fixtures/api-payload-keys.json` names the expected key set for thirteen shapes — `project_detail`, `coverage_detail`, `tree_detail`, `current_task`, `reports`, `run_record`, and the element shapes nested inside them (CBN-234): `denial`, `unparsed_row`, `dirt_line`, `new_row`, `foreign_commit`, `suite`, `suite_verb`. `backend/tests/test_api_payload_keys.py` derives each set from the live endpoint or the pydantic `model_fields` that build it; `frontend/tests/unit/api-payload-keys.test.ts` derives the same sets from `frontend/src/api/types.ts` through a compiler-enforced `Record<keyof T, true>` mirror. The same file's `enum_values` block (CBN-263) names the value set of every enum the payload serializes — `execution_tier`, `outcome`, `escalation_reason`, `hermes_review`, `hermes_verdict_code`, `suite_verdict`, `external_review`, `triage_class` — checked against the backend `StrEnum` members and the `types.ts` string-literal unions (plus the `OUTCOMES` / `ESCALATIONS` chip arrays). Both diff against the fixture, so a field added to a model, the fixture, or `types.ts` without the other two fails whichever suite runs next ([decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Status API — why the payload-key guard (CBN-EPIC-231.3)"). The nested element shapes are checked on the backend by `model_fields` alone, not the live endpoint — several of those arrays are conditionally present, so no single payload carries every element.

Must not appear: worker transcripts, worker session dumps, tasknote phase logs, start/stop/approve actions, usage payloads, secrets.

## Status UI

Read-only Vite dashboard (CBN-041). Consumes the Status API above; no start / stop / approve. Not a CLI verb — serve with `just dev` in `frontend/` (pair with `just api`).

Bind: `127.0.0.1:5195` (`strictPort`). Dev proxy: `/api` and `/health` → `http://127.0.0.1:8013`.

| Route | Surface |
|---|---|
| `/` | Fleet table — registered projects with tier, enabled, `marked/open` marker coverage, current task, last-run summary |
| `/projects/{id}` | Project detail — registry fields, `current`, marker coverage with every denied id, full `last_run`, run history grouped by `invocation_id`, `reports.last_rollup` |
| `/stats` | Fleet stats — the `GET /api/stats` fold as chips and tables: totals, outcome rollup, closed rate, duration, escalations, per-day throughput, and one table per routing dimension |

Unknown `{id}` shows a not-found message (API `404`). Empty registry, missing run, or missing reports render null states — not errors.

Run history (CBN-EPIC-12.8) groups by `invocation_id`, newest invocation first as served, each section headed by the stamp, its run count, and its own outcome tally — so the page answers "how did *that run* go?", not only "how did task X go?". Records predating the field carry no key and render in a trailing **ungrouped** section; they are never dropped, so the rendered rows still sum to `runs`. The project-wide five-chip tally above the sections stays `run_counts` and is unchanged.

The Last run panel renders a record's `follow_ups` and `new_rows` when it carries them (CBN-138.4) — proposals first, their result second, on the digest's ordering and its reason (§"Body shape"; [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Daily fleet digest — bullet ordering and tag rules"): a proposal is what the worker said, a new row is what it wrote into the target PLAN. Each new row states its own marker state. Both rows are omitted whole when the record carries neither, so a quiet run's panel is byte-identical to before (the backend normalizes an empty list to `null`); this is not the phase-log leak the must-not-appear list rules out ([decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Status UI — Last run panel and coverage cell rationale (CBN-138.4, CBN-EPIC-202.5)"). The panel also renders the legacy Hermes fields when a pre-CBN-258 record carries them: `hermes_verdict` as a `Hermes verdict` row beneath `Hermes review` (CBN-148), dash idiom otherwise. The parsed code and the degraded-launch list follow the same rule (CBN-181): `hermes_verdict_code` renders as a `Hermes verdict code` row between the two, `hermes_missing_servers` as a `Hermes missing servers` row (names joined, dash when absent), and the run-history `review:` chip carries the code as a `[code]` tag on the digest's idiom, omitted when none — so a `degraded` review is never indistinguishable from a full one on the page. The deterministic suite (§"Suite", CBN-EPIC-202.5) gets the same two surfaces, unconditionally: a `Suite verdict` row (dash idiom when the record carries no `suite`) and a `suite: <verdict>` chip on the run-history row, beside the `review:` chip, tinted for a non-`green` verdict.

Marker coverage renders as `marked/open` on the fleet table. Detail lists every denied id with its reason, its PLAN line number, the row as it reads, and — when seeding is the fix — the row with `[unattended]` correctly placed (CBN-119.3), in the order to seed them; a `null` coverage (unreadable path) renders the dash idiom on the table and a null state on detail, never `0`.

`/stats` renders **chips and tables only — no charting dependency**. Every tally the API seeds renders its full key set including zeros (five outcomes, seven escalation reasons). The contract's nulls render as the dash idiom, never `0`: `success_rate` on an empty fleet, `median_seconds` / `max_seconds` when `counted` is `0`, and the `null` routing key. Derived spans carry second precision below the hour (`2m 52s`) so fleet medians reconcile with the per-run rows they aggregate.

Must not appear: start/stop/approve controls, worker transcripts, tasknote phase logs, usage payloads, secrets.

## Hermes brief

**Retired (CBN-258, 2026-09-24; verdict CBN-242, §"Hermes review").** The brief, its launch, verdict delivery, and the `CAOBUNGA_HERMES_*` variables are gone; a close writes its run record, then the last-run card and any epic rollup. Removed: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Hermes brief — what the retirement removed (CBN-258)".

What survives is the deny-list below. It was written for the brief, but the remaining report surfaces — digest, last-run card, status API — honor the same rows, and they cite it.

### Deny-list (closed)

| Must not appear | Why |
|---|---|
| Worker transcripts, worker session dumps, tmux scrollback, raw CLI stdout | Disposable worker context. Non-negotiable. |
| Tasknote phase logs (`## 📝` / `## 🛠️` / `## 🧪`), Discovery / Implementation / Testing notes, Acceptance checklists | Phase logs are how the work was done, not the report. |
| Full archived tasknote, prior tasknote dumps | One close, one excerpt. |
| Usage payloads (CBN-020, landed) | Not a report field. |
| Picker why-log beyond run-record `provider` / `model` / `plan_model` | Internals stay in the picker (CBN-022). |
| Git diffs, patches, `git log` | SHA lives on the run record. `git log --grep` is not an index. |
| Secrets, env files, auth material | Not a report surface. |

Two boundary fields are not leaks: a proposed follow-up (a run-record field extracted once at close, never an excerpt of the note) and the run record's `log_path` (CBN-162, a filesystem pointer). The arguments: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Hermes brief — why proposed follow-ups are not a phase-log leak" and §"Hermes brief — why `log_path` is not a first-row leak (CBN-162)".

## Epic rollup

Per-project artifact written when a PLAN epic cohort completes (CBN-031). Folds **run records only** — never tasknote phase logs or worker transcripts. Daily fleet digest (CBN-032) is a different surface.

### Cohort detection

1. **Explicit ID ranges** in PLAN HTML comments — `CBN-010–014` or unpadded `DOC-1 – 5` (en dash or hyphen). Member ids use the spelling filed in PLAN (padding is not forced); every ID in the inclusive range must be checked before the rollup writes.
2. **Flaitron epic parents** — `AREA-EPIC-N` rows with `.sub` children. Children are matched by canonical parent, not by literal id prefix, so both filing forms count: `DOC-20.1` (the `SPEC/epic.md` shape) and `DOC-EPIC-20.1` alike belong to `DOC-EPIC-20` (CBN-EPIC-9.5). Complete when every child is checked (parent row may stay open until `/ft-close-epic`).

Write once per cohort key; do not rewrite an existing rollup file for the same epic.

A PLAN missing or unreadable at report time (unopenable, not UTF-8) writes no rollup rather than failing the close: the run record is already written, and the rollup is derived from it (CBN-318).

### On-disk path

`$CAOBUNGA_HOME/rollups/{project}/{YYYYMMDDTHHMMSSmmmZ}-{epic_key}.md`

`epic_key` is the range form (`CBN-010-014`) or the parent id with hyphens replaced (`CBN_EPIC_1`).

### Body shape

Markdown. Fixed headings:

````markdown
# Epic rollup — {project} — {epic_key}

{title from PLAN comment or parent id}

## Runs

- **{task_id}** (`{outcome}`) — {run.summary}
````

Runs appear in filename sort order (UTC `closed_at`). Omit tasks with no run record; do not invent summaries.

## Daily fleet digest

Operator-facing CLI rollup (`caobunga digest`). Folds **run records only** — never tasknote phase logs or worker transcripts. The CLI verb then appends a `## Model pins` section, one `- pins: …` bullet per line of the stale-pin guard (§"Task-class → model map", CBN-256); it is a live catalog probe, not a run-record fold, so it lives in the verb and not in `format_digest`, and `GET /api/digest` does not carry it. Epic rollup (CBN-031) is a per-project persisted artifact; this is a fleet snapshot for one local calendar day. Print-only: do not write under `$CAOBUNGA_HOME` (the on-disk tree has no `digests/`).

### Window

**Local** calendar day of `now` (`closed_at` converted to the host's local zone). No `--date` flag in v1 (`digest` takes no args). Runs from other days are omitted. Run records still store UTC `closed_at`; only the bucketing is local. The window was UTC through CBN-032; CBN-EPIC-95.4 made it local deliberately. Why: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Daily fleet digest — why the window is local (CBN-EPIC-95.4)".

### Projects

Every registered project, in `Store.list_projects()` order (`id`). Disabled projects still appear (same as `status` with no arg). Projects with no run that day keep the heading with `(no runs)`. Empty registry prints `(no registered projects)`. Exit 0 in all of those cases.

### Body shape

Markdown. Fixed headings:

````markdown
# Fleet digest — {YYYY-MM-DD}

## {project}

- **{task_id}** (`{outcome}`) [{run.hermes_verdict_code}] [suite: {run.suite.verdict}] [review: missing] (park: {run.park_reason}) — {run.summary}    # omit the [code] tag when none (legacy, pre-CBN-258 records only); omit the [suite: …] tag when green or absent; omit the [review: missing] tag unless external_review is missing; omit the (park: …) affix when none
  - **follow-ups proposed ({n}):**       # omit the whole block when none
    - {line}
  - **new rows ({n}, {m} unmarked):**    # omit the whole block when none
    - {task_id} — unmarked
  - **hermes:** {run.hermes_verdict}      # omit the line when none (legacy, pre-CBN-258 records only)
````

Runs appear in filename sort order (UTC `closed_at`). Bullet fields are `task_id`, `outcome`, and `summary`, plus the run record's `follow_ups` when it carries any (§"Follow-up capture", CBN-121). The `new_rows` block follows the follow-up block (§"New-row detection", CBN-138.3): proposals print first and their result second, and each row states its marker state. Both blocks are omitted whole when empty, so a quiet run's bullet is byte-identical to before. The `hermes:` line closes the bullet (§"Hermes review", CBN-148) and is omitted on the same rule; the `[code]` tag rides the task-id line instead (CBN-160). Since CBN-258 the loop writes neither field, so both appear only for records from before the retire and fade out as the window moves on. The `[suite: …]` tag (§"Suite", CBN-EPIC-202.5) rides the same line, right after the `[code]` tag, but only when the verdict is not `green`. The `[review: missing]` tag (§"External review", CBN-257) follows it, only when `external_review` is `missing`. The `(park: …)` affix (§"Park reason", CBN-187) rides the same line last, omitted when the run carries no `park_reason`. Why each block and tag sits where it does: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Daily fleet digest — bullet ordering and tag rules".

### Example

````markdown
# Fleet digest — 2026-08-19

## app

- **CBN-031** (`closed`) — Epic rollup writer.

## charts

(no runs)
````

## Last-run card

Picked: a **caller-side file**, not a GitHub PR comment and, since CBN-188.2, not a target-repo commit either — flaitron's own boundary rules it out: a caller commits nothing of its own into a producer repo (`docs/EXTERNAL-AGENTS.md` step 9). No `gh`, no PR, no GitHub auth, and no git at all. File-native. History: [decisions/cli-fleet-surface.md](decisions/cli-fleet-surface.md) §"Last-run card — why a caller-side file (CBN-033 → CBN-188.2)".

Library, not a CLI verb. After every gated `write_run` (including `failed` / `blocked` / `de-scoped` / `no-tasknote`), `status_card.publish_status` overwrites `$CAOBUNGA_HOME/status/{project}.md` through `Store.save_status` (temp file + `os.replace`, under the store lock — §"On-disk layout"). `observe` does not write. Do not append a ledger; overwrite the last-run card. The run-record YAML itself is never rendered into it.

A write failure is `ReportError` (exit 4), same as before the caller-side move; the CLI still dumps the run YAML. There is nothing to roll back: the write never leaves `$CAOBUNGA_HOME`, so a failed publish cannot leave the target repo dirty the way the old committed snippet could (CBN-EPIC-9.5).

### Body shape

Markdown. Last-run fields only:

````markdown
# Caobunga status

- **project:** {project}
- **task:** {task_id}
- **outcome:** {outcome}
- **commit:** {commit_sha}   # omit when none
- **next:** {next_task}      # omit when none
- **summary:** {summary}
````

Never worker transcripts, phase logs, or usage payloads.

## Flaitron reader

Library, not a CLI verb. CBN-013's `run` calls `caobunga.flaitron.read_project(path)` with the registry `path`.

Input: a repo root that contains `.flaitron/PLAN.md`. Open tasknotes are `.flaitron/tasknote/*.md` (not `archive/`, not README).

**`.flaitron/` is a hard cut (CBN-278).** Flaitron v6.0.0 renamed flowtron's `.flowtron/` tree and its `.flowtron/core` submodule, with no old-name fallback upstream, and the reader follows it: every target-layout path lives in `flaitron/layout.py` and roots at `.flaitron`, with no second root to fall back to. A target still on `.flowtron/` reads as a missing PLAN (exit 2, below) until flaitron's fleet wave (CORE-711.7) migrates it. Caobunga's own checkout and its guard tests moved to `.flaitron/core` in that wave.

Selection (unchecked rows only):

1. If an open tasknote is `in-progress` and that id is still an unchecked `## High` / `## Medium` / `## Low` row, that id wins (one task per window) — **attended picks only**, see below.
2. Else follow Flaitron SPEC: within High, `[!critical]` first; then High → Medium → Low; then lowest incomplete id. Skip `## Completed`, `## Future Opportunities`, and parent `AREA-EPIC-N` tracking rows. Children (`AREA-N.sub`) are eligible.

**Two dispatch dimensions, two tier sets (CBN-118).** `read_project(root, *, unattended=False, drain=False)` carries both; they are separate parameters because the two sets do not coincide.

| Parameter | Question | True on |
|---|---|---|
| `unattended` | Does this dispatch launch a **headless** worker, so only rows the operator vetted may run? | `gated`, `proceed-on-green` — every launching tier |
| `drain` | Is nobody watching the **sequence**, so preferring the operator's open note would race them? | `proceed-on-green` |

The sets stand in strict containment, so `drain=True, unattended=False` is **unrepresentable** — it raises rather than silently reinstating the race CBN-EPIC-17.3 closed. Why one parameter became two, and why it raises: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Flaitron reader — why two dispatch parameters (CBN-118)".

**The in-progress override is a dispatch preference, dropped only on a drain (CBN-EPIC-17.3, CBN-118).** Under `drain=True` step 1 is skipped and step 2 decides alone: a note the operator left open is *their* work. `gated` keeps it — a one-shot the operator aimed at one named repo should continue what they have open. Being a *dispatch* preference, the override is subject to every dispatch dimension including the marker: it picks among eligible rows and never exempts one, so an unmarked open note is passed over at `gated` exactly as any other unmarked row is. The `CBN-109` resume path survives the drain-side drop without a special case ([decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Flaitron reader — resume survives the drain-side drop (CBN-109)").

**`in_flight` is the override's display half**, split onto its own `ProjectRead` field by the same task. It answers "what is open right now" (step 1, always attended eligibility) rather than "what would be dispatched next", is computed on **every** read and every posture, and is what §"Status API" serves as `current`. Splitting it is what stops a change to the picker's dispatch preference from silently blanking the dashboard (CBN-040, from CBN-011: this is the only durable current-task signal).

**`Blocked by` don't-start gate (CBN-116).** A row whose PLAN long description carries flaitron's canonical `Blocked by [[ID]]` clause (`SPEC.md` §"Long-description conventions") is denied at **every** posture — `gated` honors it exactly as a drain does, because the clause is scoped to no tier: it is the operator writing "do not start this" on the row. Parsing is a port of the reference `viz/src/parser.ts` `extractBlockedBy`, tolerance for tolerance — wikilink-only (a bare `Blocked by: CBN-115` does not parse), comma-separated lists in one clause, code spans stripped, deduped in source order — and lands on `PlanTask.blocked_by`, the same list viz exposes as `Task.blockedBy`.

`SPEC/blocked.md` makes *clause removal* the act that clears the gate ("the task re-enters when the blocker clears — remove the `Blocked by` clause"), so a clause whose blockers are all already checked **still denies** rather than auto-releasing: dispatching a row whose PLAN line literally reads "Blocked by" would be the orchestrator overruling the operator's own text. It is reported as stale — `blocked — Blocked by [[CBN-115]] (completed; clause is stale, remove it)` — because the fix is a one-line PLAN edit and the operator should not have to diff two rows to find it. A blocker id absent from PLAN cannot be *confirmed* complete and so reads as still-blocking, the same bias-to-refuse the marker dimension applies.

**The staleness annotation reads `PLAN.md` only (CBN-125, CBN-192)** — the same gap `marker_drift` has (§"Marker coverage"). `completed_task_ids` never opens `.flaitron/PLAN-ARCHIVE.md`, so a blocker whose row `## Completed` rotation moved there denies with the plain `blocked — Blocked by [[ID]]` reason and no stale-clause pointer; the deny verdict itself is unaffected. This is a known gap against flaitron's caller contract, recorded rather than built around: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Flaitron reader — stale-clause annotation reads PLAN.md only (CBN-125, CBN-192)".

Two boundaries the gate does not cross. **Tasknote YAML `blocked-by:` is not read as a gate** — flaitron defines the key as a planning claim, not a don't-start gate (`SPEC/blocked.md`, `SPEC.md` §"Tasknote frontmatter"). **`in_flight` is unaffected** — it answers "what is open right now", so a blocked row an operator nonetheless has open still displays; only dispatch refuses.

The gate lives in `eligibility`, the single place a dispatch decision may live (CBN-EPIC-17.2), superseding CBN-011's decision that `Blocked by` is not a reader skip rule. Why, and why the grammar is ported rather than restated: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Flaitron reader — `Blocked by` grammar port and the CBN-011 overturn (CBN-116)".

**Headless safety dimension (CBN-EPIC-17.2, retiered at CBN-118).** `read_project(root, unattended=True)` — the caller's declaration that this dispatch launches a headless worker, passed by the run loop for **every launching tier** — adds a further eligibility dimension to both steps above: only tasks carrying the lowercase **`[unattended]`** marker *after* `[model]` on their PLAN line are eligible; an unmarked task is denied (bias-to-refuse: the marker opts a task in, and undecidable denies). Tasks in the open launch queue that this pick refused — for the marker or for the `Blocked by` gate above — are returned on `denied` with `<code> — <prose>` reasons, and the chosen candidate carries `eligibility_reason` (§"Run record"). The attended default is what the **non-launching** readers use (`observe`, the status API, `try_read_project`) and is byte-identical to the pre-marker read; its reason reads `attended — this read does not consult the marker`, since no launching tier is attended any more. A run record therefore never carries the attended reason.

**Exploratory dimension (CBN-114).** A row carrying **`xheavy`**, in the `[model]` slot or anywhere in the trailing token run (CBN-260), is denied outright at every launching tier, ahead of the marker check, with `exploratory — [xheavy] is manual-only (SPEC/model.md); caobunga caps dispatch at [heavy], stricter than flaitron's [frontier]`. The rule is flaitron's `SPEC/model.md`, applied to its addressee ([decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Flaitron reader — `[xheavy]` is flaitron's rule (CBN-114)"). Flaitron's own automated-chooser cap has since moved up to `[frontier]` (v6.1.0); the reason names caobunga's stricter cap rather than flaitron's.

Three things this deliberately does *not* do. It is **not** a fourth `TaskClass` — `classify("xheavy")` still answers `HEAVY` (§"Task-class → model map") for the attended reads that legitimately need a tier. It does **not** fire on the attended default: `observe` and the status API still show the row, and only launch refuses. And it is checked **before** the marker, so the reason names a fix that exists. Like the `Blocked by` gate, the denial lands *inside* `open`, so `marked + denied == open` still holds. The rejected fourth class and the pre-dimension history: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Flaitron reader — exploratory dimension history (CBN-114)".

**Frontier dimension (CBN-331).** A row carrying **`frontier`**, **`fable`**, or **`mythos`** (flaitron's chooser-assignable `[frontier]` rung and the concrete tokens `SPEC/model.md` bands there), in either slot, is denied at every launching tier with `frontier — [<token>] is operator-authorized; caobunga caps dispatch at [heavy], stricter than flaitron's [frontier]`. `<token>` is the first of `grammar.FRONTIER_TOKENS` the row carries. This is caobunga's own policy and not flaitron's: flaitron lets an automated chooser reach `[frontier]`, while caobunga dispatches `[light]` / `[medium]` / `[heavy]` only and leaves the token-heavy rungs to the operator (operator decision 2026-10-11). Like the exploratory dimension, it is checked ahead of the marker, carries no `seeded_line` or `triage`, and does not fire on attended reads. It sits right after `exploratory` and before `handoff`. The tokens have no `taskmap` alias (§"Task-class → model map"). A `[frontier]💎` row parses since the grammar accepted the 💎 glyph (CBN-330) and denies here like any other frontier row. Why: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Flaitron reader — the frontier band is caobunga's cap (CBN-331)".

The marker's position is load-bearing, and both misplacements fail **silently** — which is why tests pin them: a leading-`!` shape (`[!unattended]`) matches no `TASK_LINE` slot and drops the whole line from the parse, and a lowercase `[unattended]` placed before (or without) `[model]` is captured *as* the model token, routing effort instead of gating safety. The upstream marker and park-code history: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Flaitron reader — upstream marker work (CORE-494, CORE-495)".

**Unattended filing authority (`CORE-551`, flowtron v5.26.0, pinned at CBN-139).** An `--unattended` run's duty to file a deferred hand-off as an unchecked PLAN row is now also its authority to do so. **`--park` and `--unattended` do not compose** (refused terminally, `⏸ --unattended stop — flag-conflict`), so the discharge path is the default flow with `--unattended`; and **a run that parks files nothing**, which is why CBN-138.4 reads parked notes (§"Run record"). Both decisions and the filing's history: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Flaitron reader — the two upstream filing decisions (CORE-551)".

`**Suggested next task:** **ID**` is returned as `suggested_next` and is not the selector (the line can go stale). Under an unattended posture it is only ever an *attended* recommendation ([decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Flaitron reader — `suggested_next` is an attended recommendation"). `dispatchable(tasks, id, *, unattended=False)` is the id-addressed form of the eligibility predicate for callers holding a name rather than a `PlanTask`; the run record's `next_task` uses it to check the line at the run's own posture before repeating it (§"Run record"). Missing PLAN.md is a user error (exit 2). No eligible row → `candidate is None` (CBN-013 maps to exit 3).

Candidate fields CBN-013 needs: `task_id`, `plan_model` (PLAN `[model]` token), `shortname`, `priority`, `critical`, `reason` (`spec-selection` | `in-progress-override`), and `tasknote` (`path` + `status` + `park_reason`) when an open note exists. `in_flight` carries the same shape when a note is open, always with `reason: in-progress-override`.

### Marker coverage

`read_project` returns a fourth derived field alongside `candidate` / `in_flight` / `denied`: **`coverage`** (CBN-EPIC-18.3), the `[unattended]` marker tally over the project's open launch queue. Why: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — why `coverage` exists (CBN-EPIC-18.3)".

- **`unparsed` counts the rows the grammar dropped (CBN-115), beside the tally, never inside `open`.** A task-shaped line — checkbox, checked or not, in a launch-bucket section — that fails `TASK_LINE` never became a `PlanTask`, so it cannot join `open` honestly; counting it keeps a botched seed from reading as fully covered. The field covers the placements the grammar still rejects (e.g. `[!unattended]`, or `[unattended]` with no `[model]` at all). `unparsed_rows` carries the detail — 1-based PLAN line number plus raw text, because the id is unrecoverable by definition. `## Completed` / `## Future Opportunities`, fenced blocks, and plain prose bullets don't count — including a checkbox row with no `*` anywhere on it (CBN-209). Checked rows *do* count (CBN-210). **Report-only (CBN-119.2).** No dispatch refuses over a nonzero count, but every launching tier's pre-flight prints the dropped rows and the exit-3 message names them (§"Run loop"). Detection shares `parse_plan`'s exact masking walk (`_plan_body_lines`), so the two readers cannot disagree about which lines were candidates, and `TASK_LINE` itself is untouched: widening it would be defining the producer's format (§"Fleet boundary"). Why no refusal — the three arguments CBN-119.2 kept it on: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — `unparsed` stays report-only (CBN-115, CBN-EPIC-18.4, CBN-119.2)".
- **`open` is the queue-membership set** — the three dimensions the `Blocked by` and safety dimensions are checked *after* (not completed, inside `## High` / `## Medium` / `## Low`, not a parent-epic tracking row). `marked + denied == open` always holds, and a tracking row never appears as something to mark. A blocked row (CBN-116) therefore stays *inside* `open` and reports as a denial with a `blocked — ...` reason. `observe`'s per-task echo is deliberately wider (it echoes `deny: parent-epic` lines). Why both: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — `open` membership".
- **Computed as-if-unattended on every read, whatever the tier.** Coverage reports on the *repo*, not on the caller's posture, and `read_project(root)` and `read_project(root, unattended=True)` return the same figure. Why not `denied`, and why not launching tiers only: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — why not `denied`".
- **Derived per read, never persisted.** A cached classification would be the second source of truth CBN-EPIC-17.1 rules out, and it would go stale on the next PLAN edit. It reuses `eligibility` rather than re-deriving the marker rule, so the report cannot drift from the behavior it describes.
- **The detail form is a seeding worklist, not a list of ids (CBN-119.3).** Each `Denial` carries the 1-based PLAN `line`, the row's raw `text`, and — when a marker is the fix — a `seeded_line`: the same row with `[unattended]` placed after `[model]` and **after any suggestion glyph** — flaitron's own placement (`[light]🔧 [unattended]`, `SPEC/unattended-candidacy.md` §"Three postures"; CBN-343) and the one shape emitted, even though `TASK_LINE` accepts either side since `CBN-167`. The one exception is a glyph after a stacked-`[model]` run (`[sonnet] [light]🔧`): no token may follow that glyph slot, here or upstream, so the marker lands before it. **The suggestion is verified by re-parsing it, never by trusting the insertion** — `seeded_line` is emitted only when the rebuilt row parses back to the same id and reads as marked, so a modelless row yields `None` and a hand edit rather than a broken suggestion. `None` is also what a `blocked`, `exploratory`, or `frontier` denial gets: seeding does not fix any of them. **`denials` is ordered by the picker's own `_selection_key`, so the list *is* the seeding order** — seed the first entry and it is the row that dispatches next. `read_project`'s posture-dependent `denied` is built by the same `_denial` constructor and sorted the same way. Detail surfaces only; the fleet table keeps its `marked/open` cell (§"Status API"). `caobunga status <id>` elides both rows to 96 characters, the tail cut so the head's placement stays visible. The placement measurement, the modelless failure, and the ordering and elision reasoning: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — seeding worklist shape (CBN-119.3, CBN-167)".
- **Unmarked triage — obvious vs needs judgment (CBN-157).** `Denial.triage` (a `TriageClass`: `obvious` / `needs-judgment`) splits the `unmarked` population into rows the mirrored predicate confirmed (`obvious`) and rows it did not (`needs-judgment`). This reader no longer confirms a clause 3 keyword miss, so a real read does not produce `obvious` (CBN-361), and `Denial.triage_reason` names the clause that declined in the same `<code> — <prose>` shape `park_reason` / `eligibility_reason` use. **The predicate is mirrored, not invented** — flaitron's own `[unattended]` candidacy predicate (`SPEC/unattended-candidacy.md` §"Candidacy predicate", `CORE-577.2`). Only the clauses `eligibility` has **not** already cleared by the time a row reaches `unmarked` are checked: the `[model]` tier and `[!critical]` clauses, clause 3's free-text keyword screen (the marker form, `[handoff]`, already denies ahead of `unmarked`; a keyword miss is not a semantic pass — CORE-783.2, CBN-361 — the list stays that mechanical screen and gains no visual or raster keywords, and when every other mirrored clause is clean the row is `needs-judgment` with `keyword — a miss is not a semantic pass`, never `obvious`; the semantic decline of a person-shaped row belongs to an LLM filer and `/ft-prep-unattended`, which this reader does not run), clause 4's *unparsed* half (the parsed half already denies as `blocked`), and clause 6's epic-child stem rule — checked via `epic_stem`/`epic_parent`'s numeric fallback; a `.1` Discovery row is never obvious, a `.N` audit needs no stem check, and any other `.k` needs its numeric `.{k-1}` predecessor already closed. The `audit` dimension is mirrored too (CBN-353): an audit child (`.N`, or a numbered `.k` whose shortname ends `audit`) with an open epic sibling is `needs-judgment` with eligibility's own `audit — epic siblings still open (<ids>); …` reason, because `eligibility` checks that dimension only on a *marked* row. The check sits ahead of the keyword-miss result so the reason names the siblings (CBN-361). Every clause is conservative by the predicate's own rule: an uncertain read is `needs-judgment`, never `obvious`. **Scoped to `unmarked`, like `seeded_line`** — `blocked`, `exploratory`, `frontier`, and `handoff` denials carry `triage=None`. **Derived per read, never persisted**, the same constitution `coverage` itself states (CBN-EPIC-17.1): computed in the one `_denial` constructor from fields already on the `PlanTask`, so a PLAN edit can never leave a stale triage behind. `caobunga status <id>` groups `unmarked` denials under an `obvious` heading first, then a `needs judgment` heading for the rest — reordering the *printed* denials from `Coverage.denials`'s own dispatch order, though the underlying list and every other denial code's position are unchanged; a repo with no obvious rows keeps the flat list CBN-EPIC-18.3 shipped. The fleet table's coverage cell still has an `(N obvious)` affix alongside `unparsed` / `drifted` for a row the predicate confirmed. A keyword miss does not confirm, so a real read leaves that affix at zero (CBN-361). Why this was built and why mirrored: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — unmarked triage (CBN-157)".
  - **The paste-ready `sed` block.** `caobunga status <id>` ends its detail output with one block per repo: an id-anchored, whitespace-tolerant `sed -i.bak` line per `unmarked` denial that carries a `seeded_line`, built from `Denial.text` / `Denial.seeded_line` alone. **`-i.bak` (attached suffix), not BSD's `-i ''`** — the block's `rm -f` removes the backup. **Idempotent by construction**: each edit is guarded by a `grep -qF` check for the row's already-seeded text before `sed` ever runs, so pasting the block twice edits the file once. The block's trailing comment reminds the operator to scope their own commit to `.flaitron/PLAN.md`. **Nothing in the block runs, and nothing here writes to a target repo** — the read-only bound below is unchanged; it is text to paste into a shell already rooted at the project's own path. The incidents behind each property: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — paste-ready `sed` block (CBN-157)".
  - **The fleet worklist — `caobunga status --seed` (CBN-348).** It gathers every registered repo's `obvious` unmarked denials into one review list, using the same denial rows. Each repo gets its own block, built by the same idempotent `grep -qF` / `sed -i.bak` builder, but it names the PLAN by **absolute path**, so each repo's block pastes from any shell. The `git -C <path>` comments keep each commit per repo and scoped to `.flaitron/PLAN.md`. **`obvious` only**: a clause 3 keyword miss is `needs-judgment`, so it stays off this list (CBN-361). `needs judgment` rows stay in the per-repo `status <id>` view, where the detail block still seeds both classes. A repo with no obvious rows is omitted. An obvious row with no buildable edit is flagged for a hand edit. The headline always prints (`none` when empty) and counts only the repos actually read; an unreadable repo is named, not skipped. Read-only, like the per-repo block.
- **Unparsed blockers — the eligibility signal (CBN-128).** The worklist answers *where* to seed; this answers *whether*. SPEC §"Long-description conventions" parses `Blocked by [[ID]]` only, so prose blockers (`Blocked on [[ID]]`, `Blocked by an external prerequisite …`) parse as unblocked. `flaitron.unparsed_blocked_clause(task)` reports that: the description mentions "blocked" outside a code span and `parse_blocked_by` captured nothing from it. **The test is exact, not heuristic** — it cannot fire on a row the grammar handles, because a captured clause makes `blocked_by` non-empty by construction. It rides the same `_denial` constructor, so all three denial lists carry it and cannot disagree. What CBN-123 measured: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — unparsed blockers (CBN-123, CBN-128)".
  - **The signal is the tail the elision cuts, and the head is untouched.** `unparsed_blocker` is the fragment from the mention to the end of the description. `caobunga status <id>` prints it as a `warn:` row **between `now:` and `seed:`**, and the dashboard renders it in the denial block. `now:` and `seed:` elide exactly as before.
  - **Report-only, and the `seeded_line` is still offered beside it.** Nothing denies, refuses, or re-orders on the signal, and the fix an `unmarked` row genuinely needs is not withheld. `BLOCKED_BY_BLOCK` and `TASK_LINE` are both untouched; this is reading the producer's format, not defining it. Same posture as `unparsed` (CBN-115) and drift (CBN-125): state it, refuse nothing.
  - **Detection blanks code spans in place rather than deleting them.** Why: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — unparsed blockers (CBN-123, CBN-128)".
- **Declared hand-offs — the `[handoff]` dimension (CBN-136).** The fleet has three orthogonal facts about a row. `Blocked by [[ID]]` answers *may this start* and clears when the named task closes; `[unattended]` answers *may this run headless* and an absent marker is undecided, not refused. `[handoff]` answers the third: **will this stop mid-run for a human act that is not another task** — a scoped cross-repo filing prompt, a physical-access step, a credential. It is a durable property of the work, known at filing time — neither a dependency nor an absent opt-in. History: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — `[handoff]` dimension (CBN-136)".
  - **Checked ahead of the marker**, so the reason names a fix that exists and a hand-off row is never offered a `seeded_line`; a test pins the suppression.
  - **Position-independent (CBN-260), for both tokens.** `PlanTask.extras` carries the trailing token run verbatim, and `eligibility` asks the model slot and that list together, since `TASK_LINE` captures whichever bracket token comes first *as* the model. No grammar move — the regex is unchanged; one more captured group is read.
  - **It denies even when `[unattended]` is also present** — the one behaviour a report-only signal could not supply. The sound counter-argument it overrode is recorded in [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — `[handoff]` dimension (CBN-136)"; a later task revisiting this should start there.
  - **Attended reads never deny on it.** The declaration is about a *headless* run; with an operator present the hand-off is simply the work, so the row stays the candidate and the eligibility reason gains one clause naming what to expect. Order among the dimensions (ten since CBN-331): three membership (`queue_denial`), `blocked` (every posture), then `parked` → `exploratory` → `frontier` → `handoff` → `marker` → `audit` (launching tiers only). An `[xheavy]` row is manual-only whether or not it also hands off, so CBN-114's position is left byte-stable.
  - **Read-only, like every other dimension here.** Caobunga never writes a `[handoff]` token into a target repo; marking is an operator act on the same footing as seeding `[unattended]`.
- **Parked rows — the `parked` dimension (CBN-252).** A row whose open tasknote is `status: blocked` is waiting on an operator act, so a headless worker has nothing to do there. `eligibility(task, note=…)` denies it on launching tiers as `parked — tasknote is blocked (<park code>); resume attended with /ft-task <ID>`, and the pick falls through to the next eligible row. What re-picking a parked row cost: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — `parked` dimension measurement (CBN-252)".
  - **Checked first among the launching-tier dimensions**, ahead of `exploratory`: resuming attended is the one fix that clears the row whatever else it carries, and like CBN-114's ordering the reason is the discriminator. No `seeded_line` — the row is usually already marked, and seeding is not the fix.
  - **Threaded wherever notes are in hand**: `pick_next`, `read_project`'s `denied`, `coverage` (a parked row counts as denied, so `marked + denied == open` holds), `dispatchable` (the run record's `next_task` claim), and `observe`'s echo. `marker_drift` deliberately gets no notes — a parked row is not an evaporated marker.
  - **What it does not do.** It writes nothing into the target — no un-marking, no `Blocked by` clause — and it does not exempt the parked note from the dirt gate. So a skipped park usually still ends the invocation — exit 3 when nothing else is marked, exit 2 on dirt otherwise — with no worker, no record, and no review. Why: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — parked note and the dirt gate (CBN-252)". The first park still writes its `worker-parked` record.
  - **Attended reads never deny on it**, for the reason `[handoff]` gives: with an operator present, resuming the row is the work.
- **Early audits — the `audit` dimension (CBN-309).** A *marked* `.N` audit row — or a numbered audit child `.k` whose `| shortname` ends in the word `audit`/`audits` (CBN-350) — whose epic still has an unchecked sibling (same `epic_stem`; the parent row has none) denies on launching tiers as `audit — epic siblings still open (<ids>); the .<suffix> audit runs after they close` (`.N` or `.k`). Every open sibling counts, as at `/ft-close-epic` Step 2, so an epic carrying both audit forms denies both until the operator resolves it. Dispatch-side backstop to flaitron's candidacy screen (CORE-736). Why, and the incident: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — `audit` dimension (CBN-309)".
  - **Checked after the marker**, so an unmarked `.N` keeps its `unmarked` reason and `seeded_line`. `_unmarked_triage` mirrors the dimension for exactly that reason (CBN-353): the unmarked audit child is triaged `needs-judgment`, so the worklist never offers a seed this dimension then denies.
  - **Fed by `open_ids`** (`open_task_ids(tasks)`), threaded beside `completed_ids` on every launching-posture read: `pick_next`, `denied`, `coverage`, `dispatchable`, `observe`'s echo. Siblings are keyed by `epic_parent`, so both child-id forms are one cohort. Omitted `open_ids` denies (bias-to-refuse); an empty set skips the dimension, which the drain pin (`_marked_ids`, CBN-246 — as it empties `Blocked by`) and `marker_drift` (sequencing is not an evaporated marker) pass.
- **Marker drift — the evaporation guard (CBN-125).** Coverage is a point-in-time figure with no memory, and the fleet edits PLAN lines for reasons that have nothing to do with markers: a flaitron line rewrite that preserves the visible task text silently drops the trailing `[unattended]` bracket token. `flaitron.marker_drift(tasks, baseline)` diffs the current read against a marked set a past run recorded (§"Marked-set baseline") and returns one `Denial` per row that was armed then and does not dispatch now, in the same dispatch order and from the same `_denial` constructor the other two lists use. The live incident that motivated it: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — drift incident (CBN-120.2, CBN-125)".
  - **Scoped to rows still in the open launch queue** (`queue_denial` is `None`): armed then, still here, not armed now. The false positives this drops: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — drift scope (CBN-125)".
  - **The verdict is `eligibility`, not the marker token alone**, so the guard cannot drift from the behavior it describes — and it also catches stale `Blocked by` clauses that arrive in the same rewrite.
  - **The baseline is passed in, never fetched.** `flaitron/` reads producers and holds no handle on Caobunga's own store (§"Fleet boundary"); the join happens where the two already meet, in `cli.py`.
  - **Report-only, and stated even when empty.** Nothing refuses a dispatch over drift and nothing re-arms a row — same posture as `unparsed`, and the read-only bound below is unchanged. `caobunga status <id>` always prints one `drift:` line naming which of three states it is in — rows drifted, nothing drifted, or no run record carries a marked set. The line names the run the baseline came from, so the operator can see how old the comparison is. `caobunga status` (fleet) affixes `(N drifted)` to the coverage cell only when nonzero, sharing one parenthetical with the `unparsed` affix.
  - **CLI-only; the Status API gains no drift field, deliberately.** Why always stated, and why CLI-only: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — drift is always stated, and CLI-only (CBN-125)". `Coverage.marked_tasks` does reach the *detail* endpoint, since it is the missing half of the split this section already documents (the aggregate surfaces count, the per-project ones name ids); the counts-only list endpoint excludes it exactly as it excludes `denials`.
- **Read-only in both directions.** Coverage tells the operator which PLAN lines to mark; Caobunga never writes a marker into a target repo. Seeding stays a manual operator act — the same boundary "explicit registration only" draws around the registry. Emitting a paste-ready line does not bend this ([decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — emitting a seed line is still read-only (CBN-119.3)").
- **Seeding practice and cross-repo access.** A mark/no-mark proposal states stem ordering as a reason: don't mark a row whose stem predecessor is open unless it is the intended head. Cross-repo reads run under the operator's standing `~/Code` path-access root; a cross-repo write is a narrow hand-off prompt (exact lines, explicit no-touch list, commit instruction) for a session already rooted there, bounded by the read-only rule above rather than the hook; prefer a direct read over a transcription ([decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — seeding practice and cross-repo access (CBN-120.2, CBN-123, CBN-133)").

`flaitron.try_read_project(root)` is the shared skip-not-fail wrapper the report surfaces use: an unreadable or non-Flaitron path answers `None` rather than raising, so one moved registry path costs one row's figure instead of the whole report. A `PLAN.md` that is not valid UTF-8 is the same refusal as a missing one — `read_project` raises `UserError` (exit 2) rather than a raw `UnicodeDecodeError`, never picking off a lossy read — so the reports answer `None` for it too, and a mid-run `wait.poll` (which deliberately does not catch `UserError`) aborts with that named cause instead of `orchestrator-error` (CBN-323). An `OSError` anywhere in the read (the PLAN's `PermissionError`, the `is_file()`→`read_text` race, an unlistable tasknote directory) also answers `None` here, but `read_project` still raises it raw: `wait.poll` treats it as transient (CBN-168), so mapping it to `UserError` there would turn a transient into an abort (CBN-325).

### Parse contract

The reader parses two producer-owned shapes, and neither is caobunga's to define.

**Task lines.** Grammar is flaitron's `SPEC.md` §"Task-line format" (per-segment semantics as of v5.33.0 live in `SPEC/task-line-segments.md`), **pinned at v7.2.0** via the `.flaitron/core` submodule. `TASK_LINE` in `caobunga/flaitron/grammar.py` additionally accepts the four decorations flaitron's own `viz/src/parser.ts` tolerates — a leading nav-header status glyph, stacked `[model]` tokens, a `🧠`/`🧩`/`🔧`/`💎`/`🔭` suggestion glyph on either side of the trailing-token run (CBN-167, mirroring upstream `CORE-502`), and `[!critical]` on either side of `[model]` — so a hand-decorated board row is read rather than dropped. Both glyph placements parse to identical `PlanTask` fields; `unparsed_plan_rows` still reports the placements the grammar has never tolerated (`[!unattended]`, or `[unattended]` with no `[model]`).

**Every pin move is re-verified by reading, never rubber-stamped.** Outcomes so far: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Parse contract — pin-move history (v5.21.0 → v5.33.0)".

The pin is enforced, not merely declared: `backend/tests/test_contract.py` compares the version named above against the `**Version:**` line the submodule declares. **A flaitron bump therefore fails the suite until this section is updated, and that failure is the point** — it is the prompt to re-verify the grammar before the pin moves. Re-read §"Task-line format", diff the submodule's own `viz/src/parser.ts` across the tag range (`git -C .flaitron/core diff <old>..<new> -- viz/src/parser.ts`) since it is `TASK_LINE`'s upstream twin and moves faster than the prose does (CBN-112), confirm `TASK_LINE` in `caobunga/flaitron/grammar.py` still covers both, diff `templates/tasknote-template.md` and `templates/tasknote-micro-template.md` across the same range for the headings §"Fleet boundary" records `phase_progress()` and `brief.py` reading (CBN-235 — the template guard in the same test file fails on its own if a heading moved), then move the version here. Why the pin is written and enforced: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Parse contract — why the pin is enforced".

**What the guard test does not catch (CBN-111).** Both reads above — the pin named here and `SPEC.md`'s own `**Version:**` line — come from the *same* local `.flaitron/core` checkout, so the test can only fail when they disagree with each other. Currency against flaitron's latest tag is the "version pin" axis of Fleet HQ's alignment check, outside this suite by design: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Parse contract — the guard's self-consistency limit (CBN-111)".

**A second read of the trailing-token run needs no grammar move (CBN-136).** `TASK_LINE`'s group 5 — `((?:\s+\[[a-z][\w.-]*\])*)` — captures every trailing bracket token, and `EXTRA_TOKEN.findall` returns them all; `unattended` is picked out of that list *by name*. `[handoff]` joins it with one added membership test and no regex change. **Caobunga reads the token permanently unratified ([[CBN-137]])**, which costs nothing while flaitron documents trailing bracket tokens as tolerated and dropped (except `[unattended]`). The token carries the same two placement footguns `[unattended]` does: written before (or without) `[model]` it is captured *as* the model, and `[!handoff]` matches no slot — which drops the whole line for *this* reader, while flaitron's own parser surfaces it in `parsePlanWithDiagnostics`'s `unparsed`. Why the upstream reservation was De-scoped rather than filed: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Parse contract — `[handoff]` stays unratified (CBN-136, CBN-137)".

**Tasknote frontmatter.** Reading is **skip-not-fail**, the same policy §"Run record" applies to run records. A tasknote whose frontmatter is missing, valid YAML that is not a mapping, or a mapping whose `status` is not a string is treated as having *no* open status — the note drops out of the open set and the run continues on the remaining candidates. It is never raised. One malformed note costs one candidate; raising would have let it block the whole project's pick.

**Unparseable YAML degrades one rung less (CBN-186).** When `yaml.safe_load` fails, the two keys this reader needs — `status:` and `park-reason:` — are re-read **line-wise**, verbatim, off the frontmatter text; only a block with no recoverable `status:` line still drops out. The park is the fact, and a broken sibling value must not cost it. The incident and the upstream fix: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Parse contract — unparseable-YAML fallback (CBN-186, CORE-599)".

**The file can vanish, not just its frontmatter (CBN-168).** The watch loop globs `tasknote/` and reads each hit, and a closing worker's `git mv` into `archive/` lands between the two exactly when the watchdog wakes on that event. That is a transient state, not a malformed note, and it is answered the same way: the read returns *no* open status, the note drops out of *this* poll, and the next poll sees the archive. The same rule holds one level up — `wait_for_close` treats an `OSError` out of `inspect_close` (the PLAN's own `is_file()` → `read_text` gap included) as "not closed yet" rather than a verdict, so no poll ever ends the run over filesystem timing; a persistent problem still surfaces as `worker-exited` / `timeout`. `UserError` is not caught there: a PLAN missing at `is_file()` time remains the registration error the paragraph below names. Its one exception is `GitUnreadable` (CBN-269) — a `git` read that hung past `GIT_TIMEOUT` or found no binary — which is transient on the same terms and escalates the same way; outside the poll (the pre-launch `HEAD` read and the operator-presence dirt read, CBN-317) it refuses exit 5 as `git unreadable in …`, distinct from `not a git repository`. What the race cost before the fix: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Parse contract — vanished-file race (CBN-168)".

Two keys are read: `status`, and `park-reason:` when the producer wrote one (§"Park reason"). The reason is an annotation on a park rather than the park itself, so it degrades one step further — a non-string value costs only the reason and leaves the note's status intact. Its `<code> — <prose>` grammar is Flaitron's (`SPEC.md` §"Tasknote frontmatter", pinned above), split on the first separator by `flaitron.split_park_reason` and nowhere else.

The narrow scope is deliberate. Leniency covers only the shapes a producer's own editing can break. A missing `.flaitron/PLAN.md` is still a user error (exit 2), because that is a registration mistake rather than a malformed file.

### Unattended capability probe

A third producer read, and the only one that is not a parse: `flaitron.supports_unattended(root)` answers whether the target's **pinned** `/ft-task` recognizes `--unattended`, so §"Run loop" can pick the posture flag that pin actually honors.

It reads `<target>/.flaitron/core/claude/skills/ft-task/` when that submodule exists, and is `True` only when **both** hold: `unattended-mode.md` is present, and `--unattended` appears in that same directory's `SKILL.md`. Both halves are required — the module alone would break the skill mid-run, and the token alone would parse into a missing module.

**Second location, only when the pin cannot exist (CBN-156).** If `.flaitron/core` is absent, the same two halves are read from `<target>/claude/skills/ft-task/` — flaitron-self's own skill tree, registered 2026-09-11, which has no submodule to pin. An adopter whose `core` path exists keeps the pinned read even if that tree is also present; an empty core is still "present" so a broken pin cannot silently answer from unpinned files.

**A capability probe, not a version floor.** The question that decides the outcome is whether *this* pin's arg parser accepts the token, and a declared tag can disagree with the files on disk. Why a version comparison was rejected: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Unattended capability probe — version floor rejected".

Neither the submodule path nor the fallback is the target's `.claude/skills/ft-task` symlink, which is the wired copy of a pin, not the pin ([decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Unattended capability probe — why not the `.claude/skills` symlink").

Reading is **skip-not-fail** like the two parses above, and fails toward the recognized flag: a missing `.flaitron/core`, an unreadable directory, or undecodable text answers `False` rather than raising. A wrong `False` costs posture and never a run, which is why the failure direction is the safe one.

**The filing probe is retired (CBN-330).** It answered whether the pin's filing skill accepted `--unattended` (CBN-138.2) and, after flaitron v7's rename, which slug it shipped (CBN-329). The v7 hard cut leaves one answer, so §"Run loop"'s follow-up cue is static. History: [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Unattended capability probe — filing probe is token-only (CBN-138.2)".

## Worker interface

Library, not a CLI verb. `caobunga.worker.Worker` is the Protocol the run loop calls;
`caobunga.worker.Session` is the handle it passes back in. One adapter ships —
`local` (below); the `cao` HTTP adapter was retired in CBN-204 (history at the end
of this section).

| Op | Contract |
|---|---|
| `launch(cwd, *, provider, model, effort, session_name, log_path)` | Reserve a session rooted at the project cwd. Does not deliver a prompt. Returns a `Session`. `effort` is the rung from `taskmap.resolve_effort` (§Task-class → model map); an adapter that cannot carry it accepts and ignores it. `log_path` (CBN-162) is the file this session's stdout/stderr get teed to; an adapter with no raw subprocess accepts and ignores it the same way. |
| `send(session, message)` | Hand the worker its prompt. Does not wait for completion. |
| `status(session)` | Peek at liveness. Never reads or returns worker output. |
| `exit_code(session)` | Process return code once exited, when the adapter has one. A number, not output — distinct from `status`'s transcript ban. `None` when the adapter has no such concept or the worker hasn't exited. |
| `stderr_tail(session)` / `stdout_tail(session)` | Last `local.TAIL_LINES` non-empty lines per stream, oldest first, in memory (CBN-282) — a multi-line refusal ends in `}`, so the usage-limit match reads these, and `wait._exit_detail` quotes each stream's `_summary_line` pick into a `worker-error` / `worker-exited` / `timeout` `summary`, `clip()`ped like every other fragment (CBN-162 stderr, CBN-200 stdout, CBN-283 informative-line pick; CBN-292 stdout cause pick; the former `last_stderr_line` / `last_stdout_line` ops folded in here). Bounded facts, not a transcript. Empty when the adapter keeps none. |
| `shutdown(session)` | End the session. Already-gone is success (idempotent). |
| `close()` | Release adapter-owned resources. |

The Protocol declares exactly what `loop.execute_run` calls, and nothing an adapter
keeps for its own convenience — an adapter that never needs such a helper
implements nothing dead.

Statuses the run loop reacts to. Anything else (`idle`, `processing`, …) keeps the
close wait polling:

| Status | Meaning | Loop behavior |
|---|---|---|
| `error` | Worker reached a terminal error | `WorkerError` (exit 4) |
| `exited` | One-shot worker process finished | Re-check close once; if still open, `WorkerError` |

`exited` is how a one-shot CLI reports finishing: it can exit 0 having closed
nothing, and without this status the loop would poll until `CAOBUNGA_RUN_TIMEOUT`
(default 7200s) before escalating. A persistent session could never report it
([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Worker interface — the retired `cao` adapter").

Selection is `CAOBUNGA_WORKER` (`local`, the only value; default `local`), read by
`worker.select_worker()` at the `caobunga run` call site. An unknown value —
`cao` included, since CBN-204 — is a `UserError` (exit 2). Library callers that
inject `worker=` bypass selection and own the adapter's lifetime.

**Accept-and-ignore fields.** The Protocol declares every field the loop has
(`effort` and `log_path` included), and an adapter that cannot carry one accepts
and ignores it rather than raising — the Protocol never forks into two shapes, and
a future adapter inherits the same contract. Why: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Worker interface — why accept-and-ignore fields".

### `local` — LocalWorker (CBN-EPIC-9.2)

One already-authenticated provider CLI, spawned headless in the project cwd. One task,
one process. The reference adapter, and the only one shipped.

`launch` resolves the cwd and the binary (via `usage.BINARIES`) and reserves the
session; nothing spawns until `send` supplies the prompt, because a one-shot CLI has
nothing to run without one. `status` before the first `send` is `idle`; while the
process runs, `processing`; on exit 0, `exited`; on any non-zero exit, `error`.
`_default_spawn` starts the child in its own process group (`start_new_session=True`);
`shutdown` sends the group `SIGTERM` via `os.killpg`, then `SIGKILL` after a grace
period (`ProcessLookupError` and macOS's all-zombie-group `PermissionError`
suppressed, CBN-349), mirroring `suite_verbs._default_run` —
so a worker's grandchildren (shells, MCP servers, test runners) don't outlive
the run record (CBN-EPIC-243.2).

`exit_code` returns the subprocess return code once it has exited (`None` before
`send` or while still running). `wait.wait_for_close` folds
`_exit_detail` (exit code plus one picked stderr/stdout line each) into the `WorkerError`
message for `STATUS_ERROR` as well as `WORKER_EXITED` / `TIMEOUT`. The
parenthetical lands in the run record's `summary` — facts, not a transcript.
History: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Worker interface — exit code and output tee (CBN-110, CBN-162, CBN-200)".

`stdout`/`stderr` stay `DEVNULL` when the caller passes no `log_path` — the
pre-CBN-162 behavior, unchanged. When `log_path` is given, `send` pipes both
streams instead and tees them line-by-line, on background threads, into a
`local._RotatingLog` at that path (capped at `local.LOG_MAX_BYTES`, one `.1`
rotation on overflow — §"On-disk layout"). `stderr_tail` / `stdout_tail`
(CBN-282) each return the last few non-empty lines captured this way for
their stream; nothing else in this adapter reads the tee back.
A recipe with `stream_argv` (only `claude_code`) streams JSON events, so the log
fills live; `local.compact_stream_line` logs one line per tool call or message
and tails only the result text, verbatim (CBN-377).

Headless argv per provider, verified from each CLI's own `--help`:

| Provider | Binary | Argv |
|---|---|---|
| `grok_cli` | `grok` | `--always-approve -m <model> --reasoning-effort <effort> -p <prompt>` |
| `claude_code` | `claude` | `-p --output-format stream-json --verbose --permission-mode bypassPermissions --settings <guard-settings> --model <model> --effort <effort> <prompt>` |
| `codex` | `codex` | `exec --dangerously-bypass-approvals-and-sandbox -m <model> -c model_reasoning_effort=<effort> <prompt>` |
| `cursor_cli` | `cursor-agent` | `-p --force --trust --model <model> <prompt>` |

`<guard-settings>` is `local.CLAUDE_GUARD_SETTINGS` — one inline JSON `--settings` payload registering `python -m caobunga.hooks` (this orchestrator's own interpreter, `shlex`-quoted) as a `PreToolUse` command hook matched on `Bash|Write|Edit|MultiEdit|NotebookEdit`; it denies `run_in_background` with `FOREGROUND_ONLY`'s instruction as the reason (CBN-212, below), and denies `caobunga register` or a file write under the store home (CBN-247, below). `build_argv` emits the pair only when `local.hooks_available()` is true (`importlib.util.find_spec("caobunga.hooks")`); a snapshot install that predates `hooks.py` omits it, and fleet `status` affixes `hook: present` / `hook: missing` on its `source:` line (CBN-216). The table above is the present-hook form. Only the `claude_code` recipe carries a guard (why: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Worker interface — guard hook and effort slot shape"). That leaves a known gap on `grok_cli`, `codex`, and `cursor_cli`: nothing mechanical stops those workers from backgrounding a call or rewriting the registry. The prompt sentence is the only thing that reaches them. `<effort>` is the rung from `taskmap.resolve_effort` (§Task-class → model map),
carried per provider by `Recipe.effort_argv` and emitted only when the loop supplies
one — omit it and the argv is byte-identical to the pre-CBN-EPIC-15.2 form. Cursor
has no fragment (effort rides its model id); Codex's is a tuple because its effort
is a config override (`-c key=value`). Why: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Worker interface — guard hook and effort slot shape".

Auto-approve is unconditional and deliberate: an unattended worker that stops to ask
has nobody to answer it. The worker can therefore do anything in the target repo —
register only projects whose blast radius you accept. A provider with no row is a
`UserError` (exit 2) naming the supported set, as is a missing cwd; an uninstalled
binary is `Unlaunchable` (exit 5, CBN-EPIC-313.5). Bump a row when a CLI renames a flag; do not add a provider without
checking its `--help` first.

This table is deliberately narrower than the five-token v1 provider set the
registry allow-list validates against (§Registry), so a `providers` entry outside
these four is reachable through a fully valid registration and surfaces here, at
launch, rather than at register time (CBN-097).

**Transcripts stay disposable.** stdin, stdout, and stderr are all `DEVNULL`. The
adapter never reads or stores worker output, so a failed run reports its exit code
and nothing more. `close.inspect_close` (git + PLAN) remains the only close signal.

### History — the retired `cao` adapter

CBN-204 retired the `cao` HTTP adapter (CBN-012), its `CAOBUNGA_WORKER` value, and
its pre-launch probe. The provider-token vocabulary (`grok_cli`, `claude_code`, …)
is CAO's and stays — it names providers, not the adapter. History:
[decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Worker interface — the retired `cao` adapter".

## Run loop

`caobunga run <project>` (CBN-013 + CBN-050 + CBN-051). Composes `Store.get` → `read_project` → (if launching) the worker named by `CAOBUNGA_WORKER` → the deterministic suite on a closure outcome (below) → `Store.write_run` → `Store.save_suite` → last-run card (§"Last-run card") → epic rollup when a cohort just completed. Later-tier loops re-read PLAN after each close and reuse that same per-task write + card path. Retired steps: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — Hermes-era steps retired (CBN-258)".

Close detection fires on the worker's commit, but the worker is still finishing its own post-closure git work at that moment. Before any review step touches the worker repo, the loop waits for the worker to stop reporting `processing` — up to `SETTLE_GRACE` (30s), then proceeds regardless (CBN-EPIC-9.5). A grace, not a gate: a wedged worker costs one pause, never an abort. That post-closure work can amend the detected close commit off-branch, so once settled the loop re-reads a closure whose detected `commit_sha` is no longer reachable from `HEAD` (`close.reinspect_settled`, CBN-275) and records the final commit, tasknote, and summary; at that settle re-read, a commit stacked *on* the close leaves it reachable and the record unchanged (the post-suite pass below does attribute it); an amend plus a stacked commit re-reads to the amended close with the stacked commit foreign (CBN-279); and a git read that fails outright, or a re-read that no longer classifies as a closure, keeps the first snapshot. The worker can also outlive both the grace and the suite. In CBN-351 a `## Completed` → `PLAN-ARCHIVE.md` rotation either committed mid-suite (visible as that verb's `tree_changed`) or was killed half-done. So once the suite returns, the loop shuts the session down *before* writing the record and re-reads the summary's `tree:` line from the now-final tree (CBN-352). The worker keeps the whole settle-plus-suite window, and the record never says `0 file(s) left` over dirt that the next dispatch would refuse on. Between the shutdown and that tree read, the closure is re-read once more, this time even when the close is still on-branch (CBN-356). A commit stacked during the suite, like a fleet repo's rotation, then lands in `foreign_commits`, and an amend moves `commit_sha` to the amended close. The measured `suite` is kept. That pass is strict (CBN-357): a re-read of a still-reachable close that git cannot attribute keeps the first snapshot instead of falling back to `HEAD`, while one that names a different commit is a real re-attribution (a no-tasknote close whose note is archived in a stacked commit) and is adopted.

**Phase progress echo (CBN-214).** While that close wait is still open, each poll peeks at the candidate's open tasknote (`.flaitron/tasknote/{id}.md`) and prints one `phase: {n} {name}` line the first time a new Flaitron phase heading has been reached — `1 discovery`, `2 execution`, `3 testing`, `4 closure`. Phase 1 is reached as soon as the heading exists; a later phase is reached when it contains a checked box or every checkbox of the previous phase is ticked. A micro-tasknote (`## ⚡ Notes`, no `Phase N`) prints `phase: notes` once. Echo-only, like `eligibility:` / `foreign:`: not a run-record field, not a dashboard line, and not a worker transcript — the loop does not forward stdout. A missing or unreadable note is skip-not-fail (CBN-168's vanished-path posture); the same token is never printed twice.

**Deterministic suite (CBN-EPIC-202.3).** After the settle, on **every launching tier** — `gated` included; only the *continue* is `proceed-on-green`'s (CBN-EPIC-202.4) — and only when the close carries a closure commit (`closed` / `de-scoped` / `no-tasknote`; a park or a failure left nothing closed to measure, and §"Suite" forbids the field there), the loop runs the three fleet verbs against the tree the worker just closed: `just test`, `just lint`, `just typecheck`, in that fixed order, each under `suite_verbs.TIMEOUTS` (600 / 120 / 300 s). They are the fleet's verbs, so a `just`-less or `justfile`-less target is a `refused` result rather than a skipped step ([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — suite verbs and the `refused` stop (CBN-EPIC-202.3, CBN-EPIC-202.4)"). `suite.run_suite` composes `suite_verbs.run_verb` — the fixed argv, the registry check, porcelain before and after, the detached process group killed on timeout or any other exception mid-run, an interrupt included, its post-kill drain bounded and its output decoded with `errors="replace"` (CBN-174, CBN-EPIC-314.2) — by call, not by copy, and **never raises**: a refusal, a timeout, or a spawn error is a `refused` / `timeout` entry for that verb (CBN-171's posture for loop-side steps), folded by `models.Suite.fold` into the one `verdict` a gate reads. The result rides the **single** record write as `suite` (§"Suite") — not a second in-place rewrite — so `closed_at` ("clock at write") lands *after* the suite and a run's derived duration includes it; nothing keys on that shift. The full rendered output is saved under `suites/` (§"On-disk layout") after the write, under the record's stamp; the loop echoes one `suite: {verdict} — test {exit} {s}s · lint … · typecheck …` line — echo-only, like `foreign:`, since the record already carries every fact. **Serialized across slugs (CBN-308):** `run_suite` holds `suite.lock` around each verb's spawn, so a concurrent slug's verb waits rather than overlapping — two test pools sized to every core loaded each other into a false red; the wait is outside every verb's duration and ceiling but inside the run's (`closed_at` lands after it), and a home whose filesystem cannot `flock` runs the suite unserialized rather than raise. **The wait is echoed and a refusal never queues (CBN-310):** a verb that blocks on the lock makes the loop echo `suite: waiting for suite.lock` before it does, and the lock is taken only after the refusal check, so a `just`-less or `justfile`-less target returns its `refused` result at once instead of waiting behind another slug's suite. Why this over a rerun-on-red: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — suites serialized across slugs (CBN-308)". Injectable as `execute_run(..., run_suite=)`: tests must not run a live `just`. The worker's own Phase 3 run is not this step's evidence ([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — suite timing shift and worker-evidence incidents (CBN-167, CBN-171)").

**Suite gate (CBN-EPIC-202.4).** `proceed-on-green` continues only when the last close is `closed` **and** its `suite.verdict` is `green`; the suite rule is checked first and covers every closure outcome (`closed` / `de-scoped` / `no-tasknote`, CBN-304), so a red de-scoped close stops `suite`; the outcome rule stops the rest. `red`, `timeout`, and `refused` all stop — `refused` deliberately (no `just`, no `justfile`, an unregistered path, a justfile missing the verb's recipe — CBN-211). The stop echoes the fourth `drain:` reason, `suite` (table below). `gated` writes the field and echoes the line; it stops anyway. **The exit code does not change**: a drain that ended on a closed-but-red run still exits 0; the escalation surfaces are the record and the `suite:` and `drain:` echoes. The predicate is `suite.suite_stops`.

| Tier | Behavior |
|---|---|
| `observe` | Print id / tier / enabled / last run / next candidate / suite fitness (§"Suite fitness preflight"), then one `eligibility: {id} allow\|deny: {reason}` line per open-queue task (as-if-unattended — what any launching tier would decide, reported without launching; CBN-EPIC-17.2). Do not launch. Do not write a run record. Exit 0 even if PLAN is exhausted. |
| `gated` | One next task, one fresh worker session, wait for Flaitron close, write a run record, publish the last-run card, stop. Picks consult the `[unattended]` marker (CBN-118): an unmarked candidate is denied and echoed, and a repo with nothing marked refuses at exit 3 without launching or recording. The in-progress override is **kept** — `gated` is headless but watched (§"Flaitron reader"). |
| `proceed-on-green` | Same per-task runner; continue while each outcome is `closed`, each close's `suite.verdict` is `green` (CBN-EPIC-202.4), and `read_project` still has an eligible High/Medium/Low candidate. Picks consult the `[unattended]` marker (§"Flaitron reader"): unmarked tasks are denied and echoed, each pick echoes its allow, and a drain that cannot start names every denial in the exit-3 message. Stop on `failed` / `blocked` / `de-scoped` / `no-tasknote`, and on a `closed` run whose suite is `red` / `timeout` / `refused`. Scope a drain to one cohort by marking only that cohort's rows — the `plow` tier that used to derive that scope from the epic stem was retired at CBN-134 (§"Execution tiers"). |

**Unparsed rows are echoed, never refused (CBN-119.2).** Every launching tier's pre-flight prints one `unparsed: line {n} — {raw text}` line per PLAN row the grammar dropped (§"Marker coverage"), beside the eligibility echoes; a nonzero count also rides on the exit-3 message (`…; {n} unparsed row(s) — see line {n}`). Nothing refuses over it — the rule is in §"Marker coverage"; the three arguments against a refusal are in [decisions/flaitron-reader.md](decisions/flaitron-reader.md) §"Marker coverage — `unparsed` stays report-only (CBN-115, CBN-EPIC-18.4, CBN-119.2)". `observe` is unchanged — it dispatches nothing, and `caobunga status` is its seeding surface.

Disabled projects (`enabled: false`) refuse with exit 2.

**Run mutex (CBN-EPIC-16.2, CBN-EPIC-313.2, CBN-EPIC-313.6).** One `run` per repo at a time: after the registry validation (unknown / disabled keep their own messages), the loop takes the per-repo non-blocking lock (`$CAOBUNGA_HOME/run-locks/{repo_key}.lock`, keyed on the resolved, case-folded git toplevel of the record's path (`git rev-parse --show-toplevel`; the path itself outside a repo or when git cannot answer) rather than its slug) and holds it across the whole invocation — every tier, observe included. A second concurrent `caobunga run` on the same repo — the same slug, or another id registered at the same path or at a nested path of the same checkout, as `fleet` would launch side by side — refuses with exit 2 (`another caobunga run is already active for {id}'s repo {path}; not launching a second`) before reading PLAN, writing any record, or launching a worker; `status` remains the read-only peek at a project mid-run. Different repos — linked worktrees included, each with its own index and `HEAD` — run concurrently.

**Operator-presence gate (CBN-EPIC-17.3).** Caobunga does not dispatch into a repo someone is standing in. Before each launch — after the target's git-repo check and before the provider pick — the loop reads `git status --porcelain -uall` in the target — with `--no-optional-locks` (CBN-261), so the read never takes the target's `index.lock` — and refuses with exit 2 (`{id}: target repo has {n} uncommitted change(s); commit, stash, or discard them, then re-run: …`, naming every line, plus a `/ft-task {id}` hint per open tasknote, CBN-287) when it is dirty. Like the run mutex, the refusal writes no record and launches nothing; an echoed `allow` gets `allow withdrawn: dirty-tree` (CBN-288). It covers **every launching tier**, `gated` included. `observe` is unaffected because it never dispatches.

**A filing gets its own message, never its own exemption (CBN-138.4).** When *every* blocking line is a path a worker writes to file a discovery — `.flaitron/PLAN.md`, `.flaitron/sidequest/*`, or a tasknote — the refusal says so and offers the commit (`{id}: target repo has {n} uncommitted change(s), all of them flaitron filing artifacts a dispatched worker writes; if a worker filed this, commit it: git -C {root} add .flaitron && … — then re-run: …`). Three properties bound it. The predicate is **all-or-nothing** — one line of operator dirt returns the generic message verbatim. It is **path shape only**, never file content: an unrecoverable path (git's C-quoting) is never a filing, and a collapsed `?? .flaitron/` directory line matches nothing and falls back. And Caobunga **prints the command, never runs it** — cleaning a target tree is as much the operator's act as seeding a marker (§"Marker coverage", read-only in both directions). The exit code, the placement, and the set of lines that block are untouched; the report states the same condition (§"Target-tree report"). Why a message and why these bounds: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — filing-artifact message (CBN-138.4)".

The read is deliberately **not** skip-not-fail (contrast §"Flaitron reader"): an answer git cannot produce is not "clean", so an unreadable status refuses too — exit 2 (`cannot read git status`) when `git status` exits nonzero, exit 5 (`GitUnreadable`, `git unreadable in …`) when `git` hung past `GIT_TIMEOUT` or is missing, the same code the pre-launch `HEAD` read gives that cause (CBN-317). The status report renders both as `unreadable`. It is a *safety* boundary, not workflow hygiene: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — operator-presence gate scope (CBN-EPIC-17.3)".

**The candidate's own tasknote is the one exemption.** A stranded `in-progress` note is *always* uncommitted (Flaitron commits it only at Phase 4), so without the exemption every `CBN-109` resume would refuse. `-uall` is part of the exemption, not a verbosity choice. The exemption matches the parsed porcelain path, not a git pathspec. A half-edited source tree still refuses, and the operator cleans it themselves (v1 failure policy). A leftover `caobunga-status.md` from before CBN-188.3 is ordinary dirt — the operator's to `git rm`. Why: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — candidate-tasknote exemption and `-uall`".

**The derivation lives in `presence.py`, not in the gate (CBN-130).** `git.git_dirt` reads the tree and `presence.tree_state` applies the exemption by matching the parsed porcelain path against the open tasknotes. The gate refuses on that result, and the status report renders the same one — one implementation of one safety rule ([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — why the derivation lives in `presence.py` (CBN-130)"). A porcelain line whose path this reader cannot recover — git C-quotes unusual paths — stays **blocking**, so the parse fails in the same direction the read does (§ above: an unreadable answer is not "clean").

Worker: `pick(plan_model, provider=record.provider, model=record.model, providers=record.providers)` (registry pins; omit = leftover policy). Close wait: `CAOBUNGA_RUN_TIMEOUT` seconds (default `7200`). Pick `ValueError` is a refusal: no eligible worker or a pinned provider not installed (`ProviderUnavailable`) is `Unlaunchable` (exit 5, CBN-EPIC-313.5); an unknown pin or model-without-provider is `UserError` (exit 2). Either way, do not write a run record and do not launch. Why-log / `reason` stay off the run record.

**Usage-limit re-pick (CBN-213).** After a worker session ends in `error` or `exited` without closing, if the stderr/stdout tail (CBN-282) carries a usage/session-limit refusal **and** `git status --porcelain -uall` is empty **and** HEAD is unchanged from the run's `start_sha` (CBN-260; unreadable counts as moved) **and** the registry has no hard `provider` pin, the loop treats that provider as `leftover=exhausted` for this invocation only, shuts the failed session down, echoes `usage-limit: {provider} exhausted this invocation; re-picking`, and re-calls `pick` with the overlay. The next allow-list member (or the next installed `V1_PROVIDERS` member when no list is declared) is launched against the same task. If `pick` then has no remaining member, the last failure is the run record — one record per task, not one per attempt. The same holds when the re-picked provider cannot launch (a `UserError` from `launch`/`send`, e.g. `Unlaunchable` for a binary gone since `which`): the loop echoes `usage-limit: re-pick {provider} cannot launch: {error}` and records the last failure, attributed to the provider that ran (CBN-316); a refusal before any worker ran still exits without a record. A pin, a dirty tree, a moved HEAD, a `timeout`, a `worker-launch` failure, or a tail with no usage/session-limit phrase (including a generic rate-limit 429) escalates immediately, as before. This is not a leftover-dump parser and does not write `DUMP_ARGV`. The overlay is not carried across invocations: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — usage-limit overlay stays per-invocation (CBN-289)".

Worker prompt: `/ft-task {id} --unattended` when there is no open in-progress note (CBN-EPIC-14.3, superseding CBN-013's `--fast`). Every tier sends it, `gated` included; gates the worker cannot answer **park** the tasknote (`status: blocked` + `park-reason:`), recorded as `outcome: blocked` / `escalation: worker-parked`. Why every tier: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — `--unattended` on every tier (CBN-EPIC-14.3)". If the candidate's open note is `in-progress`, send a continue prompt instead — `/ft-task` is start-only — and that prompt carries the posture too (§"Continue prompt"). A `blocked` open note is denied at pick as `parked` (§"Flaitron reader", CBN-252); no record is written. No prompt shape sends `--micro` (§"External review", CBN-338).

**Target-pin guard (CBN-108).** `--unattended` is a *target-repo capability*, not
a caobunga-side switch: only a project whose `.flaitron/core` pin ships an
`/ft-task` that parses the token honors it. Before each launch
the loop probes the target (§"Flaitron reader" → §"Unattended capability probe")
and **degrades rather than refusing**:

Degraded prompt: `/ft-task {id} --fast` when the target's pinned `/ft-task` does not recognize `--unattended`.

**Pinned-skill cue (CBN-291).** Both fresh-launch prompts above (not the
continue shapes) carry one sentence after the posture flag:

```text
This repo pins /ft-task at `{skill}`: if the copy you loaded is a different file, follow the pinned one instead.
```

`{skill}` is `layout.pinned_skill_rel`: the `SKILL.md` beside §"Continue
prompt"'s `{module}`, and only when it is on disk; otherwise the sentence is
omitted. Why: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — pinned-skill cue, not a skew refusal (CBN-291)".

**Foreground-only cue (CBN-199).** Every prompt shape above — both
fresh-launch variants and both continue variants — carries **one appended
sentence**, ahead of the follow-up cue below, stating the one harness fact a
headless one-shot session cannot infer for itself:

```text
This session is headless and one-shot: run every command in the foreground and wait for it to finish — a background task's completion never re-invokes you, and ending your turn ends the session.
```

It is the launcher's fact to state, not flaitron's: static and provider-neutral, with no probe and no degrade. The failures behind it: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — foreground-only cue incidents (CBN-199)".

**The sentence did not hold, so it is also a denial (CBN-212).** The `claude_code`
recipe carries `--settings <guard-settings>` (argv table above): a
`PreToolUse` command hook on `Bash`, `caobunga.hooks.guard`, that
exits 2 on `run_in_background` with the same instruction on stderr, which
Claude Code hands back to the model as the reason the call was refused.
Hooks run in every permission mode; only `--bare` skips them, and no recipe
carries it. The prompt sentence stays — the model meets one rule at two points.
The guard screens the harness parameter only, not `&` / `nohup` inside a
command string. Incident, verification, and rejected options (including why a
dirty-tree relaunch is not done):
[decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — background-call denial (CBN-212)".

**The same hook guards the registry (CBN-247)**, since §"Non-negotiables"
makes registration the operator's deliberate act ([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — registry guard (CBN-247, CBN-251)"). The `--settings` matcher
also covers `Write|Edit|MultiEdit|NotebookEdit`, and `hooks.guard` exits 2 with
`REGISTRY_DENIAL` on two shapes. The first is a `Bash` command with
`caobunga register` in command position: at the start or after a separator,
past `VAR=x` assignments and a launcher (`uv run`, `python -m`). Quoted text,
such as a `grep` pattern or a commit message, is not command position and
passes. The second is a file tool whose path sits under the store home, checked
after `~`-expansion, resolution against the payload `cwd`, following symlinks,
and case-folding (macOS's default volume ignores case). The hook resolves the home as
`store.default_home()` does (`$CAOBUNGA_HOME`, else `~/.caobunga`), restated
so the hook stays off `store`'s import graph; a test pins the two together.
`local._default_spawn` re-exports `store.default_home()`'s already-absolute
value into the worker's environment (CBN-251) and strips `MallocStackLogging*` keys (CBN-379).
This is a tripwire against a drifting worker, not a sandbox. The `caobunga register`
token match is the one exception to not screening shell text, scoped to that
subcommand. A shell write into the
home (`echo >`, `sed -i`, `cp`) goes unscreened, and the three non-Claude
recipes have no guard at all (argv table above).

**Park-check cue (CBN-327).** Every prompt shape above — both fresh-launch
variants and both continue variants — carries **one appended sentence** after
the foreground-only cue and ahead of the follow-up cue:

```text
Before you end the session, re-read your tasknote: if you parked it, confirm its frontmatter says `status: blocked` — a `park-reason:` line without that status does not park the task.
```

It asks; it does not repair. Close detection still reads `status:` alone, so a
note left `in-progress` with a `park-reason:` is recorded `failed` /
`worker-exited`, never `blocked` (§"Continue prompt" close-detection steps).
Static, with no probe and no degrade: `status: blocked` is the one field every
pin's park writes, so the sentence asks a degraded pin for nothing it cannot
produce. Why: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — park-check cue (CBN-327)".

**Follow-up cue (CBN-138.2).** Every prompt shape above — both fresh-launch
variants and both continue variants — ends with **one appended sentence**
telling the worker what to do with work it discovers beyond the task's scope.
The cue is appended to the posture rather than folded into it, so four shapes
compose from one base plus one sentence ([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — follow-up cue history (CBN-138.2)").

Unprobed since the flaitron v7 hard cut (CBN-330): every target pins v7, whose
`/ft-file-task` accepts `--unattended`, so the cue is one static sentence. A
sub-v7 pin is out of support: it still gets the posture degrade below, but the
cue is not adapted and no echo flags it.

```text
If you discover work beyond this task's scope, file it before closing: `/ft-file-task --unattended`.
```

`--fast` is the fallback because every pin accepts it; the target-pin guard never
refuses. The degrade is echoed — `{id}: flaitron pin does not recognize --unattended; sending --fast`
— so a stale target pin is visible in the run rather than silently costing
posture; it is deliberately **not** a run-record field. The probe reaches the
continue prompt as well (§"Continue prompt"). Why degrade over refuse:
[decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — posture degrade, not refusal (CBN-108)".

### Target-tree report

The operator-presence gate's verdict, rendered before a launch has to reveal it (CBN-130). `caobunga status <id>` always prints one `tree:` block — `clean`, `dirty`, or `unreadable` — on the same rule §"Marker coverage" applies to drift: a guard that renders nothing when it cannot answer is indistinguishable from one that checked and found nothing. The fleet table carries a `tree` column (`clean` / `dirty (N)` / `unreadable`), and both Status API endpoints carry a `tree` field (§"Status API"), as does the dashboard.

- **Computed at the project's own tier**, not at a fixed posture — `unattended` and `drain` both move the candidate (§"Flaitron reader"), and the candidate decides the exemption. `observe` picks as `gated` would and is reported anyway.
- **The classification is the deliverable.** Each blocking line that is an open tasknote for another task is labeled as such, and when one open tasknote is the *only* thing blocking, the block states the counterfactual — the one dispatch the gate would read clean for. Why, and why the tier: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Target-tree report — why classification is the deliverable (CBN-123 F3)".
- **It states every condition the gate refuses on.** A dirty block whose lines are all filing artifacts says so (CBN-138.4), naming the condition without offering the commit ([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Target-tree report — filing artifacts and the retired `ignored` state").
- **Report-only.** Nothing new refuses, denies, or re-orders on it, and the gate's exit code and placement are unchanged. Same posture as `unparsed` (CBN-115) and drift (CBN-125): state it, refuse nothing.
- **Derived per read, never persisted**, and never written back: Caobunga does not clean a target tree any more than it seeds a marker (§"Marker coverage", read-only in both directions).

### Continue prompt

A candidate whose open note is already `in-progress` is not launched with
`/ft-task` — that skill is start-only — so it gets prose instead. That prose
**carries the posture** (CBN-109; history: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Continue prompt — why it carries the posture (CBN-109)").

Continue prompt:

```text
Continue {id}. Drive the remaining flaitron phases in `{path}` through closure. Do not re-invoke /ft-task (that skill is start-only). Run under the --unattended posture (`{module}`): at any gate you cannot answer, park the tasknote — `status: blocked` plus `park-reason: <code> — <prose>` — and stop.
```

Degraded continue prompt:

```text
Continue {id}. Drive the remaining flaitron phases in `{path}` through closure. Do not re-invoke /ft-task (that skill is start-only). Run under the --fast posture (this pin does not recognize --unattended): suppress the visual-confirmation ask and commit autonomously at closure.
```

`{module}` is the repo-relative `unattended-mode.md` path resolved by
`layout.unattended_module_rel` — `.flaitron/core/claude/skills/ft-task/unattended-mode.md`
on a pinned adopter, `claude/skills/ft-task/unattended-mode.md` on a core-less
flaitron-self (§"Unattended capability probe", CBN-156). It goes through the same
`_probe_skill_dir` gate `supports_unattended` reads, so the file the prompt names
is the file the probe proved on disk (CBN-189; [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Continue prompt — `{module}` resolution (CBN-189)").

Both continue shapes also carry the foreground-only cue, the park-check cue,
and the follow-up cue as further appended sentences, exactly as the fresh-launch
shapes do (§"Run loop"); the fenced text above is the posture half, and the four
are composed rather than written out four times. This branch needs the cues most ([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Continue prompt — the cues, and the gate exemption").

The unattended variant states the park action inline and then **points at** the
target's own fragment rather than restating Flaitron's recipe. The degraded
variant points at nothing; it names the posture that pin does honor. Why:
[decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Continue prompt — point, don't restate".

Caobunga takes the prose-continue path (flaitron `docs/EXTERNAL-AGENTS.md`
step 6, path (b)) over the frontmatter-conversion one; both are contract-legal,
and path (a) would stop the run rather than resume it ([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Continue prompt — prose continue over frontmatter conversion").

Close detection is the worker repo, never a terminal transcript. Steps 1-3 read
`HEAD`'s **committed tree** — `git show HEAD:.flaitron/PLAN.md`, `git ls-tree`
for the archive — never the checkout (CBN-158), so the two writes of an atomic
single-commit closure (§"Paper-complete guard") are only ever observed together
and `no-tasknote` means "`HEAD` has the row completed and no archived note"
([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — close detection reads `HEAD` (CBN-158)"). The open-note check in steps 4-5 stays a checkout
read — a park is inherently uncommitted, so there is nothing at `HEAD` to read.

1. PLAN row for this id is `[x]` at `HEAD` **and** `git rev-parse HEAD` moved from the pre-launch SHA.
2. Archived note at `.flaitron/tasknote/archive/*/{id}.md` at `HEAD` → `closed`, or `de-scoped` when `**Verdict:** De-scope`.
3. No archive and no open note → `no-tasknote`.
4. Open note `status: blocked` → `blocked` (SHA optional).
5. `[x]` without a new commit keeps waiting. Timeout / worker `error` → write `outcome: failed` (summary = orchestrator error string) and exit 4.

`commit_sha` is the closure commit: the first commit of the final run in `start_sha..HEAD` whose PLAN reads the row `[x]` and whose tree carries HEAD's archived note when there is one. It is HEAD unless work stacked on the close; any git read that cannot say falls back to HEAD (CBN-279), since a closed record needs a sha, except on the strict re-read of a still-reachable close (CBN-357), which keeps the first snapshot. Do not recover SHA via `git log --grep`. `summary` is the archived `**Final Summary:**` lede, or the PLAN stub for `no-tasknote`. `next_task` is the post-close `**Suggested next task:**` id when it differs from the closed id and would dispatch at this posture, else the candidate.

**`next_task` is read at the run's own dispatch posture (CBN-117).** It is a *dispatch* claim — `status_card.py` renders it as `- **next:**` into the caller-side last-run card (§"Last-run card") — so `inspect_close` takes an `unattended` flag and passes it to `read_project`. That flag is `record.tier in UNATTENDED_TIERS`, the same expression the drain uses for its own next read, and deliberately **not** `supports_unattended(root)`, the target's capability probe (CBN-108). At every posture `suggested_next` is *validated* rather than preferred — kept when `flaitron.dispatchable` says that id would dispatch at this posture, otherwise fallen through to `candidate`, which is already posture-correct; an id absent from PLAN denies, the same bias-to-refuse the safety and `Blocked by` dimensions apply. `dispatchable` resolves through `eligibility`, so the marker rule is not re-derived (§"Flaitron reader"). **The attended path is validated too (CBN-151).** Attended validation consults nothing marker-shaped: `eligibility` at `unattended=False` is the posture-blind membership and `Blocked by` dimensions only, so it drops exactly what an attended pick would refuse (a checked row, a row outside the launch buckets, a parent-epic tracking row, a `Blocked by` row) and nothing an operator would have run.

A consequence worth knowing: a drain's last record omits `next_task` exactly when the drain reached its natural exhaustion exit. Why the posture, and the incident behind it: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — `next_task` posture (CBN-117, CBN-151)".

**Every drain-ending exit names itself (CBN-119.4).** A drain echoes exactly one `drain: <reason> — <detail>` line at the return, from a closed set of five reasons:

| Reason | Emitted when | Detail |
|---|---|---|
| `complete` | no eligible candidate remains, and nothing that could have continued was denied | `no eligible candidate remains` |
| `incomplete` | no eligible candidate remains, but denied rows could have continued | the denied `id (code)` list, from the same renderer as the exit-3 message |
| `outcome` | the last close stopped the walk (`failed` / `blocked` / green `de-scoped` / green `no-tasknote`) | the task id, the outcome, and `park_reason` when the stop was a park |
| `suite` | the last close was `closed` / `de-scoped` / `no-tasknote` but its deterministic suite was not `green` (CBN-EPIC-202.4, CBN-304) | `<id> suite <verdict> — ` then every non-green verb: `<verb> exit <n>` / `<verb> timeout` / `<verb> refused: <reason>`, comma-separated |
| `unpinned` | the re-read after a close picked a row the drain's first read would not have dispatched, `Blocked by` aside (CBN-246, CBN-260) | `<id> not dispatchable on the drain's first read; not dispatched — check who changed it before re-running` |

**`complete` vs `incomplete` is scoped to the whole queue.** A drain has no cohort, so any denial is a row it would have taken. `test_contract.py` guards the table against the reasons `loop.py` emits.

**A drain is pinned to the marked set its first read saw (CBN-246).** Why: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — why a drain is pinned (CBN-246)". `execute_run` records, on the first read, the ids carrying the marker that `eligibility` allows with their `Blocked by` clause set aside, and on every later read stops with `unpinned` instead of dispatching a candidate outside that set. The pin is neither `coverage.marked_tasks` nor the bare marker (CBN-260; why: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — drain pin shape (CBN-246, CBN-260)"). Only the clause is set aside, so a row blocked *and* `[handoff]` stays out. It applies whoever marked the row — Caobunga cannot tell an operator's seed from a worker's — so an operator who seeds mid-drain gets the row on the next `run`. The stop also rides the invocation receipt (§"Invocation results"); the next `run` pins whatever is marked then, so the detail tells the operator to check who marked the row before re-running. **The pin is by id, not by row content** — a worker can still rewrite the text of a pinned row that has not run yet, or strip its `Blocked by`; the pin closes the arming of rows the first read would not have dispatched, and nothing wider. Same exit code (0) as the other drain exits. `gated` dispatches once from its first read and is untouched.

The `drain:` line is what tells an **exhaustion exit** (everything eligible was walked) from a **marker exit** (the next row was unmarked) — the two are otherwise indistinguishable on every surface ([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — drain exit reasons (CBN-119.4)"). **Separate from task history** — the `drain:` echo and task-record schema stay compatible; CBN-272 also records the reason on a separately versioned invocation receipt (§"Invocation results"). `gated` returns a single task and is not a drain, so it echoes nothing.

`started_at` is stamped when the run begins, so a record carries both endpoints whether or not a worker launched. `escalation_reason` is carried on the close snapshot by both producers (close classification and the loop's own `WorkerError` catch) and projected onto the record. The record is written once; nothing after the write rewrites it (the Hermes second write retired at CBN-258).

Exit 3 (`no next task`) writes no run record (start-of-run only). After a later-tier loop drains the eligible set, return the last run record and exit 0 — including a drain the suite gate ended on a closed-but-red run (CBN-EPIC-202.4; §"Run loop" → "Suite gate"). After each write the CLI dumps that run record as YAML (same formatter as `status <project>` last-run). Observe does not dump a record. A last-run card write failure (`ReportError`) does not rewrite the run record; the CLI still dumps it, then exits 4. Last outcome `failed` still exits 4.

**Interrupts (CBN-375):** press once, then wait for the `interrupted` receipt; repeats re-print the notice; the third force-quits, which can skip the receipt.

**One close, one block (CBN-135).** The CLI dumps each *close* once, tracking the last dumped record by identity. Every record the loop returns is its only write ([decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Run loop — one close, one block (CBN-135)").

## Invocation results

`loop.execute_invocation` returns a durable `InvocationResult` (CBN-272), including
preflight refusals that have no task record. `execute_run` remains the adapter
returning the last `RunRecord` / `None` and re-raising the original exceptions.
Both accept the same injected collaborators. Direct-run exits are 0/2/3/4/5
(§"Exit codes"). A closed
task followed by a red/timeout/refused suite still exits 0, but its
invocation is **unsuccessful**. Aggregators consume `successful`, never legacy
exit codes or the last task outcome. `fleet` does so for every child that exits
`0` (CBN-372); every other code folds on the code alone.

A launching invocation writes a `running` checkpoint, links each task record
immediately after its atomic write (before reports), then atomically overwrites
its receipt at termination. Checkpoints and
terminal writes use the Store lock and temp/fsync/replace pattern. Unknown or
invalid requested slugs are captured as data under this global directory, never
used to build paths. Known `observe` invocations remain read-only and return an
in-memory result; they write no receipt, even when refused. Persistence failures
raise rather than claim durability. An abrupt process death may leave `running`:
never success, until the slug's next launching `run` reconciles it under the run
lock (CBN-376): a receipt whose `pid` is dead (or, lacking one, whose `path` is the
registered path) becomes `abandoned` / `process-gone`, its evidence re-read, `detail`
counting commits since `started_at`, echoed as `stale-receipt:` lines. Nothing
in the target is cleaned, committed or relaunched; `observe` skips it. Why: [decisions/worker-run-loop.md](decisions/worker-run-loop.md) §"Stale running receipts (CBN-376)".

After a
launching attempt it prints `invocation:`, `invocation-record:` and `recovery:`
lines alongside the existing per-close dumps and drain echoes. An `outcome` stop adds its cause to `invocation:` (CBN-287), minus the summary when the run has a `commit_sha` (CBN-302). Multi-task runs add a per-task tally (CBN-295). Logs remain
filesystem pointers; transcripts never reach a report surface.

### Invocation status

| Invocation status | Meaning |
|---|---|
| `running` | No terminal decision; crashed/unfinished checkpoints require reconciliation |
| `observed` | Read-only tier completed; no dispatch authorized |
| `complete` | Tier-bounded approved work closed and verified green |
| `empty` | No authorized work; an unmarked-only queue is harmless but its denials remain visible |
| `refused` | Registry/readiness/routing/start refusal before any task record |
| `incomplete` | Requested work did not finish, or suite/report/shutdown/unknown-row evidence prevents success |
| `interrupted` | Caught interruption; preserve partial work and reconcile actual commits |
| `abandoned` | A later run found its process gone (CBN-376) |

`successful` and `approved_scope_finished` are true only for observed/complete/empty
with no remaining approved tasks. A committed green close (`closed` / `de-scoped` /
`no-tasknote`) drops out of `remaining_tasks` and, on `gated`, finishes its one-task scope
as `complete`; a drain still stops on the latter two (CBN-301). A drain retains
the initial safe pin and also names initially marked denied rows as unfinished
requested work. Initial dependency-only blocks stay pinned and may clear between
dispatches. Unmarked rows never become authorization. New/unpinned rows are deferred
and stop with an unsuccessful receipt. A drain may finish its approved scope while
its legacy whole-queue echo says `incomplete` over an unmarked tail. Malformed rows
remain unparsed evidence and prevent a claim of complete scope.

### Invocation fields

Invocation schema version is independent of registry/task-record versions. Lists are explicit (empty is
measured empty); unavailable optional evidence is omitted. Denials carry only
`task_id`, `reason`, `line`; task payloads live at referenced ledger paths.

| Invocation field | Meaning |
|---|---|
| `schema_version` | 1 |
| `invocation_id` | Existing compact clock key shared with task records |
| `receipt_id` | Clock plus unique random suffix; receipt filename key |
| `project` | Requested slug, including an invalid or unknown one |
| `started_at` | UTC request time |
| `pid` | Orchestrator pid; reconcile's liveness evidence |
| `finished_at` | UTC terminal decision time (`abandoned`: when found); absent while running |
| `status` | Invocation status above |
| `stop_reason` | Registry/readiness stage or existing stop vocabulary |
| `detail` | Concrete stop evidence; outcome stops include the last record's summary |
| `successful` | Aggregation success independent of direct-run exit |
| `approved_scope_finished` | Successful, with no remaining approved tasks |
| `legacy_exit_code` | Existing direct-run result; 130 on caught interrupt; absent while running or `abandoned` |
| `path` | Registered repository path when known |
| `tier` | Saved tier when known |
| `task_id` | Current or deferred task when known |
| `authorized_tasks` | Initial tier-bounded requested set |
| `remaining_tasks` | Requested tasks without this invocation's closed/green task evidence |
| `run_records` | Ordered task-record paths; payloads are not copied |
| `denials` | Latest eligibility denials, including park code/prose and PLAN line |
| `unparsed_lines` | Latest malformed PLAN row line numbers |
| `provider` | Most recently selected/current provider, or registered pin before selection |
| `model` | Most recently selected/current runtime model, or registered pin |
| `escalation_reason` | Current task record's failure classification when available |
| `last_commit_sha` | Most recent real closure SHA from this invocation's task records |
| `head_sha` | Best-effort final HEAD; not itself proof of task closure |
| `tree` | Best-effort final porcelain; absent means unreadable |
| `tasknote_path` | Current open/archived note when known |
| `tasknote_status` | Open note status when present |
| `park_reason` | Full current park code/prose when known |
| `worker_log` | Existing current worker diagnostic file |
| `suite_log` | Existing suite file for the most recently measured task |
| `timeout_seconds` | Resolved worker time limit |
| `shutdown` | Aggregate shutdown returned/failed; any failure persists; absence is unknown/no session |
| `recovery` | Safe operator steps and prerequisites for continuation |

Stop reasons preserve `complete`, `incomplete`, `unpinned`, `outcome`, `suite`;
`gated` and `observe` name tier stops. Preflight stages are `registry`, `disabled`,
`active-run`, `timeout-config`, `project-read`, `git-unreadable`, `not-a-repo`, `dirty-tree`,
`provider`, `worker-start`; `head-lost` is a post-launch stop; first-read empty/denied requests use `empty`/`denied`.
`report`, `orchestrator-error`, `interrupted` and `process-gone` identify other termination paths.
An `OSError` on the project read at `project-read` (and on `observe`'s one read) is a `UserError` (exit 2) that keeps that stage rather than `orchestrator-error` (CBN-326); only `wait.poll`'s reads treat it as transient (CBN-168).
A running checkpoint's stage is provisional, not a terminal refusal.

Recovery preserves partial work and names available evidence. A park recommends
attended `/ft-task <ID>` after resolving its reason; missing committed closure
requires inspecting HEAD/tree/note/log and resuming attended; worker failures name
routing/diagnostics; a suite stop keeps the real SHA and requires the deterministic
suite to be green before authorizing continuation, without rewriting history.
A `git-unreadable` stop names the git install / a hung git process, and leaves
`head_sha` and `tree` absent rather than re-reading the same repo (CBN-320). A
registered path that git reports has no readable `HEAD` — not a repository, or an
unborn `HEAD` — stops `not-a-repo` instead and keeps its tree read (CBN-321). A
`HEAD` that vanishes after the worker launched stops `head-lost` (exit 2), whose
recovery inspects the repo and partial work rather than the provider (CBN-322).
`shutdown: returned` reports an adapter call, not independent proof that every
descendant died; timeouts and shutdown failures require process inspection before
resume. No auto-clean, generic retry or unsafe arbitrary commit. Rationale: [decisions/worker-run-loop.md](decisions/worker-run-loop.md)
§"Invocation receipts and direct-run compatibility (CBN-272)".

## Usage adapters

Library, not a CLI verb. CBN-022's picker calls `caobunga.usage.probe_all()`. CBN-023's gated loop reaches leftover only through `pick` (inject `usage=` in tests).

v1 providers (CAO tokens, stable order): `grok_cli`, `claude_code`, `codex`, `cursor_cli`, `opencode_cli`. Binaries on PATH: `grok`, `claude`, `codex`, `cursor-agent`, `opencode`.

| Field | Type | Notes |
|---|---|---|
| `provider` | string | CAO token |
| `installed` | bool | Binary present on PATH. Present ≠ leftover available. |
| `leftover` | enum | `available` / `exhausted` / `unknown` |
| `remaining` | float or omit | Fraction remaining in `[0.0, 1.0]` when leftover is known; omit/`None` when unknown |

Best-effort: missing binary, timeout, nonzero exit, unparseable stdout, or no non-interactive leftover dump → `leftover=unknown`. Never `WorkerError`. Do not start an interactive TUI (no `/usage` inside a session). Do not read provider credential files. Do not persist the payload; it is not a report field.

Today none of the five CLIs expose leftover on a non-interactive dump. Installed CLIs still return `unknown` until a parser is wired to a stable command. `probe(..., dump_argv=…)` is the seam.

## Task-class → model map

Library, not a CLI verb. CBN-022's picker calls `caobunga.taskmap.resolve` (via `pick`). CBN-023's gated loop calls `caobunga.taskmap.resolve_effort` directly and nothing else on the map, once per pick with the provider that pick chose (CBN-332), so a CBN-213 re-pick launches at the new provider's rung. It is not folded into `pick` (CBN-EPIC-15.3). Does not call `usage.probe`.

`plan_model` is the PLAN.md `[model]` token (`NextTask.plan_model`). Runtime `model` is the string the provider CLI's model flag accepts (`local.Recipe.model_flag`). They are not the same field.

`classify(plan_model)` → `light` / `medium` / `heavy`. Omit, blank, or unknown token → `medium`. Known aliases follow Flaitron `SPEC/model.md` inherent tiers: `haiku` → light; `sonnet` / `grok` / `codex` → medium; `opus` → heavy. The frontier band (`frontier` / `fable` / `mythos`) has no alias: the §"Flaitron reader" frontier dimension denies it at launch (CBN-331), so no dispatch classifies it, and an attended read buckets it like any unknown token. **`xheavy` also → heavy.** A fourth class is not wanted. This alias remains, for the attended reads that still need a tier from the token. Concrete tokens are class hints, not a provider pin (`[opus]` is heavy on every v1 worker). Why: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Task-class → model map — xheavy folds to heavy, and the fourth-class deferral is withdrawn (CBN-112, CBN-114)".

**A provider-name token is a tier hint and never a provider pin.** Two aliases —
`grok` and `codex` — are the names of *providers*, not models, and they are
bucketed like any other token: both to `medium`. Two consequences follow, and
neither is a bug:

- `[grok]` does **not** route the task to `grok_cli`. Provider choice belongs to
  §Picker (leftover, registry pins, allow-list) and never reads `plan_model` as a
  preference. A `[grok]` task on a Claude-only allow-list runs on `claude_code`
  at medium.
- `[grok]` cannot express *"a heavy run on Grok."* The token carries one axis —
  how hard the task is — so the only way to ask for heavy is `[heavy]`, which
  then reaches Grok's `xhigh` rung through the effort map below.

To pin a provider, register one — `register --provider` / `--providers`, not the PLAN token. Why the conflation is deliberate: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Task-class → model map — provider-name tokens are tier hints, never a pin".

`resolve(plan_model, provider)` returns the pinned runtime id. Unknown provider → `ValueError`. Never `WorkerError`. `resolve_effort(plan_model, provider)` returns that provider's reasoning-effort rung for the same token, or `None` for a known provider with no effort row. Unknown provider → `ValueError`, as for `resolve`. Bump table cells when CLIs rename; that is not a `schema_version` bump.

Pins reflect live CLI inventories (2026-08-20, refreshed for `grok_cli` / `cursor_cli` (CBN-254; Cursor medium re-pinned CBN-333, 2026-10-11) and `codex` (CBN-165)); a live inventory is a probe, not a catalog read, because the codex catalog is **partial**. The codex row stays flat `gpt-5.6-terra`; `gpt-5.6-luna` and the account-gated ids are deliberately unpinned. Account-gating, unpinned ids, and probe history: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Task-class → model map — pin measurement and probe history".

**Stale-pin guard (CBN-166).** `caobunga.pins.check_all()` is a second, read-only pass over this same table — it never refuses a dispatch and never re-pins. For each *installed* v1 provider, `caobunga status <project>` reports one `pins:` block (the fleet table gains no column), and since CBN-256 the `caobunga digest` CLI verb ends with the same lines under `## Model pins`: a provider with no non-interactive catalog command reports `pins: <provider> catalog unavailable` (today: claude, opencode); a provider whose catalog is declared partial (`pins.CATALOG_PARTIAL` — codex) reports `pins: <provider> catalog partial (TAP-gated ids hidden)` and never compares its pins against the catalog; any other installed provider with a readable catalog warns `pins: <provider> <id> not in catalog` per pinned id the catalog does not list. A catalog command that fails to run or parse, or that parses to no ids at all, degrades to `pins: <provider> catalog unreadable`. A clean provider prints nothing beyond the summary line. An uninstalled provider is not reported at all. Degrade-posture rationale and the CBN-256 grok/cursor wiring: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Task-class → model map — stale-pin guard degrade posture (CBN-166)".


| class | `grok_cli` | `claude_code` | `codex` | `cursor_cli` | `opencode_cli` |
|---|---|---|---|---|---|
| light | `grok-4.7` | `sonnet` | `gpt-5.6-terra` | `composer-2.5` | `anthropic/claude-sonnet-4-6` |
| medium | `grok-4.7` | `opus` | `gpt-5.6-terra` | `grok-4.7-high` | `anthropic/claude-sonnet-4-6` |
| heavy | `grok-4.7` | `opus` | `gpt-5.6-terra` | `grok-4.7-xhigh` | `anthropic/claude-opus-4-6` |

Grok has no lighter SKU (`grok-4.5` is older, not lighter), so the flat Grok/Codex rows pick no distinct model id per class. The task class is **not** discarded for them — the effort map below routes it (CBN-EPIC-15.3). Claude's advertised aliases are `sonnet` / `opus` / `fable` — coding floor is `sonnet` even for `[light]`. `[medium]` takes `opus` on `claude_code` (CBN-255); only the effort rung separates it from `[heavy]`. When the PLAN `[model]` token is itself a Claude runtime alias (`sonnet`, `opus`), `resolve` passes it through on `claude_code`. `fable` is advertised but never dispatched (CBN-331 caps caobunga at `[heavy]`), so it has no passthrough and no `cursor_cli` override. Cursor light uses Composer; medium/heavy stay on the Grok 4.7 family with effort in the id. The dispatch measurement behind the medium-cell choice: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Task-class → model map — the medium-cell measurement (CBN-255)".

Effort mapping (CBN-EPIC-15.3, per-provider since CBN-332). One rung per class per flag-taking provider:

| task class | `grok_cli` | `claude_code` | `codex` |
|---|---|---|---|
| light | `high` | `high` | `medium` |
| medium | `high` | `medium` | `high` |
| heavy | `xhigh` | `high` | `xhigh` |

Each column follows that provider's column of flaitron `docs/PLATFORMS.md` §"Tier × platform map" (2026-10-08). `claude_code` is `sonnet@high`, `opus@medium`, `opus@high` (CBN-331): light runs a higher rung than medium on purpose, because it is the smaller model. `grok_cli` and `codex` have flat model ids. Grok's light cell takes the upper end of the map's `medium`–`high` range (CBN-360), so `[light]` and `[medium]` share `high` and only `[heavy]` (`xhigh`) differs. `codex` stays `medium` / `high` / `xhigh`: Terra is pinned flat where the map steps from Sol to Astra, and effort is still what separates its classes (operator decision 2026-10-11, kept by CBN-360). `cursor_cli` and `opencode_cli` have no row and resolve to `None`. Cursor encodes effort in the model id, so the pin table above routes it, and its heavy `grok-4.7-xhigh` matches `grok_cli`'s heavy rung. Its medium `grok-4.7-high` matches `grok_cli`'s `high` (CBN-333), so a re-pick between the two keeps a `[medium]` run's rung. Every cell sits inside its vendor's live ladder (verified 2026-08-26). The value reaches the CLI through `local.Recipe.effort_argv` (§Worker interface → `local`). Why per-provider, and the operator choices: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Task-class → model map — effort per provider (CBN-332)" and §"Task-class → model map — roster drop and grok light upper end (CBN-360)".

## Picker

Library, not a CLI verb. CBN-023's gated loop calls `caobunga.picker.pick(...)` with the candidate `plan_model` and registry pins. Calls `usage.probe_all()` unless `usage=` is injected, and `taskmap.resolve` for the runtime id.

`pick(plan_model, *, provider=None, model=None, providers=None, usage=None)` → `{provider, model, reason, why}`. `plan_model` is the PLAN.md `[model]` token. Runtime `model` is the provider CLI's model string. Registry pins are the optional `provider` / `model` kwargs, and the registry allow-list is the optional `providers` kwarg (all from the project record). Unknown provider, model-without-provider, pinned provider not installed, or no eligible worker → `ValueError`; the last two raise its `ProviderUnavailable` subclass, which the loop maps to exit 5. Never `WorkerError`.

`reason` is closed: `project-override` / `leftover` / `installed`. `why` is one operator-facing line. Do not persist either field on a run record or other report surface — those surfaces already carry chosen `provider` / `model` / `plan_model`. This clause governs the *provider choice* only; `eligibility_reason` (§"Run record" → Eligibility reason) is persisted by design.

Policy when pins are omitted:

1. When `providers` is declared, drop every candidate outside it.
2. Skip `installed=false` and `leftover=exhausted`.
3. Prefer `leftover=available`, highest `remaining` (`None` last).
4. Else first `leftover=unknown` installed worker, in **declared order** when `providers` is set, otherwise `V1_PROVIDERS` order (`grok_cli`, `claude_code`, `codex`, `cursor_cli`, `opencode_cli`).
5. None eligible → `ValueError` (naming the allow-list when one is declared).

The allow-list (CBN-091) filters eligibility and supplies the tiebreak; it does not outrank leftover. A declared member that is exhausted, uninstalled, or absent from the probe rows is skipped in favor of a later member. Pinned `provider` wins even when leftover is exhausted, and ignores the allow-list entirely. The two are mutually exclusive **on the stored record**, not merely per invocation: `register --providers` clears any stored `provider` and `model` pin, and `register --provider` clears any stored `providers` allow-list (CBN-EPIC-95.4). Pins move as a unit, so `--provider` without `--model` drops a stale model pin rather than carrying it onto a different provider. An update naming neither leaves both alone. Pinned `model` is used as-is (runtime id, not a PLAN token). Incident behind the move-as-a-unit rule: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Picker — pin/allow-list merge incident (CBN-EPIC-95.2, CBN-EPIC-95.3, CBN-EPIC-95.4)".

Today leftover dumps are all `unknown`, so ranking collapses to the order tiebreak: without an allow-list that is the first installed v1 worker, and with one it is the operator's first declared worker. When a leftover parser is eventually wired to a stable command, budget spreading takes over inside the allow-list with no further change.

CBN-213 overlays `leftover=exhausted` onto a provider **after** a clean-tree, HEAD-unchanged usage/session-limit refusal in the same invocation, then re-calls this policy. A hard pin still wins even when the overlay would have skipped that provider, which is why the loop does not re-enter `pick` on a pin. Why the overlay is the loop's, not the dump's: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Picker — usage-limit re-pick overlay (CBN-213)".

## Out of scope (do not invent here)

| Concern | Owner |
|---|---|
| On-disk tree (`CAOBUNGA_HOME`), flock, `--replace` | CBN-010 (landed) |
| PLAN.md → next-task candidate | CBN-011 (landed) |
| Worker launch / send / shutdown seam | CBN-012 (landed; `cao` adapter retired CBN-204, `local` since CBN-EPIC-9.2) |
| Gated loop wiring | CBN-013 (landed) |
| Pretty CLI report | CBN-014 (landed) |
| Usage adapters | CBN-020 (landed) |
| Task-class → model map | CBN-021 (landed) |
| Picker + why-log | CBN-022 (landed) |
| Picker into gated loop | CBN-023 (landed) |
| Hermes review session (assemble + launch the brief) | CBN-030 (landed; retired CBN-258) |
| Epic rollup writer | CBN-031 (landed) |
| Daily fleet digest | CBN-032 (landed) |
| GitHub PR comment (not picked; snippet landed) | CBN-033 |
| Status API | CBN-040 (landed) |
| Status UI | CBN-041 |
| Later tiers actually running | CBN-050 (landed) |
| Hermes observation window (N or T) | CBN-051 (landed; retired CBN-258) |
| Additional worker providers + Fable routing | CBN-052 (landed) |
| Registry provider allow-list (`--providers`) | CBN-091 (landed) |
| Fleet batch verb (`fleet`) | CBN-312 (landed; reopened CBN-124's decline — §"Entry points") |

## Compatibility

Bump `schema_version` when a required field is added, renamed, or removed. Optional fields may be added at version 1. Readers ignore unknown keys.

**v1 → v2 (CBN-EPIC-202.2).** `suite` (§"Suite") is required exactly when `outcome` is `closed` / `de-scoped` / `no-tasknote` — the same conditionally-required class as `commit_sha`, and the first field a dispatch decision reads — so it is the first bump. v1 records load unchanged: a v1 record's missing `suite` reads as *unknown*, never `green`. Why: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Compatibility — v1 → v2 bump rationale (CBN-EPIC-202.2, CBN-EPIC-202.3)".

**Hermes retire (CBN-258).** The four `hermes_*` fields stay optional on the model and are no longer written — a write-side change only, so no bump. Old records load and render unchanged; `briefs/` and `reviews/` under an old home are inert history. Why no bump: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Compatibility — Hermes retire, no bump (CBN-258)".

A `caobunga-status.md` left in a target repo from before CBN-188.2 moved the last-run card caller-side is not Caobunga's to clean (§"Fleet boundary" — the card is caller-side, not a sanctioned producer write): it is ordinary blocking dirt, the operator's own `git rm`. History: [decisions/picker-model-map.md](decisions/picker-model-map.md) §"Compatibility — stray `caobunga-status.md` history (CBN-188.2, CBN-188.3, CBN-154)".
