# Decisions — usage adapters, task-class map, picker, compatibility

Measurement, history, and rejected options behind `docs/CONTRACT.md` §"Usage adapters", §"Task-class → model map", §"Picker", §"Out of scope (do not invent here)", and §"Compatibility" (CBN-271.6). The rules themselves live in `CONTRACT.md`; this file records why they are shaped the way they are.

Headings name the `CONTRACT.md` section each entry backs.

## Task-class → model map — xheavy folds to heavy, and the fourth-class deferral is withdrawn (CBN-112, CBN-114)

`xheavy` also maps to `heavy` because flaitron's exploratory rung sits above heavy while this map has only three classes, and `SPEC/model.md` bands no roster model at `xheavy` by default — folding it up is a deliberate under-serve rather than letting it fall to the unknown-token `medium` default, which would route the ladder's top rung below `[heavy]`.

A fourth class is not wanted, and the deferral this paragraph used to carry is withdrawn (CBN-114): it read "file it when a real `[xheavy]` workload appears," but the answer does not depend on one appearing. An orchestrator may never dispatch an `[xheavy]` row at all (§"Flaitron reader", exploratory dimension), so a `TaskClass.XHEAVY` with a `MODELS` row per provider and a new `EFFORT` rung would be routing machinery for a case that must not occur — and the `EFFORT` rung could not even stay flat, since `heavy` already takes `xhigh` and the vendor ladders diverge above it (`claude` and `codex` reach `max`; `grok` stops at `xhigh`). The three-class map is the right shape; the gap the filed task named is closed by refusing the dispatch, not by widening the ladder.

## Task-class → model map — provider-name tokens are tier hints, never a pin

Two aliases — `grok` and `codex` — are the names of providers, not models, and are deliberately conflated with a tier bucket: both to `medium`. The conflation is deliberate. These aliases are copied from Flaitron `SPEC/model.md`'s inherent-tier list, where `[model]` is provider-neutral on purpose, and diverging from the spec caobunga reads would cost more than the limit does (CBN-EPIC-15.1).

## Task-class → model map — pin measurement and probe history

⚠️ The codex pin below is superseded by §"Task-class → model map — GPT-6.1 Sol pin (CBN-454)". Kept as history.

Pins reflect 2026-08-20 live CLI inventories; `grok_cli` / `cursor_cli` refreshed 2026-09-24 (CBN-254 — `grok models` now defaults to `grok-4.7`, and Cursor's live `cursor-agent models` listing dropped the `cursor-` prefix for the Grok 4.7 family, so the pinned Cursor medium/heavy ids were the live-listed `grok-4.7-medium` / `grok-4.7-xhigh`, not a string-replace of the old `cursor-grok-4.6-*` shape; medium was re-pinned to `grok-4.7-high` by CBN-333, 2026-10-11).

A live inventory is a probe, not a catalog read, because the codex catalog is partial: `codex debug models` hides TAP-gated ids, so absence there proves nothing. The 2026-09-12 refresh (CBN-165) probed each candidate with one trivial `codex exec -m <id> --sandbox read-only` on codex-cli 0.153.4 — `gpt-5.6-sol` and `gpt-6-astra` were both refused (400 `invalid_request_error`, "not supported when using Codex with a ChatGPT account"), `gpt-5.6-terra` was accepted past model validation. Sol and Astra are account-gated and unpinned: the codex row stays flat `gpt-5.6-terra` until this account's Codex reaches them, and Astra is out of scope regardless — it is the `[xheavy]` rung, which an orchestrator never dispatches (§"Flaitron reader", CBN-114). There is no codex counterpart to `CLAUDE_RUNTIME_ALIASES`; a `CODEX_RUNTIME_ALIASES` passthrough was dropped with the pins (operator decision, 2026-09-12), and `gpt-5.6-luna` is deliberately unpinned (operator decision, 2026-09-11).

## Task-class → model map — stale-pin guard degrade posture (CBN-166)

`caobunga.pins.check_all()` is a second, read-only pass over the pin table — it never refuses a dispatch and never re-pins, the same posture `flaitron`'s unparsed/drift reporting holds. CBN-166's stale-pin guard inherits the partial-catalog posture: a codex id absent from the catalog is never reported stale. A catalog command that fails to run or parse degrades to `pins: <provider> catalog unreadable`, mirroring `usage.probe`'s degrade-to-unknown — and so does a full catalog that parses to no ids at all (CBN-256), since a logged-out CLI or a changed output shape would otherwise report every pin stale. Grok keeps its `-m` pin rather than riding the CLI default (CBN-256, operator decision 2026-09-24): the run record's `model` must be what actually ran, and a stale grok pin is now caught here instead. CBN-256 also wired `grok models` and `cursor-agent models`, whose text listings each parse through their own `pins.CATALOG_PARSE` entry, under `pins.CATALOG_TIMEOUT` (30s — `cursor-agent models` takes 11–13s, past `usage.DEFAULT_TIMEOUT`).

## Task-class → model map — the medium-cell measurement (CBN-255)

`[medium]` takes `opus` on `claude_code` (CBN-255, operator decision 2026-09-24). This was a choice, not a fix: the run record did not force it. Sonnet had run all 29 claude_code `[medium]` dispatches. Of those, 19 closed, 6 parked (every park accepted or inconclusive), 1 ended `no-tasknote`, and 3 failed. None of the three failures traced to the model. APP-74 was a correct `visual-confirm` park that was recorded as `worker-exited`. BE-24 was a correct refusal on a dirty tree. CORE-638.3 hit a session limit. BE-24 and CORE-638.3 were both re-dispatched on Sonnet and both closed. The failure rate matched `[light]` on the same model (~10%). The light and medium cells no longer collapse to one id, so `[light]` stays on `sonnet` and `[heavy]` keeps `opus` at `xhigh`. Only the effort rung now separates medium from heavy.

## Task-class → model map — effort ladder flat-mapping (CBN-EPIC-15.3)

⚠️ Superseded by §"Task-class → model map — effort per provider (CBN-332)", below. Kept as history.

The effort map is flat rather than per-provider because the ladders verified live on 2026-08-26 diverge only above these rungs — `claude_code` `low/medium/high/xhigh/max`, `grok_cli` `low/medium/high/xhigh`, `codex` `low/medium/high/xhigh/max/ultra`, `cursor_cli` none (encoded in the model id, so the pin table routes it). All three flag-taking providers share exactly `low/medium/high/xhigh`. Heavy takes `xhigh` to match the Cursor heavy pin (`grok-4.7-xhigh`) — Cursor is the only provider whose effort was routed before this task, so its rungs are the precedent.

## Task-class → model map — effort realigned to the Claude Code column (CBN-331)

⚠️ The flat-table half is superseded by §"Task-class → model map — effort per provider (CBN-332)", below: effort now depends on the picked provider and is resolved per pick, not in `_launch_posture`. The rung values for `claude_code` still stand. Kept as history.

The rungs moved from `low` / `medium` / `xhigh` to `high` / `medium` / `high` (operator decision 2026-10-11) to match flaitron `docs/PLATFORMS.md` §"Tier × platform map" for Claude Code (`sonnet@high`, `opus@medium`, `opus@high`) and the way the operator runs those tiers by hand. The `xhigh` heavy rung no longer has a precedent here: it came from Cursor's `grok-4.7-xhigh` pin, and that pin still routes Cursor on its own because Cursor bakes effort into the id.

The table stayed flat (operator decision, same day) because effort being a pure function of the PLAN token is a contract property: `loop._launch_posture` resolves it without the picked provider. The cost is recorded rather than hidden. On `grok_cli` and `codex`, where the model id is flat too, `[light]` and `[heavy]` now dispatch byte-identically, and both sit below the map's medium and heavy cells. The operator accepted this, and per-provider effort is the filed follow-up (closed by CBN-332, below). CBN-EPIC-15.3's test that light and heavy differ on a flat provider was replaced by one pinning the trade.

Dropping `fable` / `mythos` from `ALIASES`, the Claude passthrough, and the Cursor `claude-fable-5-thinking-*` overrides is a consequence of the frontier dimension (§"Flaitron reader", CBN-331), not a pin refresh. Once those tokens are denied at launch, none of those rows is reachable. The `xheavy` → `heavy` alias stays, because its rationale (CBN-112, above) is a different decision and this task did not revisit it.

### Effort-ladder prose as it stood in `CONTRACT.md` (CBN-298)

Flat rather than per-provider because the ladders verified live on 2026-08-26 diverge only *above* these rungs, and all three flag-taking providers (`claude_code`, `grok_cli`, `codex`) share exactly `low/medium/high/xhigh`; `cursor_cli` has none (encoded in the model id, so the pin table above routes it). The value reaches the CLI through `recipes.Recipe.effort_argv` (§Worker interface → `local`). Why flat, and why `xhigh` for heavy: [decisions/picker-model-map.md](picker-model-map.md) §"Task-class → model map — effort ladder flat-mapping (CBN-EPIC-15.3)".

## Task-class → model map — effort per provider (CBN-332)

This supersedes both flat-table entries above. `EFFORT` is now keyed by provider, and each row is that provider's column of flaitron `docs/PLATFORMS.md` §"Tier × platform map" (as of 2026-10-08). The shared ladder floor was never the reason to stay flat. It only meant a flat table was *possible*, and CBN-331 showed what that costs: one column cannot fit providers whose model id does or does not change per class.

⚠️ `grok_cli`'s light cell and the kimi/copilot names are superseded by §"Task-class → model map — roster drop and grok light upper end (CBN-360)", below. The claude row still stands. The codex row, and Grok's light cell again, are superseded by §"Task-class → model map — v7.4.0 effort retarget (CBN-416)".

- `claude_code` stays `high` / `medium` / `high` (CBN-331). Its model id separates light from heavy.
- `grok_cli` takes `medium` / `high` / `xhigh` (operator decision 2026-10-11). The map's light cell is a range (`medium`–`high`). The operator took the low end over the map's "upper end when in doubt" so the three classes stay distinct on a flat model id, where effort is the only thing separating them.
- `codex` takes `medium` / `high` / `xhigh` (operator decision 2026-10-11). The map's Codex column steps the *model* (Sol → Astra) and keeps the effort mostly at `high`. Caobunga pins Terra flat (Sol/Astra are account-gated), so copying the cells would make all three classes identical. Effort stands in for the missing model step, following the roster equivalence that `sol@xhigh` is roughly heavy-band.

A provider with no row (Cursor, which bakes effort into the id; kimi/copilot/opencode, which take no flag) resolves to `None`. Cursor's heavy pin `grok-4.7-xhigh` now matches the `grok_cli` heavy rung again. Its medium pin was `grok-4.7-medium`, one rung below `grok_cli`'s `high`. CBN-333 re-pinned it to `grok-4.7-high` (2026-10-11, live `cursor-agent models` lists it), so a CBN-213 re-pick between the two no longer shifts a `[medium]` run's rung.

Effort now depends on the picked provider, so `loop` resolves it per pick in `_launch_until_close`, not once in `_launch_posture`. A CBN-213 re-pick to a different provider therefore launches at the new provider's rung. It is still not folded into `pick`. `pick`'s result is a contracted `{provider, model, reason, why}` shape, and nothing else needs the rung.

## Task-class → model map — roster drop and grok light upper end (CBN-360)

Two changes, both on the live tables in `CONTRACT.md`.

⚠️ The effort claims in this section are superseded by §"Task-class → model map — v7.4.0 effort retarget (CBN-416)". The kimi/copilot roster drop stands.

`kimi_cli` and `copilot_cli` leave the v1 provider set: the enum, the binary and dump tables, the pin table, and the catalog map. `opencode_cli` stays named and still has no `recipes.RECIPES` row. A registration of it remains valid and still fails at launch with the supported-set `UserError` (CBN-097). Stored registry `provider` values are free strings, so an old kimi or copilot pin is not rewritten; the next pick rejects it as an unknown provider.

`grok_cli`'s light cell moves from `medium` to `high`, the upper end of flaitron's `medium`–`high` range (`docs/PLATFORMS.md` §"Tier × platform map": on a range, take the upper end when in doubt). `[light]` and `[medium]` on `grok_cli` now dispatch the same argv (`grok-4.7` @ `high`). `[heavy]` stays `xhigh`. This reverses the 2026-10-11 low-end choice in the CBN-332 section, which existed so the three classes stayed distinct on a flat model id.

`codex` stays `medium` / `high` / `xhigh`. Its model id is also flat (Terra), and effort is still the only thing separating its classes. Copying Grok's new light cell would collapse that separation. Cursor's pins are unchanged: medium and heavy already match `grok_cli`'s `high` and `xhigh`.

## Task-class → model map — v7.4.0 effort retarget (CBN-416)

Flaitron v7.4.0 (`docs/PLATFORMS.md` §"Tier × platform map", 2026-10-20) names one cell per tier. Grok `[light]` is `Grok @ medium`, `[moderate]` is `Grok @ high`, and `[heavy]` is `Grok @ xhigh`. Codex `[light]` and `[moderate]` are both `Sol @ medium`, and `[heavy]` is `Sol @ high`.

⚠️ The Terra pin and the no-probe rule in the next paragraph are superseded by §"Task-class → model map — GPT-6.1 Sol pin (CBN-454)". The effort cells stand.

Caobunga copies those effort cells and does not copy the Sol model id. Sol is not on this account's free tier. CBN-165 already refused `gpt-5.6-sol` on a ChatGPT-account Codex, and the PLAN line forbids a new probe and a Sol relaunch. The pin stays `gpt-5.6-terra`. Effort on that flat id becomes `medium` / `medium` / `high`, so light and medium share a rung. Heavy drops from `xhigh` to `high`.

Grok's light cell moves from CBN-360's `high` back to `medium`. Medium stays `high` and heavy stays `xhigh`, so the three Grok classes are distinct again. Claude is unchanged (`sonnet@high` / `opus@medium` / `opus@high`). Cursor's medium and heavy ids already encode `high` and `xhigh`, which still match Grok's medium and heavy. No Cursor edit.

`[moderate]` is an alias of `TaskClass.MEDIUM` beside legacy `[medium]`. The three-class ladder stays. Unmarked triage admits both tokens, matching candidacy clause 1. A marked `[moderate]` row already classified as medium via the unknown-token default; the alias makes that bucket intentional, and the exhaustive `set(ALIASES)` test pins it.

## Task-class → model map — runner cap (CBN-448)

Flaitron v8.2.0's `docs/EXTERNAL-AGENTS.md` step 11 makes the runner cap the caller's: flaitron's model gate compares a row's tag with the session it finds and never limits what a caller launches. An operator-less caller runs the families whose ceiling is the heavy band (Opus, Sol, Grok) at any effort up to `xhigh`, and never a frontier model (Fable, Mythos, Astra) at any effort, because the exclusion is by name.

Caobunga already held most of it. The CBN-331 frontier dimension denies frontier PLAN tokens at launch, and every `MODELS` / `EFFORT` cell was heavy-ceiling at ≤ `xhigh`. CBN-448 closes the two gaps. `astra` joins `grammar.FRONTIER_TOKENS`, so an `[astra]` row is denied like `[fable]`. A registry `model:` pin, which `picker._override` used to pass to argv verbatim, is refused at pick when its id names a frontier family. The match is a case-folded word of the id (split on anything not alphanumeric), so `claude-fable-5-1` and `gpt-6-astra` both trip it while `affable-1` does not. The refusal is a plain `ValueError`, which the loop already maps to exit 2 with no record and no launch. It runs ahead of the not-installed check, so a frontier pin on an absent provider reads as the registration error it is, not machine state (exit 5). It is pick-time only, so a pin registered earlier is caught too, and `register` needs no second check. `taskmap.FRONTIER_FAMILIES` derives from `grammar.FRONTIER_TOKENS`, so a new frontier name is one edit. A `test_taskmap` guard pins every `EFFORT` cell and Cursor-baked rung to ≤ `xhigh`.

## Task-class → model map — GPT-6.1 Sol pin (CBN-454)

The operator lifted CBN-416's no-probe rule on 2026-11-01 for exactly one probe. Flaitron v8.2.0's Codex column (`docs/PLATFORMS.md` §"Tier × platform map") now names GPT-6.1 Sol, and Terra is kept only during the GPT-6 rollout. `codex debug models` on codex-cli 0.166.0 lists `gpt-6.1-sol`. The catalog is partial and listed the refused `gpt-5.6-sol` too, so that proved nothing; the probe decided. One `codex exec -m gpt-6.1-sol --sandbox read-only` with a one-word prompt was accepted on this ChatGPT-account Codex (exit 0, header `model: gpt-6.1-sol`, reply `ok`).

All three codex `MODELS` cells pin `gpt-6.1-sol`. The map names Sol for every tier, so the row stays flat. The effort cells are unchanged (`medium` / `medium` / `high`, CBN-416). They were already the map's Sol column, so light and medium still share one dispatch. Sol is a heavy-ceiling family, so the CBN-448 runner cap still holds. `gpt-6-astra` stays unpinned and out of scope (frontier, CBN-448). Luna and Terra stay unpinned.

## Picker — pin/allow-list merge incident (CBN-EPIC-95.2, CBN-EPIC-95.3, CBN-EPIC-95.4)

Pinned `provider` and the `providers` allow-list are mutually exclusive on the stored record, not merely per invocation: `register --providers` clears any stored `provider` and `model` pin, and `register --provider` clears any stored `providers` allow-list. Pins move as a unit, so `--provider` without `--model` drops a stale model pin rather than carrying it onto a different provider. Before that fix the merge kept both fields and the pin silently made the allow-list inert — visible only as `reason: project-override`, which is not persisted (CBN-EPIC-95.2 F0, CBN-EPIC-95.3).

## Picker — usage-limit re-pick overlay (CBN-213)

CBN-213 overlays `leftover=exhausted` onto a provider after a clean-tree, HEAD-unchanged usage/session-limit refusal in the same invocation, then re-calls the picker policy. The overlay is the loop's, not the dump's: `usage.DUMP_ARGV` stays `None` for every worker, and nothing is persisted. A hard pin still wins even when the overlay would have skipped that provider, which is why the loop does not re-enter `pick` on a pin.

A clean tree alone is not "left nothing" (CBN-260): a worker that committed and then hit its limit leaves porcelain empty, so the gate also requires HEAD unchanged from `start_sha`. The match reads a bounded stderr/stdout tail, not the last line (CBN-282): Grok's 402 "usage balance exhausted" is multi-line JSON whose last line is a bare `}`, which escalated a fleet repo's FE-104 as `worker-error` twice on 2026-10-06 instead of re-picking. The tail stays in memory (`local.TAIL_LINES`) rather than reading the log tee back, keeping the worker seam's facts-not-transcripts shape; `payment required` joined the phrase list because a 402 is billing, which no relaunch on that provider fixes.

## Compatibility — v1 → v2 bump rationale (CBN-EPIC-202.2, CBN-EPIC-202.3)

What v2 asserts that v1 could not: on a closure outcome, `suite` present; on `blocked` / `failed`, absent. v1 records load unchanged: the model keys the required direction on `schema_version`, the forbidden direction holds at every version, and a v1 record's missing `suite` reads as *unknown*, never `green`. The loop wrote `schema_version: 1` until CBN-EPIC-202.3 attached the field — a record's version is the shape it actually carries, never a declaration ahead of it — and writes v2 since.

## Compatibility — Hermes retire, no bump (CBN-258)

The four `hermes_*` fields stay optional on the model and are no longer written. That is a write-side change only — a v2 reader asserts nothing about them — so no bump. Old records load and render unchanged; `briefs/` and `reviews/` under an old home are inert history.

## Compatibility — stray `caobunga-status.md` history (CBN-188.2, CBN-188.3, CBN-154)

A `caobunga-status.md` left in a target repo from before CBN-188.2 moved the last-run card caller-side is not Caobunga's to clean (§"Fleet boundary" — the card is caller-side, not a sanctioned producer write). The CBN-154 gate and dirt exemption that once shielded it were retired at CBN-188.3: the leftover file is now ordinary blocking dirt, the operator's own `git rm`.

## Task-class → model map — provider-name alias rationale (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

**A provider-name token is a tier hint and never a provider pin.** Two aliases —
`grok` and `codex` — are the names of *providers*, not models, and they are
bucketed like any other token: both to `medium`. The two consequences are stated in CONTRACT §"Task-class → model map".

## Task-class → model map — flat pins and alias rationale (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

Grok has no lighter SKU (`grok-4.5` is older, not lighter), so the flat Grok/Codex rows pick no distinct model id per class. The task class is **not** discarded for them — the CONTRACT effort map (§"Task-class → model map") routes it (CBN-EPIC-15.3). Claude's advertised aliases are `sonnet` / `opus` / `fable` — coding floor is `sonnet` even for `[light]`. `[medium]` takes `opus` on `claude_code` (CBN-255); only the effort rung separates it from `[heavy]`. When the PLAN `[model]` token is itself a Claude runtime alias (`sonnet`, `opus`), `resolve` passes it through on `claude_code`. `fable` is advertised but never dispatched (CBN-331 caps caobunga at `[heavy]`), so it has no passthrough and no `cursor_cli` override. Cursor light uses Composer; medium/heavy stay on the Grok 4.7 family with effort in the id. The dispatch measurement behind the medium-cell choice: [decisions/picker-model-map.md](picker-model-map.md) §"Task-class → model map — the medium-cell measurement (CBN-255)".

## Task-class → model map — effort-cell provenance (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

⚠️ The Grok light cell and the Codex effort row in this extract are historical. Current cells: §"Task-class → model map — v7.4.0 effort retarget (CBN-416)".

Each column follows that provider's column of flaitron `docs/PLATFORMS.md` §"Tier × platform map" (2026-10-08). `claude_code` is `sonnet@high`, `opus@medium`, `opus@high` (CBN-331): light runs a higher rung than medium on purpose, because it is the smaller model. `grok_cli` and `codex` have flat model ids. Grok's light cell takes the upper end of the map's `medium`–`high` range (CBN-360), so `[light]` and `[medium]` share `high` and only `[heavy]` (`xhigh`) differs. `codex` stays `medium` / `high` / `xhigh`: Terra is pinned flat where the map steps from Sol to Astra, and effort is still what separates its classes (operator decision 2026-10-11, kept by CBN-360). `cursor_cli` and `opencode_cli` have no row and resolve to `None`. Cursor encodes effort in the model id, so the CONTRACT pin table (§"Task-class → model map") routes it, and its heavy `grok-4.7-xhigh` matches `grok_cli`'s heavy rung. Its medium `grok-4.7-high` matches `grok_cli`'s `high` (CBN-333), so a re-pick between the two keeps a `[medium]` run's rung. Every cell sits inside its vendor's live ladder (verified 2026-08-26). The value reaches the CLI through `recipes.Recipe.effort_argv` (§Worker interface → `local`). Why per-provider, and the operator choices: [decisions/picker-model-map.md](picker-model-map.md) §"Task-class → model map — effort per provider (CBN-332)" and §"Task-class → model map — roster drop and grok light upper end (CBN-360)".

## Task-class → model map — inventory verification rationale (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

Pins reflect live CLI inventories (2026-08-20, refreshed for `grok_cli` / `cursor_cli` (CBN-254; Cursor medium re-pinned CBN-333, 2026-10-11) and `codex` (CBN-165)); a live inventory is a probe, not a catalog read, because the codex catalog is **partial**. The codex row stays flat `gpt-5.6-terra`; `gpt-5.6-luna` and the account-gated ids are deliberately unpinned. Account-gating, unpinned ids, and probe history: [decisions/picker-model-map.md](picker-model-map.md) §"Task-class → model map — pin measurement and probe history".

## Task-class → model map — catalog comparison rationale (CBN-411)

> Extracted from CONTRACT at CBN-411. This passage preserves the explanation and its original rule context; CONTRACT is authoritative for current rules. Unlinked section references in this excerpt refer to CONTRACT.

**Stale-pin guard (CBN-166).** `caobunga.pins.check_all()` is a second, read-only pass over this same table — it never refuses a dispatch and never re-pins. For each *installed* v1 provider, `caobunga status <project>` reports one `pins:` block (the fleet table gains no column), and since CBN-256 the `caobunga digest` CLI verb ends with the same lines under `## Model pins`: a provider with no non-interactive catalog command reports `pins: <provider> catalog unavailable` (today: claude, opencode); a provider whose catalog is declared partial (`pins.CATALOG_PARTIAL` — codex) reports `pins: <provider> catalog partial (TAP-gated ids hidden)` and never compares its pins against the catalog; any other installed provider with a readable catalog warns `pins: <provider> <id> not in catalog` per pinned id the catalog does not list. A catalog command that fails to run or parse, or that parses to no ids at all, degrades to `pins: <provider> catalog unreadable`. A clean provider prints nothing beyond the summary line. An uninstalled provider is not reported at all. Degrade-posture rationale and the CBN-256 grok/cursor wiring: [decisions/picker-model-map.md](picker-model-map.md) §"Task-class → model map — stale-pin guard degrade posture (CBN-166)".

## Out of scope — implementation ownership history (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

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
| GitHub PR comment (not picked; snippet retired CBN-188.2) | CBN-033 |
| Status API | CBN-040 (landed) |
| Status UI | CBN-041 (landed) |
| Later tiers actually running | CBN-050 (landed) |
| Hermes observation window (N or T) | CBN-051 (landed; retired CBN-258) |
| Additional worker providers + Fable routing | CBN-052 (landed; Fable denied, CBN-331) |
| Registry provider allow-list (`--providers`) | CBN-091 (landed) |
| Fleet batch verb (`fleet`) | CBN-312 (landed; reopened CBN-124's decline — §"Entry points") |

## Task-class → model map — provider-token examples (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

**A provider-name token is a tier hint and never a provider pin.** `grok` and `codex` both classify as `medium`; provider choice does not read `plan_model` as a preference. Background: [decisions/picker-model-map.md](picker-model-map.md) §"Task-class → model map — provider-name alias rationale (CBN-411)".

- `[grok]` does **not** route the task to `grok_cli`. Provider choice belongs to
  §Picker (leftover, registry pins, allow-list) and never reads `plan_model` as a
  preference. A `[grok]` task on a Claude-only allow-list runs on `claude_code`
  at medium.
- `[grok]` cannot express *"a heavy run on Grok."* The token carries one axis —
  how hard the task is — so the only way to ask for heavy is `[heavy]`, which
  then reaches Grok's `xhigh` rung through the effort map below.

To pin a provider, register one — `register --provider` / `--providers`, not the PLAN token. Why the conflation is deliberate: [decisions/picker-model-map.md](picker-model-map.md) §"Task-class → model map — provider-name tokens are tier hints, never a pin".

## Picker — unknown-leftover consequence (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

Today leftover dumps are all `unknown`, so ranking collapses to the order tiebreak: without an allow-list that is the first installed v1 worker, and with one it is the operator's first declared worker. When a leftover parser is eventually wired to a stable command, budget spreading takes over inside the allow-list with no further change.

## Task-class → model map — effort-map provenance (CBN-460)

Context relocated from CONTRACT; current rules remain there. Unlinked section references and positional wording refer to the original CONTRACT context.

Effort follows flaitron's v7.4.0 tier/platform map (2026-10-20): Claude light/medium/heavy is `sonnet@high` / `opus@medium` / `opus@high` (CBN-331); Grok is `medium` / `high` / `xhigh` (CBN-416). Codex runs flat `gpt-6.1-sol` (CBN-454) at the map's Sol effort: `medium` / `medium` / `high` (CBN-416). Cursor and OpenCode have no effort row and return `None`; Cursor's medium/heavy model ids encode `high` / `xhigh`, matching Grok's medium and heavy (CBN-333). Cells are within vendor ladders (verified 2026-08-26); values reach argv through `Recipe.effort_argv`. Background: [decisions/picker-model-map.md](picker-model-map.md) §"Task-class → model map — v7.4.0 effort retarget (CBN-416)".
