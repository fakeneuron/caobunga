# Decisions — Flaitron reader

Measurement, history, and rejected options behind `docs/CONTRACT.md` §"Flaitron reader", including §"Marker coverage", §"Parse contract", and §"Unattended capability probe" (CBN-271.4). The rules themselves live in `CONTRACT.md`; this file records why they are shaped the way they are. Reference-only: nothing loads or dispatches on it.

Headings name the `CONTRACT.md` section each entry backs.

## Flaitron reader — why two dispatch parameters (CBN-118)

`unattended` and `drain` were one parameter from CBN-EPIC-17.3 until CBN-118, because the sets happened to coincide when the second was added; they do not coincide, and the conflation is why fixing one appeared to require breaking the other.

`drain=True, unattended=False` raises rather than defaulting because that is the `worker_message` required-keyword precedent applied one level up: a permissive default here would keep every call site compiling while quietly undoing the guard.

## Flaitron reader — `Blocked by` grammar port and the CBN-011 overturn (CBN-116)

Reading upstream's grammar (a port of `viz/src/parser.ts` `extractBlockedBy`) rather than restating it keeps this beside the existing task-line pin (§"Fleet boundary", CBN-EPIC-13.2) instead of opening a second divergence surface.

The gate supersedes CBN-011's recorded decision that `Blocked by` "is not a skip rule in the reader" (deferred there to CBN-013, which deferred it back; neither built it). The overturn is architectural: CBN-EPIC-17.2 made `eligibility` the single place a dispatch decision may live, so siting the gate in the run loop instead would leave `observe`'s echo and the Hermes next-candidate line still naming a blocked row as next.

Tasknote YAML `blocked-by:` was not made a gate even though the ticket that filed this work implied it should be: `SPEC/blocked.md` and `SPEC.md` §"Tasknote frontmatter" both state the key is "a *planning* claim ..., not a don't-start gate and not a park", and enforcing it here would make Caobunga gate on something flaitron defines as non-gating.

## Flaitron reader — stale-clause annotation reads PLAN.md only (CBN-125, CBN-192)

`completed_ids` is built by `completed_task_ids` from the same `tasks` list every reader here gets from `PLAN.md` alone — it never opens `.flaitron/PLAN-ARCHIVE.md`. So once an operator's `## Completed` rotation moves a blocker's row to `PLAN-ARCHIVE.md`, its id drops out of `completed_ids`, and a row still `Blocked by [[that id]]` denies with the plain `blocked — Blocked by [[ID]]` reason, silently missing the `(completed; clause is stale, remove it)` pointer. Flaitron's own contract for callers (`docs/EXTERNAL-AGENTS.md` §"Stable surfaces for callers" → "The closed-row set") says a caller resolving an id against completion "reads both" `PLAN.md` `## Completed` and a rotated `PLAN-ARCHIVE.md`; this reader does not, for either consumer of `completed_task_ids`. Recorded rather than built around, on the same reasoning CBN-125 gives for `marker_drift`: opening a second file would widen the single-root `read_project` contract, a design change a doc-only fix does not make.

## Flaitron reader — exploratory dimension history (CBN-114)

Before this dimension existed an `[xheavy] [unattended]` row dispatched like any other, resolving through `taskmap.classify` to the heavy class — `SPEC/model.md`'s exact prohibition, reached by a route nothing checked.

The task that filed this work (CBN-114, from CBN-112) scoped it as `TaskClass.XHEAVY` plus a `MODELS` row across seven providers and a new `EFFORT` rung; Discovery re-scoped that away as routing machinery for a dispatch the contract forbids. It does not fire on the attended default because manual-only means the operator may work the row by hand. It is checked before the marker because seeding `[unattended]` on an `[xheavy]` row would not make it dispatchable, and reporting `unmarked` would send an operator reading `Coverage.denials` to do exactly that.

## Flaitron reader — upstream marker work (CORE-494, CORE-495)

Caobunga read the marker first; the upstream work filed at CBN-EPIC-17.4 landed in flowtron **v5.22.0**. **CORE-494** captures `[unattended]` in `SPEC.md` §"Task-line format" and `viz/src/parser.ts` instead of dropping it as a stacked-`[model]` tolerance, and documents both misplacements as named footguns rather than things to discover. **CORE-495** closes the companion 👁️ gap: a Phase 3 visual-confirmation ask is now *parked*, not suppressed, under `--unattended`, which adds `visual-confirm` to flaitron's park-reason set — absorbed here with no edit at all, because `park_reason` is typed `str` rather than an enum for exactly this reason (§"Park reason"). Before that release the marker was a tolerated-and-discarded token to every other adopter's parser, which is why it was safe to read early.

## Flaitron reader — unattended filing authority upstream (CORE-551, CBN-138.5)

Filed `536d68b`, implemented `b992dbf`, released in flowtron **v5.26.0**, pinned here at CBN-139 — the bump that made this repo's own follow-up cue the capable one for the first time. It closes a gap one layer below the marker: flaitron's `--unattended` posture obliges a run to file a deferred hand-off as an unchecked PLAN row (`SPEC.md` §"Deferred hand-off filing") while leaving that filing impossible — `/ft-file-followup` rejected `--unattended` as an unknown arg *and asked*, `SPEC/tasknote-selection.md` §"Filing commits" grounded commit authority in an operator act ("by passing the park flag and answering the priority question"), and `unattended-mode.md`'s never-relaxes list omitted the duty entirely. v5.26.0 adds §"Unattended filing authority": the authorization is the duty itself, since the run "is discharging a duty `SPEC.md` imposes, not exercising discretion, and the operator authorized it upstream by launching an unattended run against a SPEC that imposes it."

CBN-138.2's filed scope named `--park --<prio>` and was reconciled to the default flow at CBN-138.5's closure; a worker cue that shipped the original wording would have instructed the pinned skill to refuse.

Verified from the committed state through both parsers, the check CBN-EPIC-17.4 recorded as skipped: flaitron's own `viz/src/parser.ts` (extracted to a scratchpad via `git show`, so no probe file entered that tree) and caobunga's `parse_plan` agree — `CORE-551 | High | heavy | unattended-filing-authority`, `unparsed: []`. The id is `551`, not the `543` drafted here: `CORE-543` was taken by `ci-drift-job-binding` between the draft and the run, which is the hazard CBN-137 named and the hand-off prompt's self-resolve step caught.

## Marker coverage — `unparsed` stays report-only (CBN-115, CBN-EPIC-18.4, CBN-119.2)

Before the field existed a dropped row vanished without trace: seeding a marker into a shape the grammar rejected — originally, and until `CBN-167`, a suggestion glyph placed *before* `[unattended]` (the natural motion on a rendered board; upstream `CORE-502`) — made `open` fall by one while the report read fully covered, `marked + denied == open` intact. Checked rows were added at CBN-210 because a checked-but-malformed row vanished from every reader — `parse_plan` drops it, and this field used to skip it outright too — so a caller had no way to tell a mis-detected close from a genuinely still-running one.

The count was not echoed on the dispatch path until CBN-119.2: `loop.py` never read `coverage` at all, so an operator who seeded a marker and ran straight into a drain got no signal that the seed had deleted the row.

CBN-EPIC-18.4 first kept it report-only on the ground that an escalation would be "tuned against zero observed failures"; CBN-119.2 re-opened that on two observations (18.4's own seeding, caught pre-flight; CBN-114's fixture row) and 50% measured glyph exposure across the fleet, and kept the no-refusal half **on its merits**:

- a dropped row causes *under*-dispatch, never an unsafe dispatch, so the bias-to-refuse `eligibility` applies to the marker does not transfer;
- the row carries no id by definition, so a refusal could not be row-scoped the way every other denial here is;
- `CHECKBOX_ROW` is the bare checkbox prefix, so one stray prose checkbox would wedge a whole repo's drain — clearable only by editing *and committing* in someone else's tree.

"Nothing to run" is where a dropped row misleads most, which is why the exit-3 message carries the unparsed count: without it the message names the rows that *survived* a botched seed and stays silent about the one the operator was trying to seed.

A prose checklist item with no `*` anywhere on it is excluded (CBN-209, the reference parser's `FE-087` gate and the `exclusions` fixture's "excluded silently" rows), while a genuine id near-miss still carries the emphasis it fumbled and keeps flagging.

## Marker coverage — scoping to launching tiers rejected

Scoping coverage to the launching tiers would be honest about current dispatch and useless for the purpose: every `gated` repo a registration wave just added would report a blank exactly where the seeding backlog is. CBN-118 made the point moot in the report's favor — every launching tier now consults the marker, so this is no longer a forecast of a possible promotion but a direct read of what the project's own tier will dispatch.

## Marker coverage — seeding worklist shape (CBN-119.3, CBN-167)

Naming the denied rows told the operator *that* they had work; it did not tell them where or what, so every seeding pass still began by opening the target PLAN and searching for each id.

The before-glyph placement was the whole point when this shipped. Measured across the fleet when this shipped, **11 of 21 denied rows (52%) carried a glyph**, and the natural motion — typing the marker at the end of the decorations — puts it after the glyph; at the time, that placement matched no fragment of `TASK_LINE` and dropped the row entirely (upstream `CORE-502`). `CBN-167` closed that gap — `TASK_LINE` now accepts the glyph on either side of the trailing-token run — but `seeded_line` kept emitting only the before-glyph placement, on the one-shape-is-easier-to-verify reasoning, until `CBN-343` below; the measurement records why the placement choice mattered, not a gap that still exists.

`CBN-343` then moved the emitted placement to after the glyph. Once both shapes parsed, the before-glyph shape no longer had a parsing reason, and flaitron's candidacy module (`SPEC/unattended-candidacy.md` §"Three postures") writes `[light]🔧 [unattended]`; emitting the shape upstream filers write keeps the one-shape reasoning while matching the fleet. The insertion point stayed the end of `TASK_LINE`'s trailing-token group, which already sits past a glyph directly behind `[model]`. A glyph after a stacked-`[model]` run fills the grammar's second slot, which no token may follow in `parser.ts` either, so those rows keep the before-glyph placement — the round-trip check would refuse anything else.

Re-parse verification exists because a row with no `[model]` fails quietly: `**ID** [unattended]` still parses, with the marker captured *as* the model, so the row reports `unmarked` forever with no trace of the attempt. `None` on `blocked` / `exploratory` avoids the misdirection CBN-114 closed when it put the exploratory check ahead of the marker one.

Ordering by `_selection_key`: document order matched dispatch order on all five repos when this shipped, but by coincidence of how the operator happened to file; sorting on the picker's key makes it a property rather than an artifact. `denied` is built by the same constructor because two lists of the same DTO over the same rows must not disagree about order, and a second construction site would have carried `line=0` — a zero that reads as data.

The 96-character elision: a real description runs to several hundred characters, and printed whole the `now:` / `seed:` rows wrap into a wall whose only difference is thirteen characters near the start; the edit always lands in the decoration run at the head, so eliding the tail is what makes the placement visible.

## Marker coverage — unmarked triage (CBN-157)

The worklist still made the operator do the triage the report already had the information for: reading every `unmarked` denial to find the ones a marker mechanically fixes versus the ones that need a person's read first. The predicate is mirrored rather than invented because flaitron's own candidacy predicate (`SPEC/unattended-candidacy.md`, `CORE-577.2`) is the one caobunga's filers would themselves be judged by, and this task started only once that module existed — the same discipline `TASK_LINE` applies in mirroring `viz/src/parser.ts`. Non-`unmarked` denials carry `triage=None` since the predicate never judged them, and labeling them `needs-judgment` would claim a verdict this report never reached.

The epic-child stem rule is checked through `epic_stem`/`epic_parent`'s numeric fallback because this reader has no access to a tasknote's `## 🌳 Fan-out` claim (`flaitron/` reads PLAN.md and tasknote frontmatter only, never phase content).

The `audit` dimension was added to the same triage later (CBN-353). `eligibility` applies it only to a marked row, so triage had cleared an unmarked `.N` audit as `obvious` on clauses 1-4 alone; the CBN-351 trial then seeded a fleet repo's PID-078.N and eligibility denied it `audit — epic siblings still open (PID-078.3)`. Triage calls `_audit_sibling_gap` directly rather than re-deriving the sibling rule, so the two cannot disagree about which audit rows wait.

Clause 3's keyword miss stopped counting as that mechanical fix (CBN-361, flaitron CORE-783.2). The list stays the screen and gains no visual or raster keywords. A miss is `needs-judgment` (`keyword — a miss is not a semantic pass`). The semantic decline belongs to an LLM filer and `/ft-seed-unattended`, which this reader does not run, so `status --seed` no longer lists a row on a miss alone. Per-repo `status <id>` still prints `seed:` for a `needs-judgment` row; the operator's mark is the act.

## Marker coverage — paste-ready `sed` block (CBN-157)

`seeded_line` is elided and prints with no leading whitespace preserved, so it names the fix but cannot be pasted — and hides that an indented epic child needs its indentation reproduced too. The pass this task was filed from hand-wrote a `sed` anchored at column 0 that missed 7 of 11 rows, every one an indented child.

`-i.bak` rather than BSD's `-i ''`: CI caught the space-separated empty-suffix form the hard way — GNU sed has no detached `-i` argument, so it reads the empty string as the script and the real script as an unopenable filename.

The `grep -qF` guard is one fleet repo's own lesson, where a bare `[model]`-anchored `sed` with no such guard re-matched on re-run and doubled the token (`d1d14d9a`). The trailing commit-scope comment comes from the same pass finding a `git commit -am` sweep an unrelated session's uncommitted work into the seed commit.

The block needs no line-start anchor: the longest common prefix of `Denial.text` and `Denial.seeded_line` is always unique in a well-formed PLAN.md (it contains the full `**task_id**` token), so indentation never enters into the match.

## Marker coverage — unparsed blockers (CBN-123, CBN-128)

CBN-123 measured the cost of a worklist that answered *where* but not *whether*: it seeded three rows across two repos, and all three carried real blockers in prose the grammar drops — `Blocked by an external prerequisite …` with no wikilink, and `Blocked on [[ID]]` twice. SPEC §"Long-description conventions" parses `Blocked by [[ID]]` only, so all three parsed as unblocked, denied as plain `unmarked`, and were offered a paste-ready `seeded_line`. One of them read *"do not start before the rows exist"*. CBN-116's blocked dimension is not broken — it is inert on this fleet, guarding a grammar the fleet does not write; the producer-side half is filing discipline and files upstream (CBN-126).

The test needs no frequency calibration (unlike CBN-119.3's 52% glyph measurement, which was tuning a *placement*): it is a statement that two readings of one sentence disagree. Every clause CBN-123 met sat past `_ROW_BUDGET`'s 96 characters — the budget sized so marker *placement* stays visible, and therefore hiding *eligibility* by construction — which is why the fragment starts at the mention.

Suppressing `seeded_line` beside the warning would make Caobunga decide that a producer's *sentence* means "blocked" — the same §"Fleet boundary" act that widening the grammar was rejected for, one step further in. The repo's own precedent already reads this way — a stale blocked clause still denies and is annotated `(completed; clause is stale, remove it)` rather than auto-released.

`unparsed_blocker` is the tail fragment rather than the whole line so a renderer eliding it spends the same budget on the clause instead of the decoration run; `caobunga status` prints it between `now:` and `seed:` so the caution sits immediately above the thing it cautions against.

Detection blanks code spans in place because `parse_blocked_by`'s deletion is right for extraction, but a caller reporting *where* a mention sits needs offsets that still index the original, so the quoted fragment is the producer's own words rather than a stripped paraphrase. Same idiom as `_blank_html_comments` over the PLAN body.

## Marker coverage — `[handoff]` dimension (CBN-136)

Five tasks have carried the hand-off shape — CBN-EPIC-17.4, CBN-120.2, CBN-126, CBN-123, CBN-133 — all with the same form: the deliverable is an operator-run prompt into a repo Caobunga may read but not write (`CONTRACT.md` §"Marker coverage" → Writes). CBN-133 parked mid-Phase-2 for exactly that.

The cost was a wrong recommendation, not a missing one: `_denial` offers `seeded_line` when the code is `unmarked`, so a hand-off row denied as `unmarked` and `caobunga status <id>` printed the exact `[unattended]`-seeded line to paste — the report actively recommending the seed that must not happen. This is the misdirection CBN-114 closed for `[xheavy]`, and its argument transfers verbatim. Suppression then costs no code — `_denial` already keys on the code — but it is the point, so a test pins it. CBN-120.3 measured this shape live: on a row that was unmarked anyway, allow/deny was identical either way and the reason was the whole discriminator.

Position independence (CBN-260): `TASK_LINE` captures whichever bracket token comes first after the id *as* the model, so a slot-keyed check missed the same token one position over — `**X** [handoff] [unattended]` read as `model="handoff"` and dispatched as `marked`, and `[heavy] [xheavy] [unattended]` put the rung in the trailing run where the exploratory check never looked.

Why it denies even with `[unattended]` present: CBN-123 measured an operator seeding `[unattended]` on a row whose own description read *"do not start before the rows exist"*; a signal that only annotates is silent exactly there. **The counter-argument is sound and is recorded rather than buried**: CBN-119.2's rule is that bias-to-refuse transfers only to *unsafe* dispatch, and CBN-EPIC-14.5 proved the park round-trip works end to end — so a hand-off dispatch is wasteful, not unsafe, which argues for the `unparsed` / `marker_drift` / `unparsed_blocker` posture of "state it, refuse nothing." It lost to the mis-seed case, not to a stronger principle. A later task revisiting this should start there.

A hand-off is not a dependency (nothing upstream completes to release it) and not an absent opt-in (no seeding makes it dispatchable); it is a durable property of the work, known at filing time. On attended reads the row stays the candidate and its eligibility reason names the hand-off: that is the next-task-suggester half — the signal is surfaced rather than hidden, which is what makes it useful to a reader who is not dispatching.

## Marker coverage — `parked` dimension measurement (CBN-252)

Before the dimension the pick still chose a parked row: every re-run wrote a `note-blocked` record in 0 s, spent a Hermes review on it, and never reached the rows behind it — measured on a fleet repo's `SCI-102` (three runs, one real) and on this repo's own CBN-252 (parked, then re-picked).

## Marker coverage — `retained` dimension (CBN-EPIC-450.5)

`retained` denies an id an isolated dispatch left on a `wt-<ID>` branch, worktree or directory. It sits first among the launching-tier dimensions, just ahead of `parked`: the branch may hold the only copy of the work, and nothing on main clears it. `parked` keeps its CBN-252 rationale one place lower. Resuming attended is the one fix that clears that row whatever else it carries, and, as with CBN-114's ordering, the reason itself is the discriminator. Neither carries a `seeded_line`: the row is usually already marked, and a marker is not the fix. Why it is read from git, per pick: `decisions/worker-run-loop.md` §"Run loop — isolated park and continue (CBN-EPIC-450.5)".

## Marker coverage — drift incident (CBN-120.2, CBN-125)

CBN-120.2 caught marker evaporation live **twice on the same rows inside one trial** — while one fleet repo sat parked, an unrelated `CORE-EPIC-4` session rewrote the `DATA-13.4`/`.5` lines, taking a seeded cohort from **3 marked to 0** and adding already-satisfied `Blocked by` clauses in the same edit. Nothing surfaced it until the next drain refused. A marker-only test would have reported half that repo's damage and stayed silent on the stale clauses that arrived in the same rewrite, which is why the verdict is `eligibility`.

## Marker coverage — cross-repo reads (CBN-123, CBN-133)

The NAT-195 path-access hook blocks cross-repo Read/Edit even against explicit in-conversation approval, and CBN-123 resolved that by having the operator add `~/Code` to `~/.claude/path-access-roots`. CBN-133 kept that root as the standing posture. Prefer the direct read: CBN-133 drafted a Fleet-HQ-side filing from the hook's *current* source and found CBN-123 F4's own description of it wrong, an error a transcription-based draft would have filed upstream — the `CORE-494` failure class of asserting an adopter-local reading as upstream fact.

## Parse contract — pin-move history (v5.21.0 → v5.33.0)

Each bump's re-verify has landed one of five outcomes:

- **v5.21.0 — grammar moved, `TASK_LINE` followed (CBN-112).** `CORE-484` (the `[xheavy]` exploratory tier) gave the tolerated suggestion glyph `🔭`, and a `[xheavy]🔭` row failed to match — dropped silently from the candidate set rather than parsed. Upstream `viz/src/parser.ts` moved its own `SUGGESTION_GLYPH` in the same release; `flaitron/` had not. The contrast with `CBN-EPIC-14.2` — where the same re-verify correctly found the section byte-identical and changed no code — is why the re-verify is a read, never a rubber stamp.
- **v5.22.0 — independent convergence (CBN-EPIC-17.4 → `CORE-494`).** Upstream's new captured `TRAILING_TOKENS` fragment — `((?:\s+\[[a-z][\w.-]*\])*)` — is byte-identical to the group `TASK_LINE` had already been capturing since CBN-EPIC-17.2, in the same position, feeding the same membership test. No code change. That is the outcome the early read (§"Picker") was betting on, and it is the weakest of the first three outcomes as evidence: convergence confirms the bet paid once, not that reading an unratified token is generally safe.
- **v5.23.0 — declined (CBN-127), then reversed (CBN-167).** All of the `parser.ts` movement between v5.22.0 and v5.24.0 landed in this one release. Upstream `CORE-502` added a second, non-capturing `SUGGESTION_GLYPH` slot ahead of `TRAILING_TOKENS`, so `[xheavy]🔭 [unattended]` and `[xheavy] [unattended]🔭` are equivalent (`SPEC.md` §"Task-line format" documents both as tolerances). CBN-127 left `TASK_LINE` placing the glyph only after the tokens, on the rationale that widening it for a decorative tolerance would be defining the producer's format (§"Fleet boundary"), and treated the before-tokens shape as a known, reported gap via `unparsed_plan_rows`. Flaitron's own session then wrote exactly that shape on 2026-09-11 (`CORE-579`) and it dropped to `unparsed` on `caobunga status flaitron` — an operator-authorized, upstream-tolerated row that could not dispatch. Both halves of CBN-127's rationale failed: the tolerance is not decorative once the fleet writes it, and mirroring a shape `SPEC.md` §"Task-line format" itself documents is *reading* the producer's format — the §"Fleet boundary" concern was about shapes the SPEC does not name.
- **v5.31.0 — byte-identical (CBN-220).** `git -C .flaitron/core diff v5.30.0..v5.31.0 -- viz/src/parser.ts` returns nothing, and the same diff against `SPEC.md` touches only the `**Version:**` line — §"Task-line format" itself is unchanged. Same outcome as `CBN-EPIC-14.2`: pin moved on confirmation alone.
- **v5.33.0 — extraction, not grammar change (CBN-241).** `git -C .flaitron/core diff v5.31.0..v5.33.0 -- viz/src/parser.ts` is comment/provenance cleanup only (stripped `FE-*`/`CORE-*` inline citations, no regex or fragment-order change), matching the tag notes ("a round of comment/provenance cleanup across the visualizer"). `SPEC.md` §"Task-line format" moved its per-segment semantics into `SPEC/task-line-segments.md`, leaving the grammar block in place. `templates/tasknote-micro-template.md` is byte-identical; `templates/tasknote-template.md` only adds the Phase 3 External review and Phase 4 Learnings boxes CBN-235 already reads structurally — no heading moved. `TASK_LINE` needed no change.
- **v7.0.0 — one glyph, `TASK_LINE` followed (CBN-330).** `git -C .flaitron/core diff v6.0.0..v7.0.0 -- viz/src/parser.ts` adds `💎` to `SUGGESTION_GLYPH` (the `[frontier]` rung's glyph, shipped upstream at v6.1.0) and otherwise touches comments only. A `[frontier]💎` row dropped to `unparsed` until `TASK_LINE`'s two glyph slots and `LEADING_SUGGESTION_GLYPH` took it. The v7 micro template also gained an `**External review:**` line in `## ⚡ Notes`, which `external_review_box` now reads (CONTRACT §"External review").
- **v7.2.0 — byte-identical (CBN-380).** `git -C .flaitron/core diff v7.0.0..v7.2.0` is empty for `viz/src/parser.ts` and both tasknote templates. `SPEC.md` changes its `**Version:**` line and two prose paragraphs (External-review scope, post-closure rotation count), and §"Task-line format" is unchanged. `SPEC/task-line-segments.md` only adds a `/ft-prep-unattended` sentence. The pin moved after this read; `TASK_LINE` needed no change.
- **v7.3.0 — byte-identical (CBN-406).** `git -C .flaitron/core diff v7.2.0..v7.3.0` is empty for `viz/src/parser.ts`, both tasknote templates, `SPEC/fixtures`, and `viz/`. `SPEC.md` changes only its `**Version:**` line, so §"Task-line format" is unchanged. `SPEC/task-line-segments.md` and `SPEC/plan-filing.md` only fold `/ft-prep-unattended` into `/ft-seed-unattended` in prose; the `[unattended]` segment grammar is untouched. The pin moved after this read; `TASK_LINE` needed no change.
- **v7.4.0 — regex unchanged (CBN-415).** `git -C .flaitron/core diff v7.3.0..v7.4.0 -- viz/src/parser.ts` is comments only: the category-label comment renames `medium` to `moderate` and notes legacy `medium` is stored as written. `MODEL_TOKEN`, `TRAILING_TOKENS`, `SUGGESTION_GLYPH`, and `const TASK_LINE` are untouched, and the token class `[a-z][\w.-]*` already accepts `[moderate]`. Both tasknote templates are byte-identical. `SPEC.md` §"Task-line format" is unchanged; §"Model field" renames the ladder rung `medium` to `moderate`, records legacy `[medium]` as a read-only alias, and narrows the `frontier` trigger to a decision that is still open and hard to unwind. `SPEC/task-line-segments.md` updates the recommended label and one example row. `SPEC/fixtures` renames sample `[medium]` rows to `[moderate]` and keeps one closed `[medium]` row as the legacy alias. `SPEC/plan-parser.md` adds the legacy-token paragraph and renames the glyph's prose label. The `SPEC/plan-filing.md` diff is empty. The pin moved after this read; local `TASK_LINE` needed no change.
- **v7.5.0 — byte-identical (CBN-417).** `git -C .flaitron/core diff v7.4.0..v7.5.0 -- viz/src/parser.ts` is empty. Both tasknote templates are byte-identical. `SPEC.md` changes only its `**Version:**` line, so §"Task-line format" is unchanged. `SPEC/task-line-segments.md`, `SPEC/plan-parser.md`, `SPEC/plan-filing.md`, `SPEC/fixtures`, and `SPEC/model.md` are absent from the range. The release notes are a `--micro` routing default, a `chore:` rule for `.flaitron/`-only closures, and a `📭 EMPTY` post-closure cue. The same tag's Also trailer (no Fable ladder cell at medium, a third refused `--unattended` park carve-out, viz advisories, dogfood re-verify) is also prose. None of it moves the task-line grammar. `viz/src/devApi.ts` changes a loopback-bind comment and does not touch `parser.ts`. The pin moved after this read; local `TASK_LINE` needed no change.
- **v8.1.0 — comments only (CBN-445).** The range spans v8.0.0 and v8.1.0. `git -C .flaitron/core diff v7.5.0..v8.1.0 -- viz/src/parser.ts` only moves the `scanDocument` comment block below `parseTaskLine`; no regex, fragment, or `SUGGESTION_GLYPH` changed. Both tasknote templates, `SPEC/fixtures`, `SPEC/task-line-segments.md` and `SPEC/plan-parser.md` are absent from the range. `SPEC.md` changes its `**Version:**` line and the paper-complete guard (the `🚧 DIRTY` cue, and a lone `--unattended` park committing its own note); §"Task-line format" is unchanged. The audit-overlay template gained a `context`-domain sentence that `.claude/skills/audit` already states. The v8.0.0 `GET /api/active` shape change is flaitron's visualizer API, which the reader does not use. v8.1.0 ships the park commit CBN-445 proposed (CORE-843); the cue sentence it makes redundant is CBN-446's to retire. The pin moved after this read; local `TASK_LINE` needed no change.
- **v8.2.0 — prose only.** `git -C .flaitron/core diff v8.1.0..v8.2.0` is empty for `viz/src/parser.ts`, both tasknote templates, `SPEC/fixtures`, and `SPEC/plan-parser.md`. `SPEC.md` changes its `**Version:**` line and three prose paragraphs (scope-guardrail pointer, the operator-less scope check, the `/ft-update` follow-through writes); §"Task-line format" is unchanged. `SPEC/task-line-segments.md` and `SPEC/plan-filing.md` only widen the `[unattended]` admission prose (a heavy row on a complete readiness basis; a scope starter or one bounded question as a prepare disposition); the segment grammar is untouched. The marker is still read by name, so caobunga's dispatch gates (the `[unattended]` requirement, the `[heavy]` cap, the `[xheavy]` and frontier denials) are unchanged. The pin moved after this read; local `TASK_LINE` needed no change.

## Parse contract — the guard's self-consistency limit (CBN-111)

`CBN-EPIC-14.2` bumped the pin and confirmed the guard flip red→green around that edit, but its Final Summary overstated the result as "asserts against the actual latest upstream tag" — it does not; it only re-confirmed self-consistency at that one bump. Currency belongs to Fleet HQ's alignment check because it needs a sibling `~/code/flaitron` clone, which a hermetic backend unit test should not depend on.

If the checkout is simply never bumped, both reads stay frozen and in agreement, and the test passes indefinitely no matter how far flaitron's actual latest release moves ahead.

## Parse contract — `[handoff]` stays unratified (CBN-136, CBN-137)

Reading an unratified `[handoff]` repeated the CBN-EPIC-17.2 posture deliberately — Caobunga reads the token now, flaitron formalizes the grammar later — and inherited that precedent's caveat verbatim. The upstream reservation was then drafted and De-scoped rather than filed: flaitron's `docs/VISION.md` §"What we won't accept" rejects "abstractions without two-project precedent", and `[handoff]` has exactly one consumer — this repo. The filing would have failed flaitron's own stated acceptance criterion whenever it was made. If a second project ever needs the marker, the precedent requirement is met and CBN-137's archived draft is ready to file. That `[!handoff]` is surfaced by flaitron's `parsePlanWithDiagnostics` rather than dropped is a CBN-137 Discovery finding; conflating the two parsers' behavior is the CORE-494 failure class.

Upstream is safe too: flaitron documents trailing bracket tokens as "tolerated and dropped — *except* `[unattended]`" (`SPEC/plan-parser.md` since v5.25.0's lazy-module split; `SPEC.md` §"Task-line format" before it), so an unratified `[handoff]` cannot break flaitron's own parser or `viz/src/parser.ts`.

## Parse contract — unparseable-YAML fallback (CBN-186, CORE-599)

The case that forced the line-wise fallback: flaitron's `--unattended` writer emits `park-reason:` prose unquoted, and a `: ` inside an unquoted plain scalar is a YAML mapping indicator, so `visual-confirm — Phase 3 👁️ CONFIRM: header renders` broke the whole block. The parked note vanished from the open set, `inspect_close` never reached its `worker-parked` branch, and the run recorded `failed / worker-exited` with the worker's unrelated stderr attached as the cause (a fleet repo's APP-74, 2026-09-13). The CBN-EPIC-7.4 rule was written for a *missing* frontmatter, not for one key's prose.

Flowtron v5.28.0 fixes the root cause upstream (`CORE-599`): `SPEC/blocked.md` §"Park reason" now requires every `park-reason:` value to be a YAML plain scalar — forbidding `: ` and ` #` in the prose, and a trailing `:` — mirrored at both runner writers. The fallback stays as defense in depth for notes parked before the bump and for producers that never adopt the discipline.

## Parse contract — vanished-file race (CBN-168)

Before CBN-168 the exception escaped, the run wrote no record, and the crash's cleanup killed a worker that had finished everything but its commit.

## Unattended capability probe — version floor rejected

Comparing the target's `**Version:**` against `v5.20.0` would be the symmetric move to the pin guard, and is deliberately not what the probe does. The question that decides the outcome is whether *this* pin's arg parser accepts the token; a declared tag can be ahead of, behind, or unrelated to the files on disk (a fork, a hand-moved submodule, a partial checkout). CBN-EPIC-14.5 verified the same fact by hand the same way — "by reading the pinned `SKILL.md` arg-parse block and the presence of `unattended-mode.md`, not by the tag alone" — and the probe encodes that method. It also needs no semver comparator and no floor constant to keep current.

## Unattended capability probe — filing probe is token-only (CBN-138.2)

**Retired at the v7 hard cut (CBN-330).** Every target pins flaitron v7, whose `/ft-file-task` accepts `--unattended`, so the probe, its degraded `### Follow-ups proposed` cue, and its echo were removed and the cue names `/ft-file-task --unattended` unconditionally — the CBN-278 `.flaitron/` stance, no old-name fallback. The history below is why it existed.

The posture reached `/ft-task` at flowtron v5.20.0 and `/ft-file-followup` only at v5.26.0 (`CORE-551`), so the two probes cross; this repo was exactly that target while it pinned v5.25.0.

`CORE-551` added its posture **inline** and shipped no new file — the v5.26.0 `ft-file-followup/` file set (`SKILL.md`, `park-mode.md`) is identical to v5.25.0's — so there is no second half to require, and inventing one would answer `False` on every capable target. `--unattended` occurs **0** times in the v5.25.0 skill and **16** in v5.26.0, because the older usage notice does not list the flag it rejects.

**The probe answers a slug, not a bool (CBN-329).** Flaitron v7 hard-cut renamed `ft-file-followup` to `ft-file-task` (`CORE-769.3`), so a fixed directory read answered a false `False` on flaitron-self first and would have on every adopter after its v7 bump — and the capable cue would have named a skill the pin no longer ships. The probe reads `ft-file-task` then `ft-file-followup` and returns the first slug carrying the token; the cue formats it in. The degraded cue and its echo name no slug: a sub-v5.26.0 pin and a v7 pin whose `ft-file-task` read failed both land there, and only one of them ships `ft-file-followup`.

## Flaitron reader — resume survives the drain-side drop (CBN-109)

A worker that died mid-task left PLAN untouched, so under `drain=True` step 2 returns the same id it returned when the task was dispatched, and the candidate still carries the open note — which is what makes the run loop send the continue prompt (`CONTRACT.md` §"Continue prompt") rather than a start-only `/ft-task`. Dropping the in-progress override on a drain therefore needs no special case for the `CBN-109` resume path.

## Flaitron reader — `[xheavy]` is flaitron's rule (CBN-114)

The exploratory dimension is not Caobunga's own policy but flaitron's, applied to its addressee: `SPEC/model.md` §"The `xheavy` rung is manual-only" says "an orchestrator or any other automated chooser must never assign `[xheavy]` to a task nor **pick up an `[xheavy]`-tagged one**; the tag is the operator's deliberate opt-in." Caobunga is that orchestrator.

## Flaitron reader — the frontier band is caobunga's cap (CBN-331)

Unlike `[xheavy]`, the frontier denial is caobunga's own rule and not flaitron's. Flaitron v6.1.0 added `[frontier]` 💎 as a *chooser-assignable* rung and moved its automated-chooser cap from `[heavy]` up to `[frontier]`. The operator decided on 2026-10-11 that caobunga dispatches `[light]` / `[medium]` / `[heavy]` only, because the token-heavy rungs are for the operator to authorize. The concrete `fable` / `mythos` tokens are denied too, because `SPEC/model.md` bands them at frontier, and before this they passed straight through to Fable at `xhigh`.

The denial got its own `frontier` code rather than reusing `exploratory` (operator choice), because flaitron does not call frontier work exploratory and the two have different authors. It sits right after `exploratory`: a row carrying both is manual-only first. The `exploratory` reason was reworded at the same time, because "automated choosers cap at [heavy]" had stopped being true of flaitron.

## Flaitron reader — the two upstream filing decisions (CORE-551)

Two upstream decisions land on this repo's side of the boundary, both narrower than the filing asked for:

- **`--park` and `--unattended` do not compose** — refused terminally (`⏸ --unattended stop — flag-conflict`), because park mode "resumes *their* interrupted work inline" and both halves presume an operator. The discharge path is the **default flow** with `--unattended`, not park mode.
- **A run that parks files nothing** — "a park is paused, not closed," and its `park_reason` already records what the operator resumes. That does not weaken CBN-138.4's parked-note read; it is the reason for it. A parked run's discoveries can only exist as prose *inside* the parked note, so a reader that skips parked notes is skipping the one case where filing was structurally unavailable.

The filing's history and its two-parser verification: §"Flaitron reader — unattended filing authority upstream (CORE-551, CBN-138.5)" above.

## Flaitron reader — `[unattended]` UI rows park by construction (CBN-419)

`visual-confirm` is the fleet's largest park class. On 2026-10-26 it accounted for 31 of 80 blocked run records, 27 distinct tasks across nine registered repos, between 2026-09-13 and 2026-10-20. Each of those runs parked at Phase 3's 👁️ ask. The latest seven (2026-10-16 to 2026-10-20) were examined row by row: none committed (no `commit_sha` on any record), each closed later in an attended resume, and most of those rows named their own test.

The cause is a mismatch inside flaitron, not a misread here. Candidacy is judged on the **row text** (`SPEC/unattended-candidacy.md` §"Candidacy predicate"). The park is fired by the **diff**: `SPEC/gate-postures.md` §"Park conversions" parks on every 👁️ emission and refuses a tests-cover-it split, and the 👁️ box fires for any task with a rendered surface. A marked row whose work changes a rendered surface therefore parks every time, whatever its row says. Clause 3's semantic layer (a keyword miss is not a semantic pass) asks whether a row *reads* as needing a person. A row such as "guard the NaN case, add a test" does not read that way, but its diff still touches a renderer. Five of the latest seven were plain marks by `/ft-seed-unattended`, with the token added and no words reworded, so no Pin preparation was involved. Three of those five ran on a flaitron pin that already carried the semantic layer (v7.1.0+), which means it read those rows and admitted them. The other two predate it (v7.0.0 pins).

The fix belongs upstream, as one clause-3 sentence: work that changes a rendered surface reads as needing a person, because the `--unattended` 👁️ conversion parks on it. That is a coherence sentence, not a new keyword, so the closed keyword list stays closed. The same filing has to keep §"Preparing declined rows" → Pin consistent with it: Pin lets a named test command stand in for a look, but under `--unattended` no test decides a rendered-surface 👁️, because the park conversion refuses that split. The operator files it from a flaitron session (CBN-419's archived Final Summary carries the row, tracked by CBN-422).

Caobunga adds **no dispatch guard** (operator decision, 2026-10-26). Two candidates were rejected:

- **Decline marked rows that look like UI work.** At dispatch the reader has the PLAN row and, at most, a starter. A fresh scaffold has no Acceptance to scan for 👁️ yet. Caobunga does deny some marked rows at dispatch, but only on mechanical facts: the explicit `[handoff]` token (CBN-136) and an audit's still-open siblings (CBN-309). Its triage mirror of the predicate (CONTRACT §"Marker coverage", CBN-157) deliberately leaves the semantic decline of a person-shaped row to an LLM filer and `/ft-seed-unattended` (CBN-361). Guessing "this is UI work" from a row is exactly that semantic read, turned into a dispatch gate. It would also miss a row that names no path.
- **A visual-confirm-specific recovery hint.** The park is already visible everywhere it needs to be. The digest bullet carries `(park: visual-confirm)` and the status table `blocked (visual-confirm)` (CBN-187). `fleet` exits `4` on the parked leg (CBN-372). The recovery hint names the attended `/ft-task <ID>` resume, which spends no worker session. The status `tree:` column shows the leftover dirt, which the dirty-tree refusal holds until the operator acts.

Until the upstream sentence ships, a marked UI row still costs one park and one attended resume. That is the conservative failure: the work is preserved and nothing commits unseen.

## Flaitron reader — `suggested_next` is an attended recommendation

`**Suggested next task:**` is prose flaitron wrote, and flaitron's own authorship of that line consults no marker — `[unattended]` is captured grammar since v5.22.0 (`CORE-494`) but carries no meaning flaitron assigns itself — so under an unattended posture it can only ever be an *attended* recommendation. That is why the run record checks it through `dispatchable` at the run's own posture before repeating it.

## Marker coverage — why `coverage` exists (CBN-EPIC-18.3)

`coverage` exists because the rollout question is not "what would dispatch next" but "where does this repo still need markers seeded before it could be promoted" — a question every registered project has, including the ones not promoted yet.

## Marker coverage — `open` membership

A blocked row stays *inside* `open` and reports as a denial rather than shrinking it: shrinking `open` instead would let a repo read "fully covered" while still carrying rows to seed — the silent-blind-spot class `unparsed` closed one dimension earlier. `open` is deliberately narrower than the per-task echo `observe` prints, which omits only completed rows and non-launch buckets and therefore echoes `deny: parent-epic` lines: those are a dispatch decision, not a seeding target.

## Marker coverage — why not `denied`

`denied` cannot serve the coverage job: it is posture-dependent and empty on an attended read by contract, so the status API and `caobunga status` — neither of which declares an unattended posture — would report nothing at all. This is the same split, for the same reason, that `in_flight` made from `candidate`. Scoping coverage to the launching tiers was rejected: see §"Marker coverage — scoping to launching tiers rejected".

## Marker coverage — parked note and the dirt gate (CBN-252)

A parked row's open note is not exempted from the operator-presence dirt gate: the next worker's own `/ft-task` foreign-dirt gate would refuse an untracked sibling note anyway, and a park that left source edits (a fleet repo's `SCI-102`) *should* hold the repo until the operator resumes it. So a skipped park usually still ends the invocation — exit 3 when nothing else is marked, exit 2 on dirt otherwise — but with no worker, no record, and no review, and with the `eligibility: … deny: parked — …` line naming the resume.

CBN-443 keeps the exemption out and changes what is left to refuse: the worker commits a park whose only change is its own note (CBN-443's park-check cue sentence asked for it; since CBN-446, flaitron v8.1.0's `--unattended` park recipe makes it), and a drain continues past a committed workflow-only park (`decisions/worker-run-loop.md` §"Run loop — park and continue (CBN-443)"). An uncommitted park still refuses as above, and its filing-shaped message now names the resume.

## Marker coverage — `audit` dimension (CBN-309)

`CONTRACT.md` §"Marker coverage" → "Early audits". A fleet repo, 2026-10-08: JD-041.N was seeded `[unattended]` (`cb01d6b`) while JD-041.2 and JD-041.3 were open and unmarked. Flaitron's candidacy clause 6 admits a `.N` on clauses 1–4 alone, so the seed was legal and caobunga dispatched it. The worker hit `/ft-close-epic`'s early-audit gate and parked `drift` in 77 s; its untracked parked note then refused the next marked row (JD-035) on the dirt gate. The screen fix belongs upstream (CORE-736); the gate here covers hand-added markers and adopters pinned below that release.

It sits **after** the marker, not ahead of it like `exploratory` and `handoff`. Those go first so the reason names a fix that exists, and seeding fixes neither. Here seeding is still a valid fix — a cohort seeded in one pass marks `.N` beside its siblings — so `unmarked` remains the honest reason for an unmarked `.N` and its `seeded_line` stays on offer. The drain pin leaves the dimension out for the reason it empties `Blocked by` (CBN-246): siblings closing mid-drain is a legitimate continue.

CBN-350 widened it to numbered audit children, which flaitron `SPEC/epic.md` keeps valid: a fleet repo's DEPLOY-EPIC-1.4 (`| access-gate audit`) dispatched 2026-10-12 with 1.2/1.3 open and parked `drift`. The word sits only in the `| shortname`, so that is the signal; a false match only holds a row back, so the test leans wide (`audits`, trailing punctuation). A numbered `.k` waits on *every* open sibling, later ones and `.N` included: a lower-only rule was tried in review and dropped, because `/ft-close-epic` Step 2 bails on any open sibling, so it dispatched straight into the park. An epic carrying both audit forms therefore denies both — a legible stop naming each other, which is the malformed shape the operator has to fix anyway.

## Marker coverage — drift scope (CBN-125)

Scoping drift to rows still in the open launch queue (`queue_denial` is `None`) drops three false positives by construction rather than by filtering: a row that closed legitimately, a row the operator re-prioritized out of the launch buckets, and a row `## Completed` **rotation** moved to `PLAN-ARCHIVE.md` — a file this reader does not open and would therefore report as evaporated forever. A row whose rewrite broke the *grammar* is not lost either: `unparsed_rows` already carries it, echoed on the dispatch path since CBN-119.2. What survives is exactly the claim being made — armed then, still here, not armed now.

## Marker coverage — drift is always stated, and CLI-only (CBN-125)

The `drift:` line always prints because a guard that prints nothing when it has no baseline is indistinguishable from one that checked and found the cohort intact, which is the silent-blind-spot class CBN-115 closed one dimension earlier.

The Status API gains no drift field because the diff joins a producer read with Caobunga's own run history, and `coverage` on the API is documented as derived from PLAN alone — a store-joined field there would change what that payload means. The moment drift guards is "before a drain", which is the CLI moment.

## Marker coverage — emitting a seed line is still read-only (CBN-119.3)

CBN-119.3 sharpens the read-only bound rather than bending it: the report now emits the exact line to paste, and still pastes nothing. Generating text that *satisfies* the pinned grammar is reading it, not defining it — `TASK_LINE` is untouched.

## Marker coverage — seeding practice and cross-repo access (CBN-120.2, CBN-123, CBN-133)

Fan-out `Sequential` claims are non-gating by flaitron's own definition, so stem ordering is enforced only by hand-written `Blocked by` clauses or numeric coincidence — a partial seed can mark a row whose stem predecessor is still open. Stating the ordering as a reason in the mark/no-mark proposal lands it where the operator already reviews (CBN-120.2 F3).

The standing `~/Code` path-access root and how it came about: §"Marker coverage — cross-repo reads (CBN-123, CBN-133)" above. The write bound is the read-only rule, not the hook, so widening path access does not relax it.

## Parse contract — why the pin is enforced

Writing the pin down at all is what Fleet HQ's architecture requires of a consumer that parses someone else's format (`CONTRACT.md` §"Fleet boundary"); enforcing it is this repo's own choice, on the precedent that a contract claim nothing checks is a claim that rots.

## Unattended capability probe — why not the `.claude/skills` symlink

The **submodule** path is read, not the target's `.claude/skills/ft-task` symlink: "pinned" is what `.flaitron/core` holds, and a target with no skill wired at all is a larger failure than this probe answers. The flaitron-self fallback is also not that symlink — `.claude/skills` is the *wired copy* of a pin, while `claude/skills/` (no leading dot) is flaitron-self's source tree. Reading the symlink would either loop back into a missing pin or treat a local wire as the pin.

## Flaitron reader — override ownership and resume rationale (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

**The in-progress override is a dispatch preference, dropped only on a drain (CBN-EPIC-17.3, CBN-118).** Under `drain=True` step 1 is skipped and step 2 decides alone: a note the operator left open is *their* work. `gated` keeps it — a one-shot the operator aimed at one named repo should continue what they have open. Being a *dispatch* preference, the override is subject to every dispatch dimension including the marker: it picks among eligible rows and never exempts one, so an unmarked open note is passed over at `gated` exactly as any other unmarked row is. The `CBN-109` resume path survives the drain-side drop without a special case ([decisions/flaitron-reader.md](flaitron-reader.md) §"Flaitron reader — resume survives the drain-side drop (CBN-109)").

## Flaitron reader — independent current-task display (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

**`in_flight` is the override's display half**, split onto its own `ProjectRead` field by the same task. It answers "what is open right now" (step 1, always attended eligibility) rather than "what would be dispatched next", is computed on **every** read and every posture, and is what §"Status API" serves as `current`. Splitting it is what stops a change to the picker's dispatch preference from silently blanking the dashboard (CBN-040, from CBN-011: this is the only durable current-task signal).

## Flaitron reader — stale blockers require clause removal (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

`SPEC/blocked.md` makes *clause removal* the act that clears the gate ("the task re-enters when the blocker clears — remove the `Blocked by` clause"), so a clause whose blockers are all already checked **still denies** rather than auto-releasing: dispatching a row whose PLAN line literally reads "Blocked by" would be the orchestrator overruling the operator's own text. It is reported as stale — `blocked — Blocked by [[CBN-115]] (completed; clause is stale, remove it)` — because the fix is a one-line PLAN edit and the operator should not have to diff two rows to find it. A blocker id absent from PLAN cannot be *confirmed* complete and so reads as still-blocking, the same bias-to-refuse the marker dimension applies.

## Parse contract — polling race and unreadable-git rationale (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

**The file can vanish, not just its frontmatter (CBN-168).** The watch loop globs `tasknote/` and reads each hit, and a closing worker's `git mv` into `archive/` lands between the two exactly when the watchdog wakes on that event. That is a transient state, not a malformed note, and it is answered the same way: the read returns *no* open status, the note drops out of *this* poll, and the next poll sees the archive. The same rule holds one level up — `wait_for_close` treats an `OSError` out of `inspect_close` (the PLAN's own `is_file()` → `read_text` gap included) as "not closed yet" rather than a verdict, so no poll ever ends the run over filesystem timing; a persistent problem still surfaces as `worker-exited` / `timeout`. `UserError` is not caught there: a PLAN missing at `is_file()` time remains the registration error CONTRACT names. Its one exception is `GitUnreadable` (CBN-269) — a `git` read that hung past `GIT_TIMEOUT` or found no binary — which is transient on the same terms and escalates the same way; outside the poll (the pre-launch `HEAD` read and the operator-presence dirt read, CBN-317) it refuses exit 5 as `git unreadable in …`, distinct from `not a git repository`. What the race cost before the fix: [decisions/flaitron-reader.md](flaitron-reader.md) §"Parse contract — vanished-file race (CBN-168)".

## Parse contract — trailing-token read rationale (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

**A second read of the trailing-token run needs no grammar move (CBN-136).** `TASK_LINE`'s group 5 — `((?:\s+\[[a-z][\w.-]*\])*)` — captures every trailing bracket token, and `EXTRA_TOKEN.findall` returns them all; `unattended` is picked out of that list *by name*. `[handoff]` joins it with one added membership test and no regex change. **Caobunga reads the token permanently unratified ([[CBN-137]])**, which costs nothing while flaitron documents trailing bracket tokens as tolerated and dropped (except `[unattended]`). The token carries the same two placement footguns `[unattended]` does: written before (or without) `[model]` it is captured *as* the model, and `[!handoff]` matches no slot — which drops the whole line for *this* reader, while flaitron's own parser surfaces it in `parsePlanWithDiagnostics`'s `unparsed`. Why the upstream reservation was De-scoped rather than filed: [decisions/flaitron-reader.md](flaitron-reader.md) §"Parse contract — `[handoff]` stays unratified (CBN-136, CBN-137)".

## Marker coverage — worklist invariant rationale (CBN-411)

> Extracted from CONTRACT at CBN-411; CONTRACT remains authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

- **The detail form is a seeding worklist, not a list of ids (CBN-119.3).** Each `Denial` carries the 1-based PLAN `line`, the row's raw `text`, and — when a marker is the fix — a `seeded_line`: the same row with `[unattended]` placed after `[model]` and **after any suggestion glyph** — flaitron's own placement (`[light]🔧 [unattended]`, `SPEC/unattended-candidacy.md` §"Three postures"; CBN-343) and the one shape emitted, even though `TASK_LINE` accepts either side since `CBN-167`. The one exception is a glyph after a stacked-`[model]` run (`[sonnet] [light]🔧`): no token may follow that glyph slot, here or upstream, so the marker lands before it. **The suggestion is verified by re-parsing it, never by trusting the insertion** — `seeded_line` is emitted only when the rebuilt row parses back to the same id and reads as marked, so a modelless row yields `None` and a hand edit rather than a broken suggestion. `None` is also what a `blocked`, `exploratory`, or `frontier` denial gets: seeding does not fix any of them. **`denials` is ordered by the picker's own `_selection_key`, so the list *is* the seeding order** — seed the first entry and it is the row that dispatches next. `read_project`'s posture-dependent `denied` is built by the same `_denial` constructor and sorted the same way. Detail surfaces only; the fleet table keeps its `marked/open` cell (§"Status API"). `caobunga status <id>` elides both rows to 96 characters, the tail cut so the head's placement stays visible. The placement measurement, the modelless failure, and the ordering and elision reasoning: [decisions/flaitron-reader.md](flaitron-reader.md) §"Marker coverage — seeding worklist shape (CBN-119.3, CBN-167)".

## Flaitron reader — layout hard-cut history (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

**`.flaitron/` is a hard cut (CBN-278).** Flaitron v6.0.0 renamed flowtron's `.flowtron/` tree and its `.flowtron/core` submodule, with no old-name fallback upstream, and the reader follows it: every target-layout path lives in `flaitron/layout.py` and roots at `.flaitron`, with no second root to fall back to. A target still on `.flowtron/` reads as a missing PLAN (exit 2, below) until flaitron's fleet wave (CORE-711.7) migrates it. Caobunga's own checkout and its guard tests moved to `.flaitron/core` in that wave.

## Unattended capability probe — retired filing-probe history (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

**The filing probe is retired (CBN-330).** It answered whether the pin's filing skill accepted `--unattended` (CBN-138.2) and, after flaitron v7's rename, which slug it shipped (CBN-329). The v7 hard cut leaves one answer, so §"Run loop"'s follow-up cue is static. History: [decisions/flaitron-reader.md](flaitron-reader.md) §"Unattended capability probe — filing probe is token-only (CBN-138.2)".

## Unattended capability probe — file capability rationale (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

**A capability probe, not a version floor.** The question that decides the outcome is whether *this* pin's arg parser accepts the token, and a declared tag can disagree with the files on disk. Why a version comparison was rejected: [decisions/flaitron-reader.md](flaitron-reader.md) §"Unattended capability probe — version floor rejected".

## Flaitron reader — marker-position rationale (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

The marker's position is load-bearing, and both misplacements fail **silently** — which is why tests pin them: a leading-`!` shape (`[!unattended]`) matches no `TASK_LINE` slot and drops the whole line from the parse, and a lowercase `[unattended]` placed before (or without) `[model]` is captured *as* the model token, routing effort instead of gating safety. The upstream marker and park-code history: [decisions/flaitron-reader.md](flaitron-reader.md) §"Flaitron reader — upstream marker work (CORE-494, CORE-495)".

## Parse contract — self-consistency rationale (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

**What the guard test does not catch (CBN-111).** Both reads above — the pin named here and `SPEC.md`'s own `**Version:**` line — come from the *same* local `.flaitron/core` checkout, so the test can only fail when they disagree with each other. Currency against flaitron's latest tag is the "version pin" axis of Fleet HQ's alignment check, outside this suite by design: [decisions/flaitron-reader.md](flaitron-reader.md) §"Parse contract — the guard's self-consistency limit (CBN-111)".

## Parse contract — malformed-frontmatter rationale (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

**Tasknote frontmatter.** Reading is **skip-not-fail**, the same policy §"Run record" applies to run records. A tasknote whose frontmatter is missing, valid YAML that is not a mapping, or a mapping whose `status` is not a string is treated as having *no* open status — the note drops out of the open set and the run continues on the remaining candidates. It is never raised. One malformed note costs one candidate; raising would have let it block the whole project's pick.

## Parse contract — leniency boundary rationale (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

The narrow scope is deliberate. Leniency covers only the shapes a producer's own editing can break. A missing `.flaitron/PLAN.md` is still a user error (exit 2), because that is a registration mistake rather than a malformed file.

## Marker coverage — evaporation-guard rationale (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

- **Marker drift — the evaporation guard (CBN-125).** Coverage is a point-in-time figure with no memory, and the fleet edits PLAN lines for reasons that have nothing to do with markers: a flaitron line rewrite that preserves the visible task text silently drops the trailing `[unattended]` bracket token. `flaitron.marker_drift(tasks, baseline)` diffs the current read against a marked set a past run recorded (§"Marked-set baseline") and returns one `Denial` per row that was armed then and does not dispatch now, in the same dispatch order and from the same `_denial` constructor the other two lists use. The live incident that motivated it: [decisions/flaitron-reader.md](flaitron-reader.md) §"Marker coverage — drift incident (CBN-120.2, CBN-125)".
