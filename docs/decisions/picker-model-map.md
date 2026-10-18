# Decisions — usage adapters, task-class map, picker, compatibility

Measurement, history, and rejected options behind `docs/CONTRACT.md` §"Usage adapters", §"Task-class → model map", §"Picker", §"Out of scope (do not invent here)", and §"Compatibility" (CBN-271.6). The rules themselves live in `CONTRACT.md`; this file records why they are shaped the way they are.

Headings name the `CONTRACT.md` section each entry backs.

## Task-class → model map — xheavy folds to heavy, and the fourth-class deferral is withdrawn (CBN-112, CBN-114)

`xheavy` also maps to `heavy` because flaitron's exploratory rung sits above heavy while this map has only three classes, and `SPEC/model.md` bands no roster model at `xheavy` by default — folding it up is a deliberate under-serve rather than letting it fall to the unknown-token `medium` default, which would route the ladder's top rung below `[heavy]`.

A fourth class is not wanted, and the deferral this paragraph used to carry is withdrawn (CBN-114): it read "file it when a real `[xheavy]` workload appears," but the answer does not depend on one appearing. An orchestrator may never dispatch an `[xheavy]` row at all (§"Flaitron reader", exploratory dimension), so a `TaskClass.XHEAVY` with a `MODELS` row per provider and a new `EFFORT` rung would be routing machinery for a case that must not occur — and the `EFFORT` rung could not even stay flat, since `heavy` already takes `xhigh` and the vendor ladders diverge above it (`claude` and `codex` reach `max`; `grok` stops at `xhigh`). The three-class map is the right shape; the gap the filed task named is closed by refusing the dispatch, not by widening the ladder.

## Task-class → model map — provider-name tokens are tier hints, never a pin

Two aliases — `grok` and `codex` — are the names of providers, not models, and are deliberately conflated with a tier bucket: both to `medium`. The conflation is deliberate. These aliases are copied from Flaitron `SPEC/model.md`'s inherent-tier list, where `[model]` is provider-neutral on purpose, and diverging from the spec caobunga reads would cost more than the limit does (CBN-EPIC-15.1).

## Task-class → model map — pin measurement and probe history

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

Flat rather than per-provider because the ladders verified live on 2026-08-26 diverge only *above* these rungs, and all three flag-taking providers (`claude_code`, `grok_cli`, `codex`) share exactly `low/medium/high/xhigh`; `cursor_cli` has none (encoded in the model id, so the pin table above routes it). The value reaches the CLI through `local.Recipe.effort_argv` (§Worker interface → `local`). Why flat, and why `xhigh` for heavy: [decisions/picker-model-map.md](picker-model-map.md) §"Task-class → model map — effort ladder flat-mapping (CBN-EPIC-15.3)".

## Task-class → model map — effort per provider (CBN-332)

This supersedes both flat-table entries above. `EFFORT` is now keyed by provider, and each row is that provider's column of flaitron `docs/PLATFORMS.md` §"Tier × platform map" (as of 2026-10-08). The shared ladder floor was never the reason to stay flat. It only meant a flat table was *possible*, and CBN-331 showed what that costs: one column cannot fit providers whose model id does or does not change per class.

⚠️ `grok_cli`'s light cell and the kimi/copilot names are superseded by §"Task-class → model map — roster drop and grok light upper end (CBN-360)", below. The claude and codex rows still stand.

- `claude_code` stays `high` / `medium` / `high` (CBN-331). Its model id separates light from heavy.
- `grok_cli` takes `medium` / `high` / `xhigh` (operator decision 2026-10-11). The map's light cell is a range (`medium`–`high`). The operator took the low end over the map's "upper end when in doubt" so the three classes stay distinct on a flat model id, where effort is the only thing separating them.
- `codex` takes `medium` / `high` / `xhigh` (operator decision 2026-10-11). The map's Codex column steps the *model* (Sol → Astra) and keeps the effort mostly at `high`. Caobunga pins Terra flat (Sol/Astra are account-gated), so copying the cells would make all three classes identical. Effort stands in for the missing model step, following the roster equivalence that `sol@xhigh` is roughly heavy-band.

A provider with no row (Cursor, which bakes effort into the id; kimi/copilot/opencode, which take no flag) resolves to `None`. Cursor's heavy pin `grok-4.7-xhigh` now matches the `grok_cli` heavy rung again. Its medium pin was `grok-4.7-medium`, one rung below `grok_cli`'s `high`. CBN-333 re-pinned it to `grok-4.7-high` (2026-10-11, live `cursor-agent models` lists it), so a CBN-213 re-pick between the two no longer shifts a `[medium]` run's rung.

Effort now depends on the picked provider, so `loop` resolves it per pick in `_launch_until_close`, not once in `_launch_posture`. A CBN-213 re-pick to a different provider therefore launches at the new provider's rung. It is still not folded into `pick`. `pick`'s result is a contracted `{provider, model, reason, why}` shape, and nothing else needs the rung.

## Task-class → model map — roster drop and grok light upper end (CBN-360)

Two changes, both on the live tables in `CONTRACT.md`.

`kimi_cli` and `copilot_cli` leave the v1 provider set: the enum, the binary and dump tables, the pin table, and the catalog map. `opencode_cli` stays named and still has no `local.RECIPES` row. A registration of it remains valid and still fails at launch with the supported-set `UserError` (CBN-097). Stored registry `provider` values are free strings, so an old kimi or copilot pin is not rewritten; the next pick rejects it as an unknown provider.

`grok_cli`'s light cell moves from `medium` to `high`, the upper end of flaitron's `medium`–`high` range (`docs/PLATFORMS.md` §"Tier × platform map": on a range, take the upper end when in doubt). `[light]` and `[medium]` on `grok_cli` now dispatch the same argv (`grok-4.7` @ `high`). `[heavy]` stays `xhigh`. This reverses the 2026-10-11 low-end choice in the CBN-332 section, which existed so the three classes stayed distinct on a flat model id.

`codex` stays `medium` / `high` / `xhigh`. Its model id is also flat (Terra), and effort is still the only thing separating its classes. Copying Grok's new light cell would collapse that separation. Cursor's pins are unchanged: medium and heavy already match `grok_cli`'s `high` and `xhigh`.

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
