# Decisions — entry points, CLI, fleet boundary, report surfaces

Measurement, history, and rejected options behind `docs/CONTRACT.md` §"Entry points" through §"Last-run card" (CBN-271.3). The rules themselves live in `CONTRACT.md`; this file records why they are shaped the way they are. Reference-only: nothing loads or dispatches on it.

Headings name the `CONTRACT.md` section each entry backs.

## Entry points — no batch entry point (CBN-124)

> ⚠️ **Superseded by CBN-312** (2026-10-08) — the reopen condition below was met; see §"Entry points — batch entry point reopened (CBN-312)". Kept as the record of why the verb waited.

That rule stood as an assertion from v1 until CBN-124 examined it against a fleet that by then existed. It holds, on four grounds, and they are recorded here because a refusal whose reasons are written down is the one that can be overturned honestly later:

- **The concurrent form already exists and is proven.** `caobunga run a & caobunga run b &` is the fleet form, and CBN-123 confirmed live that it is safe: the run mutex is genuinely per-slug (`CONTRACT.md` §"Run loop"), so a second slug proceeds while the first holds its lock. A verb would wrap a shell construct the operator already has.
- **The sequential form is a scheduler.** Walking slugs in order needs an ordering policy, a fleet-level failure policy (does repo 3 failing stop repo 4?), and a resumption story — and each leg blocks up to `CAOBUNGA_RUN_TIMEOUT` (2h default). Those three decisions are the thing `VISION.md` §"What it is not" refuses when it says Caobunga is "not an unbounded autonomous coder in v1", and the thing the closed verb set is protecting.
- **Measured fleet dispatchability has never exceeded one repo.** CBN-123 read the fleet in full and found "exactly one cleanly dispatchable task", concluding that "two full drains side by side are not available today without knowingly dispatching blocked work" — a queue-readiness limit, not a hygiene one, on clean trees. CBN-EPIC-18.4 measured the same shape earlier: the queue head was an operator-gated row in four of five repos. A fleet verb would parallelise a set of size one.
- **CBN-134's precedent.** `plow` was retired because a coarse *derived* scope (the epic stem) layered on top of a fine *operator-chosen* one (the per-row `[unattended]` marker) was redundant and produced false refusals (`CONTRACT.md` §"Execution tiers"). A fleet verb is a coarser scope over N repos sitting on that same marker — the identical shape one level out.

**What would reopen it:** a fleet that regularly shows two or more repos with a dispatchable candidate at the same time, which `caobunga status` with no argument already reports per project. The read half of "fleet-wide" is built; it is the dispatch half that is declined.

## Entry points — batch entry point reopened (CBN-312)

CBN-124's reopen condition was "a fleet that regularly shows two or more repos with a dispatchable candidate at the same time". On 2026-10-08 `caobunga status` showed about two dozen registered repos, several of them clean with `[unattended]`-marked rows at once (four when CBN-312 was filed; three when it was built). The shell form `caobunga run a & caobunga run b &` over that fleet means unbounded fan-out and unprefixed interleaved output. `caobunga fleet [--jobs N]` is that form with a bound, a `[slug]` line prefix, and one receipt.

Each CBN-124 ground, answered:

- **The concurrent form already exists.** Still true, and `fleet` is that form — each leg is a child `caobunga run <slug>`, so the per-slug mutex CBN-123 proved is the only coordination. What the verb adds is the bound and the readable output.
- **The sequential form is a scheduler.** The three decisions it named are each answered by refusal, not by a policy. Ordering: registry (id) order, no priority. Fleet failure policy: none — one repo's stop never halts another. Resumption: none — a re-run spawns again, and `run`'s exit 3 skips a repo with nothing eligible. Serial within a repo is `run`'s own drain, unchanged.
- **Dispatchability never exceeded one.** No longer true (above). CBN-308 / CBN-310 also removed the load hazard parallel drains had: every slug's suite verbs serialize on `suite.lock`, so two test pools no longer load each other into a false red.
- **CBN-134's coarse-over-fine precedent.** `fleet` derives no scope. It never reads a PLAN and launches no worker itself; every per-row decision stays `run`'s and the `[unattended]` marker's.

Shape choices, operator-picked at CBN-312 Discovery: **child processes** rather than in-process threads, so `run`'s code path is the same under `fleet` (the child runs `python -P -u -m caobunga run <slug>` — `-u` unbuffers only that interpreter, `-P` keeps the cwd off `sys.path`) and a crash stays isolated; **live prefixed lines** rather than per-repo log files, so no new on-disk layout; **every enabled repo, no prefilter**, because `run`'s own fast exits (2, 3) already sort them and a prefilter would restate its gate; and **worst-of exit** where `2` and `3` are skips — a dirty tree or an idle queue is a repo's state, not a fleet failure. `run`'s arity guard stays: the batch form is a separate verb, never a second argument or an `--all` on `run`.

## Entry points — environment failures are not skips (CBN-EPIC-313.5)

CBN-312's fold skips a child's `2` because a dirty tree, a held lock, or a disabled record is that repo's state. But `2` also carried three refusals that belong to the machine: no eligible worker (nothing installed, or every allowed provider exhausted), a pinned provider that is not installed, and a `git` that hung or is missing (CBN-269). On a box with no provider on `PATH`, every leg refused, the receipt read `refused` down the column, and `fleet` exited 0. That is the same silence as a green dashboard over a repo that will refuse.

Those three now exit `5` (`errors.Unlaunchable`), and `fleet` names a `5` `unlaunchable` and folds it to `4`. Operator-picked at Discovery over two alternatives:

- **Exit 5 only under `fleet`**, via an environment flag on the child. Rejected: `run` would have two exit dialects, and a `run` launched by hand and the same `run` launched by `fleet` would disagree about one failure.
- **`fleet` reads the child's invocation receipt** (`stop_reason` `provider` / `git-unreadable`) and leaves every exit code alone. Rejected: `fleet` reads nothing but exit codes today, and parsing the `invocation-record:` echo couples it to a line format and a file it never otherwise opens. ⚠️ Reversed for a child's exit `0` by CBN-372 — §"Entry points — an exit-0 leg answers to its receipt (CBN-372)".

The cost is the CBN-272 promise that direct-run exits stay 0/2/3/4. A script that tested `== 2` for "provider missing" now sees `5`. Accepted because the meaning of every other code is unchanged and `5` was unused.

"No eligible worker" counts whatever leaves the pick empty. That includes an allow-list or pin that names only providers this machine lacks, where the fix may be an install or a registry edit, and a first pick with every allowed provider exhausted. Either way nothing can launch, and the fleet should not read green. Exhaustion is unreachable on a first pick today, since no CLI exposes a usage dump; mid-invocation exhaustion (CBN-213) still ends as the last launch's `4`.

**Scope stays at those three.** A provider with no adapter recipe (CBN-097), a bad `CAOBUNGA_RUN_TIMEOUT` / `CAOBUNGA_WORKER`, a missing path or non-repo, and a missing PLAN stay `2`. Each is a registration or configuration fact the operator fixes with one edit, and the PLAN line named only the three. `pick` keeps its `ValueError` contract, which the loop's re-pick branch relies on; the two machine cases raise its `ProviderUnavailable` subclass, so the loop chooses the exit code by class, not by message text.

## Entry points — an exit-0 leg answers to its receipt (CBN-372)

On 2026-10-17 two `fleet` legs drained, closed tasks, and then parked one for a human (one task in each of two registered repos, both `visual-confirm`), each leaving its code uncommitted in the tree. `run` exits `4` only when the last outcome is `failed`, so both exited `0`, and the fleet receipt read `ok` over two dirty, parked repos. Each child's invocation receipt already said `successful: false`. CBN-351 F3 met the same fact from a `gated` driver loop and filed nothing, because "the receipt is the signal". `fleet` was the one aggregator not reading it.

`fleet` now reads the receipt an exit-`0` child names on its `invocation-record:` line and folds an unsuccessful one to `4`. The receipt's own `<status> (<stop_reason>)` is the row's meaning, so no new vocabulary is added. Operator-picked at Discovery over two exit-code alternatives:

- **A new `run` exit for a parked drain.** Rejected: it breaks the direct-run exit promise a second time, after CBN-EPIC-313.5, and it leaves a closed-but-red suite exiting `0` and reading `ok`.
- **A new `run` exit for any unsuccessful invocation.** Rejected: it reverses CBN-EPIC-202.4's deliberate "the exit code does not change" for a red suite and breaks the same promise.

This reverses CBN-EPIC-313.5's second rejected alternative, but only for exit `0`. Its objection, that `fleet` "reads nothing but exit codes" and would couple to a line format, still holds for every other code, where the code is enough. At `0` the exit code cannot separate a finished scope from a park, while CONTRACT §"Invocation results" already tells aggregators to read `successful`. The coupling is kept small. Only the `invocation-record:` prefix is matched, and the file is loaded by its id through `Store.load_invocation`, so a line can name only a receipt under this home. A receipt that will not load fails the leg, since an unreadable receipt is not evidence of success. A child that names no receipt keeps its exit-code meaning, because `observe` legitimately names none and `fleet` never learns a child's tier. `test_fleet.py` drives a real child, so a reworded `invocation-record:` echo fails there and cannot quietly turn every leg back to `ok`. A park is the operator's to resolve, the same as `failed`, so it takes the escalation exit and not a skip.

## Entry points — `fleet --jobs` default 3 → 4 (2026-10-20)

Across 99 worker-launching invocation receipts the peak overlap was 3, the old default, reached on three days (2026-10-08, -17, -18); the host is 8 cores / 16 GB. A fourth leg adds no suite-vs-suite load (§"Run loop — suites serialized across slugs (CBN-308)" in `worker-run-loop.md`), but it does add provider-limit burn and worker-session test load, the residual that section accepts. Operator-picked 4 over 5 and over leaving 3: nothing measures demand above the old cap, and `--jobs 5` stays a flag away for a trial. Reopen the default if a false red recurs with no suite overlap in the logs, or CBN-213 re-picks climb.

## Self-drive — why `gated`, and why no guard

`execute_run` (`CONTRACT.md` §"Run loop") is a single process. Under `gated` it returns after the first written run record and the process exits. Under `proceed-on-green` it iterates, re-reading the PLAN each pass but running the `caobunga` modules it imported at launch for the whole invocation. For every other project that costs nothing — the worker edits that project's tree, and the code driving it lives elsewhere. Here the two trees are the same tree. A worker that closes task *k* by editing `caobunga/loop.py` leaves the orchestrator to run task *k+1* under pre-edit code against a repo that no longer matches it, and nothing detects the split: Python does not re-import mid-process, and the loop does not check.

`gated` closes that window structurally rather than by vigilance — there is no second iteration, so the next invocation imports the tree as the worker left it. That is why `gated` is the stance here and not merely the `register` default.

**Why no guard.** A path-aware check in `register` would be the only path-aware branch in the registry, which is a bigger change to what a registry record means than the hazard warrants while one operator is the only writer.

## Install provenance — why the `source:` line exists (CBN-169)

A snapshot install (`uv tool install` without `--editable`) silently shadows local fixes, as CBN-154/155/156 did until an operator noticed and reinstalled by hand; the `source:` line makes that state visible without asking. CBN-173 later closed a false positive in the classification, where a `.git`-rooted `$HOME` made an unrelated snapshot install misreport as editable — which is why the walk-up requires the `.git` entry's relative layout to match, not merely exist.

`--version` omits the hook affix and the reinstall hint because it reports provenance; `status`'s fleet-table context is where the fix belongs.

## Fleet boundary — the retired write outward (CBN-033 → CBN-188)

CBN-033 originally committed a last-run snippet into the worker repo — a second write outward beside the task commits — but CBN-188.2 retired that commit for a caller-side file (`CONTRACT.md` §"Last-run card") and CBN-188.3 retired the gate and dirt exemption built around it. Fleet HQ's actuator carve-out (`FLEET-ARCHITECTURE.md` §"One-way fact flow") names the worker's task commits as the actuator's producer write and says the caller-side card is not a second sanctioned write, so nothing is left that calls for the retired write. The actuator tier did not change: the work still lands in the producers it drives through the worker session's commits, while caobunga's own code writes nothing there.

## Fleet boundary — tasknote-template read history (CBN-235)

A third body reader, `goal_body()` / `final_through_archived()` over `## 🎯 Goal` and `**Final Summary:**` … `**Archived:**`, fed the Hermes brief and retired with it at CBN-258. Upstream `CORE-655` landed the phase-heading and Goal row into `docs/EXTERNAL-AGENTS.md` §"Stable surfaces for callers" at v5.33.0 (CBN-241) — the CONTRACT paragraph now records the read against a table that names the row, rather than against a vendored template that predated it. The CBN-241 bump that landed `CORE-655` is where the two halves were read against each other.

## Fleet boundary — Fleet HQ reads the run store (NAT-293)

`CONTRACT.md` §"Fleet boundary" → the Fleet HQ reader note. Fleet HQ's fact-flow rule 4 ("reads are written down on both ends") is why the note lives in `CONTRACT.md` as well as in Fleet HQ; its reader-side half is `FLEET-RUNS.md` §"Reading a shape this file does not own".

The direction is the one `CONTRACT.md` already describes: Fleet HQ reads the actuator's store, and the actuator still reads no Fleet HQ surface. The two folds over the run ledger — Fleet HQ's `scripts/fleet-runs.sh` sweep and this side's `/api/stats` — are two producers of overlapping facts by design, neither downstream of the other.

## Name collisions — why nothing is renamed

Renaming Fleet HQ's surfaces would churn every document, script, and JSON emission that names them; renaming caobunga's is work Fleet HQ's own remit boundary puts outside its reach — which is why that repo recorded the collision instead of resolving it, and why the `CONTRACT.md` table is the reciprocal record rather than a rename. Two tables, authored independently from each side, that agree. This one goes stale only if a sense itself changes.

## Status API — why `current` reads `in_flight` (CBN-EPIC-17.3)

`in_flight` and the chosen `candidate` were one field's two jobs until the picker stopped preferring in-progress notes under an unattended posture; reading the candidate after that split would report **no current task while the operator is working on one** — an unsafe behavior swapped for a lying one. The payload is unchanged, and `in_flight` is computed on every read regardless of posture, which is what makes it the durable current-task signal CBN-040 needs (run records are written at close, so they cannot answer "now").

## Report-surface deny-list — why proposed follow-ups are not a phase-log leak

A `### Follow-up…` section can sit under any phase heading, so the deny-list's second row deserves an explicit boundary rather than a reader's inference. What that row denies is the *execution record* — how the work was done: ticked checkboxes, Discovery / Implementation / Testing notes, Acceptance criteria. A proposed follow-up is forward-looking and operator-facing; it records what was **not** done, and it reaches a surface as a run-record field extracted once at close, never as an excerpt of the note.

## Report-surface deny-list — why `log_path` is not a first-row leak (CBN-162)

The tee file `log_path` points at is exactly the "raw CLI stdout" the deny-list's first row denies, so the field earns the same explicit boundary. What that row denies is the *content* reaching a report surface; `log_path` is a filesystem pointer an operator follows by hand — the same reasoning that lets `tasknote_path` sit on the record without becoming the tasknote excerpt.

## Daily fleet digest — why the window is local (CBN-EPIC-95.4)

The window was UTC through CBN-032 and was changed deliberately by CBN-EPIC-95.4: for a UTC−4 operator a UTC day rolls over at 20:00 local, so the digest was structurally blank through the whole evening — the hours it is actually read (CBN-EPIC-95.3 F1).

## Daily fleet digest — bullet ordering and tag rules

- **Follow-up block (CBN-121).** Rendered in the digest because a drain closes tasks with nobody reading the transcript, so the digest is the first place an operator meets what those runs discovered.
- **`new_rows` after follow-ups (CBN-138.3).** The same argument one step later: a proposal is what the worker said, a new row is what it wrote into the target PLAN — so proposals print first and their result second. Each row states its marker state because a bare id would leave the reader inferring it from the heading count, which fails the moment a run files a mix.
- **`hermes:` line last (CBN-148).** What the worker said and wrote come first, the reviewer's judgment of it last.
- **`[code]` tag on the task-id line (CBN-160).** It is the fleet-scannable count/sort key, so it belongs where a reader scans task-by-task rather than after the prose the `hermes:` line already carries.
- **`[suite: …]` green-silent (CBN-EPIC-202.5).** `suite` is a required field on every closure outcome (`CONTRACT.md` §Compatibility), so an unconditional tag would fire on nearly every bullet. A red/timeout/refused suite is the anomaly worth a scan; a green one needs no comment.
- **`[review: missing]` anomaly-only (CBN-257).** `ticked` and `na` are dispositions a runner recorded; only a full-shape note closed without the in-run review is worth a scan.
- **`(park: …)` affix (CBN-187).** A `blocked` outcome alone does not say why the run stopped, and a code like `visual-confirm` — the one that needs a human's eyes, not just a wait — reads identically to any other park among a day's bullets unless it is visible right on the line.

## Last-run card — why a caller-side file (CBN-033 → CBN-188.2)

CBN-033 originally picked a snippet committed into the target repo over a GitHub PR comment. CBN-188.2 retired that design once flaitron's own boundary — "a caller commits nothing of its own into a producer repo", `docs/EXTERNAL-AGENTS.md` step 9 — ruled it out.

## Hermes brief — what the retirement removed (CBN-258)

Moved from `CONTRACT.md` §"Hermes brief" (CBN-298), verbatim:

**Retired (CBN-258, 2026-09-24; verdict CBN-242, §"Hermes review").** The brief, its launch (`hermes chat -q`), the reviewer profile and its MCP grants (`code_fs` / `code_git` / the `caobunga-mcp-suite` `code_suite` server), verdict delivery (`hermes send`), and the `CAOBUNGA_HERMES_*` cadence and launch variables are gone. A close now writes its run record, then the last-run card and any epic rollup — no brief, no second write. The allow-list, body shape, reviewer authority, and profile hand-off this section held live in git history (`git log -- docs/CONTRACT.md`). An operator who built the Hermes `reviewer` profile can delete it; nothing here reads it.

## Fleet boundary — reads no Fleet HQ surface

That caobunga reads no Fleet HQ surface is a constraint on this side, not a description of current behaviour: joining on a Fleet HQ shape would put the actuator downstream of Fleet HQ and change the tier relationship. The registry is deliberately a *second, opt-in* inventory over the same repositories Fleet HQ discovers — the two are expected to disagree, and that is not drift.

## Fleet boundary — Fleet HQ drives `register` (NAT-326)

A confirmed loop of `register` is the shell loop `CONTRACT.md` §"Entry points" already expects, run by Fleet HQ's session instead of typed by hand. It is not Fleet HQ writing this store — Fleet HQ's path guard keeps `$CAOBUNGA_HOME` read-only to its session. CBN-124 declined a multi-slug `run`, whose scheduler, failure, and resumption policies are what it refuses. It says nothing against a confirmed loop of `register`, which carries none of those policies. CBN-312's `fleet` batches `run`, not `register`, and changes nothing here.

## Status API — one read, and the `tree` cost (CBN-130)

`current`, `coverage`, and `tree` come from one read because parsing the PLAN three times would be the same answer at three times the cost. The extra `git status --porcelain -uall` per project per request that `tree` adds is accepted deliberately: the dashboard is where an operator decides whether a repo is ready, and a fleet view reporting full marker coverage on a repo that will refuse at exit 2 is the same silence in a different place.

The `snippet_ignored` key CBN-154 added was retired at CBN-188.3 with the gate it reported. Unlike marker *drift*, serving `tree` joins no run record: it reads the target repo alone, so nothing about what the payload means changes.

## Status UI — Last run panel and coverage cell rationale (CBN-138.4, CBN-EPIC-202.5)

Each new row on the Last run panel states its own marker state, since a bare id would leave the reader inferring it from a count, which fails the moment a run files a mix. An empty list is omitted rather than labelled because a labelled empty row would say neither "proposed none" nor "not measured".

Rendering `follow_ups` is not the phase-log leak the must-not-appear list rules out, on the argument `CONTRACT.md` §"Report-surface deny-list" → Deny-list already makes for the same field: a proposed follow-up records what was *not* done, and reaches the page as a run-record field extracted once at close, never as an excerpt of the note.

The suite row and chip render on every closure because the panel and history are full detail views; the digest's `[suite: …]` tag is the one surface that stays green-silent.

Marker coverage renders as a count, not a percentage, because the operator seeds a countable number of PLAN lines and a ratio hides how much work that is.

## Entry points — what an entry point is not

"Entry point" is not three neighbouring words it would otherwise be confused with:

- Not a **tier**. Tier is *policy* — how far a run goes before stopping (`CONTRACT.md` §"Execution tiers"). Either entry point honors any tier.
- Not a **worker mode**. That is `CAOBUNGA_WORKER` (`local`, the only value since CBN-204), which adapter spawns the session (`CONTRACT.md` §"Worker interface"), and is what "worker-mode-neutral" means in `CONTRACT.md` §"Registry".
- Not a **Surface**. `CONTRACT.md` §"Surfaces" is the report-visibility table — what each reader may see. An entry point starts a run; a surface reports on one.

## Entry points — install step and the in-repo skill's refusal (CBN-132, CBN-EPIC-12.5)

Bare `caobunga <verb>` remains the notation `CONTRACT.md` writes verbs in; it names the surface, not a guarantee about any particular shell. `--editable` is part of the install step because a copied install goes stale against the checkout, which is §"Self-drive"'s hazard displaced one layer out and with nothing watching for it.

The in-repo skill refuses on a miss rather than offering to register: making registration a side effect of a run request is the slope "explicit registration only" exists to hold. Reading the registry is not registering.

## Status API — why the payload-key guard (CBN-EPIC-231.3)

`types.ts` is hand-maintained against the detail endpoint with no shared schema, and it had drifted in six fields before the payload-key guard landed.

## Fleet boundary — template-read provenance and guard rationale (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

**Caobunga reads flaitron's tasknote template, and this paragraph is the reader's half of that record (CBN-235).** Two readers here parse the body of a tasknote, not just its frontmatter, and both hook headings or labels `docs/EXTERNAL-AGENTS.md` §"Stable surfaces for callers" now lists (v5.33.0). `phase_progress()` in `caobunga/flaitron/tasknote.py` (§"Run loop", CBN-214) relies on the four `## <emoji> Phase N: <Name>` headings of `templates/tasknote-template.md` — an optional single leading token, the literal `Phase N:`, and a name whose first word is the slug it echoes — on `- [x]` / `- [ ]` boxes at column 0 outside a fenced block (CBN-346) beneath each one up to the next `## ` outside a fenced block (CBN-344), and on the micro template's `## ⚡ Notes` heading. `external_review_box()` (§"External review", CBN-257) relies on the full template's Phase 3 `- [ ] **External review**` box — the bolded label matched literally, its disposition read from whatever follows, then from the first `External review:` line below the box in the same section (CBN-337); an unbolded label opening a box, and a boxless Phase 3 `N/A` line, are tolerated drift reads (CBN-341) — and on the same phase-vs-`## ⚡ Notes` headings to tell a micro note (no box by design) from a full note that dropped one. Moving, renaming, or renumbering any of those is a change to this side's input. Caobunga reads the producer's format and defines none of it: a heading the template does not write is not a shape this side invents. The re-verify point is the pin bump (§"Parse contract"): `backend/tests/test_contract.py` asserts the vendored templates still match the reader regexes, so a template move fails the suite the way a `**Version:**` move does. The retired third reader and the `CORE-655` landing: [decisions/cli-fleet-surface.md](cli-fleet-surface.md) §"Fleet boundary — tasknote-template read history (CBN-235)".

## Self-drive — registration rationale (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

Caobunga's own repository is registered like any other project — explicitly,
with `caobunga register`, at a path the registry stores without knowing what
lives there. Resolution, the run loop, and both entry points treat it as an
ordinary record. There is no `--self` flag, no bootstrap hook, and no
auto-registration on first run: that would put a registry write inside the tool,
which "explicit registration only" (§"Non-negotiables") forbids regardless of
which repo is being written.

## Status API — payload-guard mechanics and rationale (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

**The detail payload's key vocabulary is guarded on both sides (CBN-EPIC-231.3).** `fixtures/api-payload-keys.json` names the expected key set for thirteen shapes — `project_detail`, `coverage_detail`, `tree_detail`, `current_task`, `reports`, `run_record`, and the element shapes nested inside them (CBN-234): `denial`, `unparsed_row`, `dirt_line`, `new_row`, `foreign_commit`, `suite`, `suite_verb`. `backend/tests/test_api_payload_keys.py` derives each set from the live endpoint or the pydantic `model_fields` that build it; `frontend/tests/unit/api-payload-keys.test.ts` derives the same sets from `frontend/src/api/types.ts` through a compiler-enforced `Record<keyof T, true>` mirror. The same file's `enum_values` block (CBN-263) names the value set of every enum the payload serializes — `execution_tier`, `outcome`, `escalation_reason`, `hermes_review`, `hermes_verdict_code`, `suite_verdict`, `external_review`, `triage_class` — checked against the backend `StrEnum` members and the `types.ts` string-literal unions (plus the `OUTCOMES` / `ESCALATIONS` chip arrays). Both diff against the fixture, so a field added to a model, the fixture, or `types.ts` without the other two fails whichever suite runs next ([decisions/cli-fleet-surface.md](cli-fleet-surface.md) §"Status API — why the payload-key guard (CBN-EPIC-231.3)"). The nested element shapes are checked on the backend by `model_fields` alone, not the live endpoint — several of those arrays are conditionally present, so no single payload carries every element.

## Fleet boundary — ledger consumer provenance (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

**Fleet HQ reads this store, and this paragraph is the producer's half of that record.** Since Fleet HQ `NAT-293` (2026-09-19), its `scripts/fleet-runs.sh` sweeps `$CAOBUNGA_HOME/runs/{project}/*.yaml` — the §"On-disk layout" path and the §"Run record" shape — into a tenth fleet surface, `docs/FLEET-RUNS.md`, and its `data/fleet-runs.json`. It joins on six fields and nothing else: `outcome`, `closed_at`, `task_id`, `provider`, `hermes_verdict_code`, and `suite.verdict`, plus the length of `marked_tasks`. Since CBN-258 `hermes_verdict_code` is legacy — no longer written, so absent on every newer record; its successor signal is `external_review` (§"External review"); it reads the files, not `GET /api/stats`, so a running server is not part of its input. Renaming, retyping, or moving any of those is a change to that sweep's input; why the note lives on both ends, and why the two folds over this ledger stay independent: [decisions/cli-fleet-surface.md](cli-fleet-surface.md) §"Fleet boundary — Fleet HQ reads the run store (NAT-293)". Its reader skips-not-fails on the same rule §"Run record" sets (CBN-106), and reads a schema-1 record's missing `suite` as *unmeasured*, never green.

## Daily fleet digest — clauses moved from CONTRACT (CBN-EPIC-450.6)

CONTRACT's digest section was at its word budget when CBN-EPIC-450.6 added the `[not landed]` tag and the `**retained**` bullet, so two rationale clauses moved here verbatim:

- On the stale-receipt bullet: "It is not day-bucketed, since a dead run's commits otherwise reach no report surface until the slug's next `run`." The `**retained**` bullet is unbucketed for the same reason: a leftover is a live condition, and the drain skips its id until the operator acts.
- On the legacy Hermes fields: "Since CBN-258 the loop writes neither field, so both appear only for records from before the retire and fade out as the window moves on."

## Entry points — batch reopen evidence (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

**Reopened (CBN-312).** CBN-124 declined a batch entry point and recorded its
reopen condition: two or more repos dispatchable at the same time, as `caobunga
status` reports. On 2026-10-08 it did — several clean repos with marked rows —
and CBN-308 / CBN-310 had already serialized concurrent slugs' suite verbs on
`suite.lock`. Evidence, the shape choices, and how each CBN-124 ground is
answered: [decisions/cli-fleet-surface.md](cli-fleet-surface.md) §"Entry points — batch entry point reopened (CBN-312)".

## Contract — status-line provenance (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

**Status:** Stable — see `git log -- docs/CONTRACT.md` for field/feature provenance (the inline task-ID enumeration this line used to carry was retired at CBN-141: hand-maintained, guarded by no test, and chronically stale — see CBN-138.N Finding 2). Implement against this file; do not re-open field names in later epics without a `schema_version` bump.

## Fleet boundary — vocabulary context (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

Caobunga is not the only system reading this fleet. The operator's fleet standards and dashboard repo is the **Fleet HQ** — it discovers every repository on disk and publishes catalogs, rollups, and stats over them. Its own `docs/FLEET-ARCHITECTURE.md` assigns caobunga the **Actuator** tier, and four of the words in this document mean something else over there.

## Status API — loopback origin and host rationale (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

Bind: `127.0.0.1:8013`. CORS is scoped (CBN-EPIC-7.3) to the two Status UI origins — `http://127.0.0.1:5195` and `http://localhost:5195` (`UI_ORIGINS` in `caobunga/api.py`), GET only. Both loopback spellings are listed because a browser treats them as distinct origins. The shipped UI is same-origin via the Vite proxy and does not depend on this; the allow-list covers a direct browser open. Non-browser clients send no `Origin` and are unaffected. `TrustedHostMiddleware` (CBN-248) additionally rejects any request whose `Host` header isn't `127.0.0.1` or `localhost` (`ALLOWED_HOSTS` in `caobunga/api.py`) — a loopback bind alone doesn't stop DNS rebinding, since a browser page can resolve an attacker-controlled name to `127.0.0.1` and send an untrusted `Host`.

## Status API — shared-read rationale (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

All three fields come from **one** read per project per request. `current`, `coverage`, and `tree` are three questions about the same `ProjectRead`, so the endpoints call `flaitron.try_read_project` once and derive all of them. `tree` does add one `git status --porcelain -uall` per project per request — accepted deliberately ([decisions/cli-fleet-surface.md](cli-fleet-surface.md) §"Status API — one read, and the `tree` cost (CBN-130)").

## Status API — history representation rationale (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

Detail `runs` is that project's full run history (CBN-EPIC-12.3) — every readable record under `runs/{id}/` (§"Run record" — unreadable files are skipped, not raised), newest-first by `closed_at`, each the same `exclude_none` run-record object as detail `last_run`, so the §"Run record" signals (`started_at`, `escalation_reason`, `hermes_review`) surface here too. Ordering is an explicit sort on this surface, not the runs-directory filename order. Unbounded in v1: no pagination, no cap. `runs[0]` is detail `last_run`; `last_run` stays for the list/detail symmetry the UI already reads.

## Self-drive — tier and enforcement rationale (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

What *is* contractual is not the record's existence — registry contents are
per-machine operator state — but the one constraint that applies to this record
and to no other: **its tier is `gated`.**

`gated` is the stance here, not merely the `register` default — a drain would
keep running modules a worker has since edited. Mechanism: [decisions/cli-fleet-surface.md](cli-fleet-surface.md)
§"Self-drive — why `gated`, and why no guard".

**Recorded, not enforced.** `register` does not special-case this path, and v1
adds no check. Why no guard: [decisions/cli-fleet-surface.md](cli-fleet-surface.md)
§"Self-drive — why `gated`, and why no guard".
