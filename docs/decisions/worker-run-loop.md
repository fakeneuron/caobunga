# Decisions — worker interface and run loop

Measurement, history, and rejected options behind `docs/CONTRACT.md` §"Worker interface" and §"Run loop", including §"Target-tree report" and §"Continue prompt" (CBN-271.5). The rules themselves live in `CONTRACT.md`; this file records why they are shaped the way they are. Reference-only: nothing loads or dispatches on it.

Headings name the `CONTRACT.md` section each entry backs.

## Worker interface — why accept-and-ignore fields

`Worker.launch` was shaped for two adapters that were asymmetric by design: `local` builds a real argv and can pass anything a CLI accepts, while `cao` posted a fixed JSON body to a control plane with no effort parameter and no subprocess to tee. So the Protocol declares every field the loop has, and an adapter that cannot carry one accepts and ignores it rather than raising — the Protocol never forked into two shapes. That rule survives the second adapter: a future adapter inherits the same contract.

## Worker interface — exit code and output tee (CBN-110, CBN-162, CBN-200)

`exit_code` was the recoverable-cause fix for CBN-110's F2: a `STATUS_ERROR` close previously carried `escalation_reason: worker-error` with nothing to distinguish one failure from another. CBN-213 later needed the last stderr/stdout lines to classify a usage-limit refusal on the error path, which is why `wait.wait_for_close` folds `_exit_detail` into `STATUS_ERROR` as well as `WORKER_EXITED` / `TIMEOUT`.

The tee closed CBN-110's remaining gap: `WORKER_EXITED` and `TIMEOUT` carried no code and no line, so a usage-limit refusal, a rejected CLI arg, and a genuine crash were indistinguishable on the run record. On a clean exit stderr is usually empty, so the stdout line (CBN-200) is what actually carries the diagnostic.

The tee was live from the start, but text-mode `claude -p` prints nothing until it exits, so a worker killed mid-task left a 0-byte log (2026-10-18, ~20 minutes of edits, no trail). CBN-377 switched the `claude_code` recipe to `--output-format stream-json --verbose` (`-p` refuses stream-json without `--verbose`) and compacts each event before the tee writes it: one log line per tool call or assistant message, failed tool results kept, successful ones and allowed rate-limit events dropped. Those progress lines are log-only. The stdout tail gets only what text mode printed — the `result` event's text, unprefixed and line by line, or a non-JSON line — so the usage-limit match (CBN-282) and the stdout cause pick (CBN-292, CBN-300) never read a worker's own `grep "usage limit"` as a refusal. Compaction lives in `local.py`, the adapter that owns the stream, as the recipe row's own `compact`; `wait` and `launch` never see JSON. The other recipes stay raw: codex already streams, and grok's and cursor's stream flags are unverified.

## Worker interface — guard hook and effort slot shape

A snapshot install that predates `hooks.py` omits the `--settings` pair rather than failing: emitting it would make the worker `worker-exited` on its first Bash call. Fleet `status`'s `hook: present` / `hook: missing` affix (CBN-216) exists so that miss is visible without a dispatch.

Only the `claude_code` recipe carries a guard because the other three verified CLIs expose no hook surface, and every observed failure of this shape was `claude -p`.

Cursor has no effort fragment on purpose: it encodes effort in the model id, so the pin table routes it and there is nothing for argv to add. Codex is the reason the slot is a tuple rather than a flag name — its effort rides a config override (`-c key=value`), where the value is embedded in the second token rather than standing alone.

## Worker interface — the retired `cao` adapter

CBN-012 shipped the `cao` adapter (`cao.py`), HTTP against a local CAO server (CLI Agent Orchestrator), as the first and then-only worker; CBN-021 built the task-class → model map on it and correctly recorded that effort could not be passed. CBN-EPIC-9.2 added `local`, which builds a real argv and so *could* pass effort; CBN-EPIC-14.5 measured what routing effort was worth and CBN-EPIC-15.2/15.3 wired it through — on `local` only, because `cao`'s control plane had no effort parameter: under `cao` a `[light]` and a `[heavy]` task dispatched identically on the flat-pin providers. CBN-155 made `local` the default for that reason. Every real run in the archive used `local`; no registered project pinned a provider only `cao` could reach. CBN-204 retired the adapter, its `cao` value for `CAOBUNGA_WORKER`, and its pre-launch probe. The provider-token vocabulary (`grok_cli`, `claude_code`, …) is CAO's and stays — it names providers, not the adapter.

The `exited` status exists because `local` is a one-shot CLI; a persistent session — the retired `cao` adapter's shape — could never report it.

## Run loop — filing-artifact message (CBN-138.4)

Since CBN-138.2 the prompt *asks* a worker to file what it discovers, so dirt made of `.flaitron/PLAN.md`, sidequest stubs, and tasknotes is now routinely something Caobunga itself requested — and it read identically to an operator's half-edited source tree. CBN-138.1 F3 named the gap as a refusal whose cause was unnamed, not one that was wrong, so the fix is a message and never an exemption.

The predicate is all-or-nothing because a filing beside an unreviewed edit is not a filing to commit, and because that is what keeps every CBN-EPIC-17.3 assertion true unedited. It is path shape only, never file content, so an unrecoverable (C-quoted) path is never a filing and bias-to-refuse survives into the message.

## Run loop — candidate-tasknote exemption and `-uall`

Flaitron commits an active tasknote only at Phase 4, inside the atomic closure commit, so a stranded `in-progress` note is *always* uncommitted; treating it as foreign dirt would refuse every `CBN-109` resume, which is the one path this gate is load-bearing on.

The `-uall` flag is part of that exemption rather than a verbosity choice: default porcelain collapses a wholly-untracked directory to one `?? dir/` line, which names a directory and can therefore neither be excluded by a path-level pathspec nor matched against an open note's path.

The `caobunga-status.md` exemption and snippet-ignore gate CBN-154 added were retired at CBN-188.3: both existed to protect a `git add` of the last-run card into the target, and since CBN-188.2 no such write happens.

A per-dispatch check is safe inside a drain because the worker's closure commit is atomic and Caobunga itself writes nothing into the target repo.

## Run loop — why the derivation lives in `presence.py` (CBN-130)

The exemption was a `:(exclude,literal)` pathspec on the git call until CBN-130 moved it, and the move is the point: the status report has to render the same verdict the gate refuses on, and two implementations of one safety rule is a report that lies about the gate.

## Run loop — posture degrade, not refusal (CBN-108)

A target pin that does not parse `--unattended` routes it into an operator-less `AskUserQuestion` — the exact banner the flag was adopted to prevent, and strictly worse than the `--fast` it replaced.

`--fast` is the fallback because every pin accepts it and it is the prompt CBN-EPIC-14.3 replaced, so a degraded run is never worse than that baseline. Refusing would turn a posture problem into a hard stop on a repo that would still run, and would abort a drain over one stale submodule. The degrade echo is deliberately not a run-record field, which would be a `schema_version` bump for an operator-facing notice.

Because every pin accepts `--fast`, a degraded run is never worse than the pre-CBN-EPIC-14.3 baseline.

## Run loop — pinned-skill cue, not a skew refusal (CBN-291)

Claude Code resolves `/ft-task` from the personal `~/.claude/skills/` ahead of the target's `.claude/skills/` wire, so a target pinned behind the operator's global flaitron checkout launches a skill whose fragments its pin does not ship. Both of a fleet repo's TEST-004 runs on 2026-10-07 loaded the global copy, which expects `preamble.md`, against a v6.0.0 pin that has none. `20261007T150557122Z` declined: "I would have to guess the status, model and foreign-dirt gate rules". The rerun three minutes later "followed the inline steps in the pinned `SKILL.md` instead" and closed. The outcome turned on the model's reaction, not on resolution, so the prompt now names the pinned file and the fallback. Two alternatives were declined. A preflight refusal on skew would read `~/.claude` (outside §"Fleet boundary") and would stop every claude_code run on any repo pinned behind the global checkout until `/ft-update`. Dropping user setting sources from the recipe would also drop the operator's global hooks and permissions. The global-link fix belongs upstream (flaitron/Fleet HQ).

## Run loop — foreground-only cue incidents (CBN-199)

Three observed `worker-exited` failures (CBN-167, CBN-196, a fleet repo's `CORE-11.3` — all Sonnet under `claude -p`) end the same way: the worker runs its Phase 3 suite with `run_in_background`, then ends its turn to "wait for the notification" — and a headless one-shot session has no next turn, so the process exits 0 with the work done, uncommitted, and the tasknote open. Claude Code exposes no switch for this (only `CLAUDE_AUTO_BACKGROUND_TASKS`, which forces the opposite), so the prompt states the fact instead. This is the launcher's fact to state, not flaitron's: the posture module cannot know how the harness was invoked.

## Run loop — background-call denial (CBN-212)

The fourth occurrence — CBN-210, 2026-09-20, Sonnet, the day after CBN-199 shipped — backgrounded its suite with the sentence in its prompt, so the sentence alone did not hold. The hook was verified live under `-p --permission-mode bypassPermissions` (hooks run in every permission mode; only `--bare` skips them, and no recipe carries it): the call is denied, the model reruns in the foreground.

The guard screens the harness parameter only, not `&` / `nohup` inside a command string: every observed failure used the parameter, and screening shell text would be a grammar this side does not own.

The PLAN line's other route, relaunching a worker that exited 0 with a dirty tree and an open note, was rejected: it is an automatic retry, and §"Non-negotiables" forbids those in v1. CBN-213 is the other shape — a *different* provider, a *clean* tree, a usage/session-limit refusal in the output tail (a last line until CBN-282) — and is the one carve-out that section now names.

## Run loop — park-check cue (CBN-327)

CBN-274's second Codex retry (invocation `20261009T152253989Z`, a fleet repo's CORE-243) ended with a worker that reported a park and wrote `park-reason: input-needed`, but left `status: in-progress`. The loop recorded `failed` / `worker-exited` and did not launch FE-475. That is the right verdict: the metadata disagreed with itself, and a park reason alone is not proof of a park. Before this, only the unattended continue prompt named the park shape; the fresh launches relied on the pinned skill's own instructions. The cue asks the worker to re-read its note before exiting. Three alternatives were declined. Reading `park-reason:` as a park would let a half-written note pass as the worker's verdict. Rewriting the target's tasknote is a repair the supervisor does not make. A degraded twin was unnecessary, because the sentence asks only for `status: blocked`. Like the foreground-only sentence, the cue is best effort: no test can prove a model will follow it, which is why detection stays strict.

## Run loop — park-status mismatch diagnostic (CBN-328)

CBN-327's cue is best effort, so the loop also names the CORE-243 shape when it happens: a `worker-exited` summary gains `park-reason present but status in-progress, not blocked` when the open note carries both. It reads the same `list_open_tasknotes` rows the park branch uses and changes no verdict, because a park reason alone is still not proof of a park. It fires on `worker-exited` only, the escalation CBN-327 pinned; a `timeout` with the same note shape is not named. The clause sits ahead of the tree line because `summary` is clipped at the tail.

## Run loop — follow-up cue history (CBN-138.2)

Before CBN-138.2 the prompt said nothing at all about out-of-scope discoveries, and capture (§"Follow-up capture") depended on the worker spontaneously writing a section nothing had asked for. The cue is appended to the posture rather than folded into it, on the `CONTINUE_UNATTENDED` precedent: composing base + sentence is what keeps four shapes from drifting into four hand-written strings.

## Run loop — close detection reads `HEAD` (CBN-158)

A mid-task commit (e.g. a worker's own `/ft-file-task` filing) moves `HEAD` off the pre-launch SHA before the worker's own close lands, and at that instant the checkout can show the PLAN row already flipped with the archive move still in flight on disk. Reading from `HEAD` means the two writes of an atomic single-commit closure are only ever observed together, so `no-tasknote` is a stable fact, never a mid-move instant.

## Run loop — `next_task` posture (CBN-117, CBN-151)

The posture flag is deliberately **not** `supports_unattended(root)`: the probe is the *target's* capability and governs only the worker message's flag (CBN-108), so sourcing the claim from it would let a stale target pin silently change what Caobunga says about its own queue.

CBN-117 ran the check only under an unattended posture and left the attended path byte-identical as behavior preservation; CBN-151 extended it to the attended path after the header went stale on a completed row — an attended `gated` close would have rendered that id into the last-run card, the same claim one door over.

Without the check, an unattended drain published a `next:` it had refused in the same invocation — one repo → `DEPLOY-13.5`, another → `DATA-13.3`, both `unmarked` (CBN-EPIC-18.4 F4). It is the same honesty class CBN-EPIC-17.3 closed for the dashboard, and resolved the same way: ask what question the field answers, then read it at the posture that answer belongs to. Until CBN-134 the exhaustion-omits-`next_task` consequence had one exception — `plow`'s stem-change exit, where a next-stem candidate honestly existed and was reported even though that invocation would not run it.

## Run loop — drain pin shape (CBN-246, CBN-260)

The pin is not `coverage.marked_tasks`: that is the *eligible* set, and a marked row whose `Blocked by` clears mid-drain is a legitimate continue the drain must keep. It is not the bare marker either (CBN-260): a marked row denied `handoff`, `exploratory` or `parked` at first read used to be pinned, so a worker that stripped the denying token armed it with nobody asked.

## Run loop — usage-limit overlay stays per-invocation (CBN-289)

`CONTRACT.md` §"Run loop" → "Usage-limit re-pick (CBN-213)". Carrying the exhausted overlay across invocations was considered and declined: the cost is one fast-failing launch per invocation, the re-pick succeeded in every sampled run, and a persisted hint would amend the CBN-213 non-negotiable. Revisit if wasted launches become costly or exhaustion shows up on more than one provider/project.

## Run loop — suite timing shift and worker-evidence incidents (CBN-167, CBN-171)

`CONTRACT.md` §"Run loop" → "Deterministic suite (CBN-EPIC-202.3)". The suite rides the single record write, so `closed_at` ("clock at write") lands after the suite and a run's derived duration now includes it: about 2 min on a fleet backend, measured 96 / 3 / 7 s on caobunga. Nothing keys on that shift — filenames still sort, and runs and suite logs still pair.

The worker's own Phase 3 run is not the step's evidence because CBN-167 and CBN-171 were both targeted-green and full-suite-red.

## Run loop — drain exit reasons (CBN-119.4)

This closed `CBN-EPIC-18.N` Finding 5, which carried forward that a drain did not discriminate an **exhaustion exit** (everything eligible was walked) from a **marker exit** (the walk stopped because the next row was unmarked). Both ended the loop with no candidate, no error, and no `next_task` on the last record, so the two were indistinguishable on every surface. Finding 5 deferred the gap to a live trial; CBN-119.4 closed it on the loop's own state instead: at the exit the loop already holds the denial list, and whether it is empty *is* the difference. The set had a `stem-change` reason and a stem-scoped verdict while `plow` existed; both went with it at CBN-134.

## Target-tree report — why classification is the deliverable (CBN-123 F3)

CBN-123 F3 found that seeding `BE-019` in a fleet repo silently turned an untracked `DEPLOY-017.md` into a launch blocker: *which row the operator seeds* and *whether an existing tasknote is foreign dirt* are two decisions that look independent and are not. Naming the coupling is what the surface adds; a bare dirt list would have been the same silence with more text.

The report is computed at the project's own tier because `unattended` and `drain` both move the candidate, and the candidate decides the exemption — a report computed as `gated` would be wrong on every `proceed-on-green` project. `observe` is reported anyway on the same argument coverage uses: blanking an unpromoted tier blanks exactly the rows a promotion wave needs.

## Continue prompt — why it carries the posture (CBN-109)

CBN-EPIC-14.3 read the missing flag as a syntactic problem (no invocation token to attach it to) and left this branch bare; the flag was never the point, the posture was, and prose carries a posture fine. CBN-EPIC-14.5 then reached this branch live on its first failure: it is entered only when a prior session died mid-task and stranded the note, which is precisely when park guidance matters most.

## Continue prompt — `{module}` resolution (CBN-189)

`{module}` was a submodule-path literal until CBN-189, which pointed a core-less flaitron-self worker at nothing.

## Continue prompt — point, don't restate

A Caobunga-side paraphrase of Flaitron's park rules would make a Flaitron release a Caobunga contract change (§"Fleet boundary"), so the unattended variant points at the target's own fragment — which the probe has already proved is on disk whenever this variant is sent. The degraded variant points at nothing, because a pin that does not parse the token ships no fragment either.

## Continue prompt — prose continue over frontmatter conversion

Flaitron's `docs/EXTERNAL-AGENTS.md` step 6 "Two caller paths" declares both contract-legal — path (a), write `status: blocked` + `park-reason: interrupted — …` then invoke normally, and path (b), point a fresh agent at the note with a prose continue — and `SPEC/blocked.md` §"Resuming an interrupted run" describes the same pair. Caobunga's own loop refuses to launch a `blocked` note, so path (a) would stop the run rather than resume it; this layer takes (b).


## Invocation receipts and direct-run compatibility (CBN-272)

A task record answers what happened to one task. It cannot represent an active-lock
or dirty-tree refusal before dispatch, or whether a drain finished its authorized
scope. Returning the last closed record also hides a red suite's stopped sequence.
CBN-272 therefore gives the invocation its own versioned receipt under Caobunga's
home and references the existing task ledger rather than growing its payload or a
second Flaitron task system.

Direct CLI exits remain compatible: 0 after a committed close whose suite stopped
the run, 2 for readiness/configuration errors, 3 when the first read has no candidate,
4 for a failed last task or reporting failure. CBN-EPIC-313.5 later broke that promise
for one case, splitting 5 out of 2 for refusals the machine causes — no installed or pickable provider, an unreadable
`git` — so `fleet` stops folding them into a skip ([cli-fleet-surface.md](cli-fleet-surface.md)
§"Entry points — environment failures are not skips (CBN-EPIC-313.5)"). `execute_invocation.successful` is
the strict aggregation surface. A genuinely empty or unmarked-only queue is empty
authorized scope; an initially marked but denied task is unfinished requested work.
A gated run requests one candidate, so it does not report the whole remaining queue
as its own failure. Unparsed rows are unknown scope; unpinned rows require a new
operator review before the next invocation can treat them as consent.

CBN-301 extends that one-candidate scope to `de-scoped` and `no-tasknote` closes.
A drain stops on them because they do not authorize *continuation*. That reason
does not apply to a gated run, which stops after one task anyway. A green, committed
close of that task is its whole scope, so the receipt is `complete`. Before this,
a fleet repo's gated run with a clean tree and green suite read `successful: false` and
cited an empty queue in its recovery. On every tier, such a close also leaves
`remaining_tasks`: the committed close is task evidence even when the drain stops on it.

The legacy timestamp invocation key stays on task records. A separate random receipt
key avoids collisions for parallel invocations and safely stores unknown requested
slugs without using them in paths. Running checkpoints and per-task references let
an interrupted or crashed run say what it knows without asserting success; crash
reconciliation and fleet cancellation remain later scope. Snapshots are diagnostics,
not replacements for HEAD-based committed-close inspection. Recovery never upgrades
a historical red suite to green or tells an operator to fabricate a closure commit.

CLI worker selection moved into the loop's accounted readiness/start path. This
preserves exit categories while ensuring unavailable adapter errors have receipts;
it also lets observe remain independent of worker configuration. Existing task
callbacks, report failures, narrow usage-limit re-picks and mutex boundaries retain
their meaning. Adapter shutdown return is recorded honestly, without claiming an
independently verified process-tree termination.

## Stale running receipts (CBN-376)

CBN-272 left a receipt that a process death stopped at `running` as unknown, and
CBN-375 made a caught ^C write `interrupted`. An out-of-memory kill or a closed
terminal reaches no handler at all. On 2026-10-18 that left two receipts at
`running`, both at stage `worker-start`. CBN-376 reconciles them on the slug's
next launching `run`.

**Liveness evidence: the run lock plus a recorded pid.** The per-repo run lock is
an `flock` that dies with its process, so a run holding it knows no other caobunga
run is live on that repo. The lock alone is not enough, though: `_execute_run`
writes the `running` checkpoint *before* it tries the lock, so that a refused
`active-run` is durable. A concurrent run sitting between those two steps is
alive, and its receipt looks stale. New receipts therefore carry the orchestrator
`pid`, and a receipt counts as dead only when that pid no longer names a process.
A reused pid reads as alive, so the error goes the safe way. Receipts from before
the field have no pid and fall back on the lock alone. That holds only when the
receipt's `path` is the registered path the lock was taken for, since a slug
re-registered elsewhere is locked under a different key. Two residuals are
accepted. An older install racing a new one in the checkpoint-to-lock window has
no pid, so it can be called dead on the lock. A pid reused by a long-lived
process keeps a dead receipt `running` until that process ends; recording a
process start time beside the pid would close that, and is not done.

**`abandoned`, not `interrupted`.** `interrupted` means the run caught its own
interrupt and wrote the receipt with exit 130. A reconciled receipt is a later
run's verdict, and the dead run's exit code is unknown. So `abandoned` is the one
terminal status with no `legacy_exit_code`, rather than one with an invented
code, and its `finished_at` is when the death was found.

**What landed.** A receipt records no start HEAD (`start_sha` lives on the trace),
so "landed" is `git log --since=<started_at> HEAD` by committer date. The tree and
the open tasknote come from the same read `finish` uses. This is report-only, and
the next readiness check stays the dirty-tree refusal's own fresh read.

**The orphan worker.** A worker is spawned with `start_new_session=True`, so it is
outside the terminal's process group and can outlive a killed orchestrator. The
verdict is about the orchestrator only. The `process-gone` recovery step says to
check for a live worker first, and the dirty-tree refusal still guards the repo
while one writes. Tracking the worker's own pid was considered and left out of scope.

**`observe` reconciles nothing.** It writes no receipt of its own, and it keeps
that write-free posture here too. A stale receipt can only come from a launching
tier, and the next launching run picks it up.

## Run loop — registry guard (CBN-247, CBN-251)

A worker running under `bypassPermissions` could run `caobunga register <path> --tier … --replace`, which rewrites the record with `enabled: true`, or could Write/Edit `$CAOBUNGA_HOME/projects/<id>.yaml` directly. Either way it could retier or re-enable a project without the operator, and `CONTRACT.md` §"Non-negotiables" makes registration the operator's deliberate act.

The hook and `store.default_home()` both anchor a relative `$CAOBUNGA_HOME` to their own process's cwd, which would otherwise diverge between the orchestrator and the worker (the hook runs rooted in the project cwd); CBN-251 closed that gap by having `local._default_spawn` re-export the already-absolute value into the worker's environment rather than letting a relative one pass through unchanged.

The one-command token match is a deliberate exception to "screening shell text is a grammar this side does not own", scoped to a single subcommand.

## Run loop — suite verbs and the `refused` stop (CBN-EPIC-202.3, CBN-EPIC-202.4)

The suite runs the **same verbs** Fleet HQ's `STACK-TENDENCIES.md` §"Command interface" makes every fleet repo answer through its root `justfile`, which is why a `just`-less or `justfile`-less target is a `refused` result rather than a skipped step: the verb contract is the fleet's, and a repo that cannot answer it is a fact the record states.

The suite gate stops on `refused` deliberately: a drain that cannot measure its suite (no `just`, no `justfile`, an unregistered path, a justfile missing the verb's recipe — CBN-211) is the unattended state the gate exists for, and continuing as "not measured" would leave exactly those repos unguarded, which is the bias-to-refuse posture of `CONTRACT.md` §"Execution tiers" applied.

The exit code does not change on a suite stop because the task *did* close, and `4` means worker / orchestrator failure. A shell-level signal is a separate filing if ever wanted (CBN-EPIC-202.1 Follow-ups).

## Run loop — suites serialized across slugs (CBN-308)

Concurrent per-slug runs are the supported fleet form (CBN-124), and two closes landing together ran two suites at once. A fleet repo's `JD-040.4` (2026-10-08) went `red` on three vitest tests hitting their own 5000 ms per-test timeout — `test` 33 s and `lint` 33 s against 3 s / 5 s solo — while a caobunga run and another repo's run overlapped; the two files re-ran green in 2.2 s. Each suite's test pool sizes itself to every core, so two at once oversubscribe the host.

Three options were weighed, and the operator chose the first:

- **Serialize** (chosen) — `Store.suite_lock`, a blocking global `flock` on `suite.lock`, held by `run_suite` across all three verbs. It removes suite-vs-suite contention without touching the record, the fold, or the gate. A third lock file rather than `lock`, because holding the write lock for minutes would stall every registry and run write fleet-wide.
- **Rerun a red verb once** — covers every source of load, but lets a genuinely flaky test pass about half the time. That weakens the deterministic gate CBN-EPIC-202 built to replace a sampled reviewer, and an honest rerun needs a recorded field (the three-place payload rule).
- **Leave as-is with a note** — a false red already fails closed (the drain stops), but every overlap would cost an operator rerun.

**Narrowed to the spawn (CBN-310).** The lock first spanned all three verbs, so a slug queued behind another's suite waited silently, up to the sum of the holders' ceilings (~17 min each, no FIFO fairness), while holding its own run-lock, and an instant `refused` queued the same as a real run. `run_verb` now takes the lock as a `guard` entered after its refusal check and held across the porcelain reads and the spawn, and `Store.suite_lock` calls an `on_wait` before it blocks, which the loop wires to one `suite: waiting for suite.lock` echo line (up to one per verb). Exclusion is unchanged — no two verbs from different slugs ever run at once — but another slug's suite may now start between this slug's verbs, and `flock` has no FIFO fairness, so a slug's total wait is not bounded by one holder's suite. A missing recipe is only known after `just` runs, so that refusal still takes its turn. Capping each suite's test pool through its environment stays unchosen, for the reason in the residual below.

**Residual, accepted:** a worker session running its own tests can still load a concurrent slug's suite. If a load red recurs with no suite overlap in the logs, that is the case that reopens the question — the rerun option, or capping each suite's test pool through its environment, which would cover worker-session load too.

## Run loop — operator-presence gate scope (CBN-EPIC-17.3)

The gate covers every launching tier, `gated` included, because Caobunga always launches a *headless* worker, so an operator waiting at the orchestrator terminal is still nobody inside the target repo.

It is a *safety* boundary, not workflow hygiene — on a fresh launch it is the cheaper, earlier form of a stop flaitron's own foreign-dirt gate would make anyway (`SPEC.md` §"Paper-complete guard"); on the continue path it is the **only** stop that exists, because that prompt tells the worker not to re-invoke `/ft-task` (`CONTRACT.md` §"Continue prompt").

## Run loop — `--unattended` on every tier (CBN-EPIC-14.3)

Every tier sends `--unattended`: Caobunga always launches a *headless* worker, so nobody is inside the worker session even under `gated`, where the operator waits at the orchestrator terminal. `--unattended` is a strict `--fast` superset in flaitron `SPEC/gates.md`, and adds one behavior — the gates `--fast` still lets fire instead **park** the tasknote (`status: blocked` + `park-reason:`) and stop, rather than banner into an empty session. Caobunga's own gated stop remains after the run record. The park round-trips on wiring that predates the flag: a parked note is recorded by `close.inspect_close` as `outcome: blocked` / `escalation: worker-parked` (the next-run refusal to relaunch it was replaced by the `parked` pick denial at CBN-252).

## Target-tree report — filing artifacts and the retired `ignored` state

The report labels an all-filing-artifact dirty block (CBN-138.4) because a label the gate prints and the report omits is the gate/report divergence CBN-130 closed, arriving by a second door. The two surfaces state the same fact in different registers: the refusal offers the commit because it is the surface that stopped; the report only names the condition. The `ignored` state CBN-154 added for a gitignored `caobunga-status.md` was retired at CBN-188.3 with the gate it mirrored.

## Continue prompt — the cues, and the gate exemption

The continue branch needs the foreground-only and follow-up cues most: it is entered only when a prior session died mid-task, so whatever that session was doing — including a stray background wait — is already at risk of being lost.

This branch is also why the operator-presence gate exempts the candidate's own tasknote: a stranded note is uncommitted by construction, so a bare dirt check would refuse the resume outright and the continue prose would never be sent.

## Run loop — why a drain is pinned (CBN-246)

Every later read in a drain is of a PLAN the worker that just closed wrote, and the marker is the only per-row operator consent a launching tier has (`CONTRACT.md` §"Execution tiers", CBN-118) — so without the pin a worker could add `[unattended]` to a row, or file a row already carrying it, and the same drain would dispatch it headless with nobody asked.

The next `run` still pins whatever is marked then, so the `unpinned` detail tells the operator to check who marked the row before re-running: re-running is what turns a worker's mark into consent.

## Run loop — one close, one block (CBN-135)

The CLI tracks the last dumped record by identity because a bare flag hid every close after the first (CBN-EPIC-9.5 F3). The `run updated:` line that once folded a Hermes flush's record rewrite into the close it upgraded left with the flush (CBN-258).

## Run loop — Hermes-era steps retired (CBN-258)

The Hermes brief, launch, and observation window that sat between the record write and the last-run card retired at CBN-258. The suite's `suite_verbs.TIMEOUTS` (600 / 120 / 300 s) are the ceilings the retired Hermes reviewer's `code_suite` server used.
