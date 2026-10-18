# Decisions — registry, execution tiers, run record

Measurement, history, and rejected options behind `docs/CONTRACT.md` §"Non-negotiables" through §"Run record" (CBN-271.2). The rules themselves live in `CONTRACT.md`; this file records why they are shaped the way they are. Reference-only: nothing loads or dispatches on it.

Headings name the `CONTRACT.md` section each entry backs.

## Execution tiers — marker on every launching tier

**CBN-118 overturned CBN-EPIC-17.2's founding assumption** that `observe` / `gated` need not consult the `[unattended]` marker because "the operator is present by definition." That presence is between runs, at the orchestrator terminal — never inside the worker session and never inside the target repo, which is the same distinction CBN-EPIC-17.3 had already drawn for the operator-presence gate and CBN-116 for the `Blocked by` clause. The marker was the last dispatch dimension still keyed to a tier set that did not describe what the tier does.

**What CBN-EPIC-18.4 measured, and what it now costs.** Across the first five-repo fleet the queue head was an operator-gated row in four of five repos: one repo's was `DEPLOY-13.5`, `[!critical]` and blocked on physical access to another machine. Before CBN-118 a `gated` first contact handed it to a headless worker; `proceed-on-green` refused it by name. Now both refuse it. A freshly registered repo with no markers dispatches nothing at any tier and exits 3 naming every denial (`8 task(s) denied under headless dispatch: DEPLOY-13.5 (unmarked), …`). That refusal is the intended first-contact experience — bias-to-refuse, the same posture the safety and `Blocked by` dimensions already take: an unmarked task costs one operator confirmation, an unsafe dispatch commits unreviewed work.

**The default stays `gated` — two options rejected.** Stopping after one task is the property `register` should default to, and the fix for the *pick* is seeding, not a different default. Rejected:

- *Leave the pick unguarded and document it.* Accurate documentation of an unsafe default is still an unsafe default, and it asks the operator to know at first contact the one thing first contact has not taught them.
- *Add a fifth read-and-report tier.* `observe` plus the coverage column already is that surface, and it would leave `gated` — the actual default — unchanged.

## Execution tiers — `plow` retirement (CBN-134)

From CBN-050 to CBN-134 a fourth value, `plow`, ran the same per-task runner as `proceed-on-green` restricted to one epic cohort: the first candidate had to carry an epic stem (a standalone row was exit 3, CBN-EPIC-9.5 F5), the walk exited when the next candidate left that stem, and it continued through `de-scoped` / `no-tasknote` where `proceed-on-green` stops.

The stem bound was the blast-radius limit on an unattended drain when it was the *only* one. CBN-118 made the per-row `[unattended]` marker a requirement of every launching tier, and that marker is the finer bound — per row, chosen by the operator, not derived from the shape of an id — so the stem was a second, coarser limit layered on top of the explicit one, and the one that produced false refusals: a repo holding exactly the rows the operator marked was refused because their ids had no dot (CBN-123 F2; one fleet repo sat in that state with the fleet's one cleanly dispatchable row).

`plow` was never a step *above* `proceed-on-green` on the tier ladder; it was `proceed-on-green` with a narrower scope and a looser stop rule, and `VISION.md`'s `observe → gated → proceed-on-green → plow` read as monotonic autonomy when it was not. Retiring it deleted the only tier-specific branches the run loop had, and the `drain:` exit vocabulary lost `stem-change`. The one behavior lost — continuing through a `de-scoped` / `no-tasknote` close inside a cohort — is a `proceed-on-green` stop-rule question, filed separately if it is ever wanted. At retirement the live registry held five records and none at `plow`.

## Registry — worker-mode-neutral provider validation (CBN-097)

A registry record outlives any single `CAOBUNGA_WORKER` value, and the variable is read at run time, not at register time — so `register` never narrows the allow-list to what one adapter happens to support. The accepted cost: a declared token with no `RECIPES` row registers cleanly and then fails at launch with a `UserError` (exit 2). Fast, clean, and no data risk.

The rule was written when a second adapter (`cao`, retired CBN-204) could honor tokens `local` could not; it stands on its own now — the registry records what the operator allows, and the adapter reports what it can run, at the moment it is asked to.

## Follow-up capture — parked notes under a drain (CBN-138.4)

CBN-121 read follow-ups only from the archived note and recorded the exclusion of a *parked* note as deliberate: its follow-ups are still in the operator's working tree, which is not the surface that swallows them. That is a claim about somebody being there, and it holds exactly as far as the posture does. At `gated` the operator reads the recap and walks into the repo, so the note is in front of them; under a drain nobody is watching, the park is just a run that ended without an archive, and the note sits in a tree nobody opens for days — the same swallow CBN-120.2 named, one door over. So the dispatch posture decides it.

An unreadable parked note degrades to `None` rather than raising: a park is already the unhappy path, and losing the whole escalation record over a narrative field is the worse trade.

## Follow-up capture — heading family, not one spelling

CBN-121 was filed naming `### Follow-ups proposed` as an existing convention, and it is not one. Flaitron's pinned `SPEC.md` has no such heading, and this repo's own archive (at CBN-121) carried **seven spellings across ten of 132 notes**. Matching the prefix family reads what producers actually write; requiring one spelling would be caobunga defining a producer's format (`CONTRACT.md` §"Fleet boundary").

Items inside fenced blocks count because that is the load-bearing case: CBN-120.2's three ready-to-paste `CORE-XXX` lines — the follow-ups whose loss filed this work — live inside a ` ```markdown ` fence, so a fence-skipping reader would find nothing at all on the note that filed it.

**Table rows — measured cost of the exclusion.** When the field shipped, table rows accounted for **1 of 5 matched sections** in this archive (`CBN-120.N`). Recorded so the exclusion can be revisited on evidence.

Table rows are a non-target because a markdown table row has no canonical projection to a single follow-up line — choosing a cell, or a join, is deciding what a producer's table *means*.

## Follow-up capture — asking is not requiring (CBN-138.2)

CBN-121 read *asking* a worker for a follow-up heading and *requiring* one as the same act and left `worker_message` alone; CBN-138.2 corrected that. Three things separate the acts:

- The cue names `/ft-file-task --unattended` (`/ft-file-followup` before the v7 rename; the only cue since CBN-330), a *flaitron skill invocation*, so it invents no heading, spelling, or grammar at all.
- `worker_message` **already** asks a producer for an on-disk shape one sentence earlier — the posture sentence instructs the worker to write `status: blocked` plus `park-reason: <code> — <prose>`, which `CONTRACT.md` §"Park reason" then parses — so asking cannot itself be the boundary violation without indicting the sentence beside it.
- A request **narrows no read**: defining a format constrains what counts as valid input, and the input grammar is untouched.

What survives of CBN-121's reasoning is the limit: flaitron defines no follow-up heading at any released version, so `### Follow-ups proposed` remains a caobunga-originated spelling — one caobunga may *ask its own dispatched worker* for, and may not *require* of any producer.

## Marked-set baseline — not a cached classification

CBN-EPIC-17.1 forbids "a cached classification artifact, no second file that says what a task is", and `CONTRACT.md` §"Marker coverage" restates it as *derived per read, never persisted*. That rule governs whatever answers the **current** question, and current coverage is still derived per read on every surface. `marked_tasks` records what **one past run observed**, which is the same category as every other run-record field and is read as history by exactly one caller. `eligibility_reason` is the precedent — persisted at CBN-EPIC-17.2 over the same constitution, on the same distinction, deliberately drawn there rather than assumed.

## New-row detection — scope and marker choice (CBN-138.3)

**Why every parsed row, not just the launch queue.** `marker_drift` scopes to the open launch queue because scoping *removes* three false positives there; scoping `new_rows` would *hide* a true one — a row filed into `## Future Opportunities` is still a discovery. Reporting it is the same posture `CONTRACT.md` §"Marker coverage" takes on `unparsed` (CBN-115) and drift (CBN-125).

**Why the literal marker, not the `eligibility` predicate.** `coverage` and `marker_drift` reuse `eligibility` so their claims cannot drift from dispatch behaviour — but both make a claim about the **current** queue. `new_rows` records what **one past run produced**, and the live dispatch question about the same rows is answered by `coverage` on the next read; re-deriving it here would duplicate that on a subset, and would report a `handoff` or `blocked` row as "unmarked" when a marker is not its fix — the misdirection CBN-114 and CBN-136 each closed by checking the more specific dimension first.

## Foreign-commit detection — motivating incident (CBN-172)

`close.py` compared `HEAD` against `start_sha` only to find the run's own closure or park commit; a commit landing in the target repo from anywhere else during the same window was invisible until a hand `git log` turned one up mid-diagnosis — Fleet HQ's NAT-247.4 sweep, committed inside a CBN-167 worker window with nobody watching for it.

Before CBN-188.2 the last-run card was itself a target-repo commit, excluded by its fixed commit message; the card is caller-side now, so nothing lands in `start_sha..HEAD` for it to exclude.

### Filing commits leave `foreign_commits` (CBN-299)

CBN-172 excluded by SHA alone, so a worker's own `/ft-file-followup` commit landed in `foreign_commits` and echoed `foreign:` even when `new_rows` already named what it filed (a fleet repo's HO-091, run `20261007T152043742Z`: `d61cb4f` filed HO-095). The match is deliberately narrow: the commit must change `.flaitron/PLAN.md` and no other path, remove nothing but a blank line or a section's `(none)` placeholder (the one removal flaitron's own filing makes, `SPEC/plan-filing.md` §"Empty-section placeholder"), add only task rows, and every added id must be in `new_rows`. A filing that also writes a starter or sidequest file stays foreign, since `new_rows` accounts for rows and not for those files. Dropped from the record rather than kept with a `kind` field: `new_rows` is already the record's account of the rows, and a `kind` would widen `ForeignCommit`, the payload-keys fixture, and `types.ts` for a label.

## Escalation reasons — tree snapshot fold (CBN-171, CBN-184)

On their own, `worker-exited` and `timeout` said only what the process did, never what it *left*: an exit 0 with six modified files and a scaffold-only tasknote used to read identically to a true no-op, so the operator had to open the repo to tell them apart (CBN-171). CBN-184 extended the fold to `closed` / `de-scoped` because a worker's post-commit filing can leave the tree dirty *after* the closing commit lands, and that residue used to surface only as the *next* launch's dirty-tree refusal — a stale complaint about the previous run.

## Hermes review — retire verdict (CBN-242, CBN-258)

Flaitron's in-run External review (`CORE-656`, pinned at `24e15d3`) does the Hermes review's job, and does it earlier. Over the first 13 headless closes after the bump (10 `claude_code`, 3 `grok_cli`; `codex` never dispatched), all 13 archived tasknotes recorded the review with dispositions. It caught 2 real blockers (CBN-EPIC-243.2 and CBN-251; each sent its run back to Phase 2) and 3 notes that were fixed. Hermes returned `accept` 13/13 and caught nothing the in-run review had missed — the pattern CBN-205 saw across 23 suite-bearing closes. Max duration was 3532s against the 7200s `CAOBUNGA_RUN_TIMEOUT` (no raise needed). There were no `input-needed` parks.

Hermes then retired whole: once the review goes, the brief has no other reader, and the epic rollup and the daily digest fold run records on their own.

### Retired paragraph, as it stood in `CONTRACT.md` (CBN-298)

**Retired (CBN-258, 2026-09-24).** The Hermes post-commit review, its brief, cadence window, and verdict delivery are gone; the loop writes none of the four `hermes_*` fields. They stay on the model as optional legacy keys so records written before the retire still load, and the digest (§"Daily fleet digest") and status UI still render them where present. No schema bump: removing a *write* of optional fields changes nothing a v2 reader asserts (§"Compatibility").

## Suite — missing recipe reclassified as `refused` (CBN-211)

a fleet repo's `CORE-12` (2026-09-20) stopped the only `proceed-on-green` drain as `red` because its justfile had `check:`, not `typecheck:` — `just` exits 1 with `justfile does not contain recipe`. The repo could not answer the verb, which is a fleet-layer gap, not a code failure, so that one exit reclassifies as `refused`. The same incident motivated the no-alias rule in `CONTRACT.md` §"Suite fitness preflight" (CBN-215).

## External review — why the field exists (CBN-257)

CBN-242 retired the Hermes post-commit review in favor of Flaitron's Phase 3 `**External review**` box: over 13 post-bump headless closes the in-run review caught two blockers and Hermes caught nothing (§"Hermes review — retire verdict" above). That left one thing Hermes did that the box alone does not: saying, on a caobunga surface, whether the review happened at all. `codex` has never been dispatched, so nobody has seen whether it runs one — the field makes a skipped review a value rather than silence.

## Run record — skip-not-fail, and why the registry is strict (CBN-106)

Run-record reads skip rather than raise because every reader folds a whole directory and `/api/stats` folds every project, so raising made one bad file a fleet-wide outage. Registry reads are deliberately not lenient in the same way because skipping a corrupt registry file would silently unregister a project rather than degrade one row.

## Follow-up capture — why the field exists (CBN-120.2)

A worker that discovers work mid-run writes it into its own tasknote and then archives that note at closure. Under `gated` an operator reads the recap and acts; under `proceed-on-green` nobody is watching, so the discovery is sealed in a file no surface reads — the failure CBN-120.2 named and `follow_ups` closes.

A prose-only section correctly reports nothing: two archived audits open their follow-up section with `**None.**` — they are saying there is nothing to file, and the item rule reports zero. Capturing the section *body* instead would surface both as follow-ups.

## New-row detection — why `unmarked` equals the count (CBN-138.3, CBN-246)

Caobunga never seeds and Flaitron never writes the marker, so a worker-filed row is unmarked by construction — unless the worker writes the marker itself, which the drain pin (CBN-246) refuses to dispatch in the same invocation. The figure earns its place by *stating*, per run, that every discovery a drain made needs an operator act before anything can dispatch it — and it stays correct when the operator seeds mid-drain.

The one snapshot that could not carry `new_rows` was `loop`'s pre-launch refusal of an already-`blocked` note, retired by CBN-252: nothing ran, so there was nothing to measure and the field stayed absent.

## Eligibility reason — not the picker why-log

`eligibility_reason` is not the picker why-log. `CONTRACT.md` §"Picker"'s "do not persist `why` / `reason`" clause governs the *provider choice*, which stays off every report surface unchanged; this field records the *task-eligibility decision*, which the PLAN line for CBN-EPIC-17.2 explicitly routes onto the record.

## Suite — why a structured field (CBN-EPIC-202.2)

Until `suite` the only suite result on any surface was Hermes reviewer prose (`hermes_verdict`, since retired), which nothing could count or gate on. It is the first record field a dispatch decision reads, which is why it is structured rather than folded into `summary` the way `tree:` is (CBN-184): it has had four consumers — the drain gate, the report surfaces, the retired Hermes brief, and the CBN-205 disagreement tally. Rollout: defined at CBN-EPIC-202.2, filled by `suite.run_suite` at `.3`, gated on at `.4`, rendered at `.5` (`status`, the digest, the Status UI).

## Suite fitness preflight — why (CBN-205, CBN-211, CBN-215)

The preflight runs `just --summary` and never the fleet verbs themselves, whose ceilings (600/120/300s) are wrong for a synchronous CLI call. It matches recipe names exactly, with no `check:` → `typecheck` alias — the `CORE-12` shape CBN-211 fixed after the fact.

It is a structural snapshot at register/observe time, not a guarantee: a repo can still turn `fit` into a `red` close, and a `refused` preflight can turn `fit` the moment a justfile is added. It exists so an operator sees "this repo's suite can never be `green`" before a `proceed-on-green` drain finds out the hard way (CBN-205's ledger kept citing exactly this class of stop).

## External review — classification choices (CBN-257)

`na` keys on the disposition's *leading* token because the template's own box text mentions "`N/A` with a one-line reason" mid-sentence, so a substring match would read every untouched box as `na`. A `de-scoped` close whose box reads `missing` records `na` because a de-scope has no diff to grade, so flagging it would be noise on every de-scope.

A ticked box whose own text does not lead with `N/A` defers to the first `External review:` line below the box in its `##` section (CBN-337), because the template box ends "… or `N/A` with reason" and ten cbn notes put that `N/A` in Testing Notes, and their run records say `ticked`. Those records are not rewritten. The read stops at the section's end, so a recap's mention cannot reach back. The colon keeps the box's own `**External review** —` line from matching itself. The label must open a line or follow a sentence's `. `, so a phrase quoted in inline code does not count. The accepted tradeoff: the first such line wins, so prose that opens a sentence with `External review: N/A …` after a review that ran still reads `na`. That shape appears in no archived note. A ticked box with its own non-`N/A` disposition also defers. The alternative is telling template text from a filled disposition, which would mean matching the producer's template prose.

Two further drift shapes read tolerantly (CBN-341). Both came from closed fleet notes whose run records say `missing` though the note states `N/A`: one repo's FE-106.2 ticked `- [x] External review — N/A: …` with the bold dropped, and another's BE-284.2 dropped the box and wrote only `- External review: N/A — …` under `## Phase 3: Testing Notes`. An unbolded label counts as the box only when it opens the box text inside a Phase 3 section. A fleet repo's BE-077 lists an unticked `- [ ] External review against Acceptance` subtask above its real box. If that line counted, a review that ran would read `missing`, and on a micro note it would override the `**External review:**` label. The unbolded form is matched case-sensitively and only at the start of the box text. The bold form's `N/A (De-scope) —` prefix is not read for it, since no fleet note shows that shape. A boxless line reads `na` only when it leads with `N/A` and sits in a Phase 3 section. A boxless non-`N/A` line stays `missing`, keeping CBN-330's "the box decides" rule: a stated `N/A` is a disposition, while "clean" prose on a full note is not evidence the box's step ran. A fleet-wide archive probe flipped 18 notes `missing` → `na`, all stating `N/A`, and no other read. Run records already written are not rewritten.

## External review — micro notes and dispatch (CBN-338)

The CBN-335 live trial found that no dispatch path starts a micro note, so CBN-330's micro `**External review:**` read is reached only when an operator chose micro (F0). The other option, routing `[light]` rows to `--micro`, was rejected for two reasons. First, flaitron's pinned `step-0-flags.md` makes `--micro` "Explicit-opt-in only — the operator picks the ceremony tier at invocation", and `SPEC/tasknote-selection.md` defaults an unsure pick to plain `/ft-task`. Second, `[light]` names the model a row needs, not how much ceremony it gets, so deriving one from the other would make caobunga define a producer's semantics (CONTRACT §"Fleet boundary"). The read is not dead code: the continue prompt drives an operator-started `in-progress` micro note, so dispatch can close a micro note it did not start. It cannot resume a parked one. Flaitron's `micro-mode.md` §"Step 2" would resume it in micro mode, but caobunga denies a `status: blocked` note at pick (`parked`, CBN-252). One known limit: the continue prompt names `unattended-mode.md` but not `micro-mode.md`, so a continued micro note relies on the worker recognizing the note's `## ⚡ Notes` shape. A worker that fills no `**External review:**` line reads as `missing`, a visible value rather than silence, which is the field's purpose. Tracked as CBN-339, to start only once that shows up. If flaitron ever adds a PLAN-row ceremony marker, the prompt can pass it through. That would be a new row, not a reopening of this one.

## Park reason — why a string, and the one park site

Importing Flaitron's park-code set would make a Flaitron release a Caobunga contract change — the same reciprocal-record stance `CONTRACT.md` §"Fleet boundary" takes toward Fleet HQ. Since CBN-252 retired the refusal to launch a note that was already `blocked`, the mid-run classification is the one park site; records from before carry `park_reason` from both.

## Registry — case-insensitive path match (CBN-131)

`Path.resolve()` does not normalize case, and macOS preserves case without distinguishing it, so `~/Code/foo` and a record written as `~/code/foo` are the same repo. `register` therefore merges them rather than letting them collide, and the in-repo skill matches them as one.

## Invocation — why the grouping key (CBN-EPIC-12.6)

`gated` writes one record and `proceed-on-green` one per drained task, and nothing else in the record separated those, so reading a flat history meant doing arithmetic on `closed_at` gaps to guess where one drain ended (CBN-EPIC-12.6 F3). The **cohort** half of that finding needs no field — `flaitron.epic_stem` derives an epic's cohort key from `task_id`.
