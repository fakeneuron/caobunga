## Workflow

Planning and task tracking (flaitron) live in the private repo this snapshot is exported from; they are not shipped here.

## What this is

**Caobunga** is a multi-project Flaitron orchestrator. It registers approved repos, routes each task to a coding CLI by best-effort leftover usage (today degrades to installed order — no CLI exposes a safe non-interactive usage dump), runs it in a fresh worker session, and reports what committed. It does not replace Flaitron, nor the agent runners it drives.

See `VISION.md`.

## Non-negotiables

- Explicit registration only. No auto-scan of `~/code`.
- One task, one fresh worker session. Worker transcripts are disposable and never stored on a report surface. A usage/session-limit refusal that left a clean tree at an unmoved HEAD is a picker signal, not a session that ran the task (CBN-213); the eventual launch is still one worker session per close.
- v1 dashboard is read-only (no start / stop / approve).
- v1 failure policy: escalate to the operator. No automatic retries. Carve-out (CBN-213): on a clean-tree, HEAD-unchanged (CBN-260) `worker-error` / `worker-exited` whose stdout/stderr tail (CBN-282) carries a usage/session-limit refusal, treat that provider as `leftover=exhausted` **for this invocation**, re-pick the next allow-list member, and only then escalate. A hard `register --provider` pin does not pivot. Same-worker relaunch on a dirty tree stays forbidden (CBN-212). Leftover-dump wiring is left alone.
- First-ship tiers that must work: `observe` and `gated`. `proceed-on-green` runs as of CBN-050; `plow` ran from CBN-050 to CBN-134, which retired it (§"Execution tiers").
- Flaitron stays the work SSOT. Caobunga does not grow a second task system.

Dispatch-safety mechanics — the `[unattended]` marker requirement, the dirty-tree refusal, the `[xheavy]` and frontier-band (`[frontier]` / `fable` / `mythos`) denials that cap dispatch at `[heavy]`, headless-launch degrade, and the `drain:` exit vocabulary — are canonical in `docs/CONTRACT.md` §"Execution tiers" / §"Run loop" / §"Flaitron reader"; see `git log -- CLAUDE.md` for the trial history behind them.
- `justfile`'s backend `lint` recipe deliberately diverges from Fleet HQ's `templates/justfile` by one line — `&& uv run ruff format --check .` (CBN-095). Fleet HQ's layer-refresh skill classes `justfile` as base-stable and offers to re-copy it; **decline its per-file confirm for `justfile`**, or the format-check gate silently disappears — along with the project-only `public-export` / `public-export-scan` recipes (CBN-367) that the open-source flip-time checklist depends on. The inline comment at the diverging line (CBN-EPIC-13.4) carries the same instruction for a reviewer reading the diff directly.
- `.gitleaks.toml`, `.pre-commit-config.yaml`, and `.editorconfig` carry genericized comments that diverge from Fleet HQ's deposits (CBN-366) until Fleet HQ adopts the same wording (NAT-425). Until then, **decline the layer-refresh per-file confirm for all three**, or the private repo names come back.

## Stack

| Layer | Choice |
|-------|--------|
| Backend | Python / uv CLI (`--with-uv-backend`; click + rich + watchdog) + FastAPI status API on **8013** *(strict bind in `backend/main.py`)* |
| Frontend | Vite + React + TypeScript status UI (`--with-frontend`; router + react-query + table). Dev server **5195** *(strict bind in `frontend/vite.config.ts`; proxies `/api` → backend **8013**)*
| Execution | Already-authenticated provider CLIs. `CAOBUNGA_WORKER=local` (CBN-155 / CBN-EPIC-9.2) is the only adapter; the `cao` adapter was retired in CBN-204 |
| Quality gates | Fleet quality stack per Fleet HQ's `STACK-TENDENCIES.md` (CBN-170): ruff on the ratcheted baseline — the `# noqa` fence from `ruff check --add-noqa` is a **fence, not an endorsement**; burn it down as touched, never strip it wholesale — · pyright `standard` via `uv run pyright` (never `uvx`; outside the venv every `caobunga.*` import reads as missing) · eslint `recommendedTypeChecked` + `jsx-a11y` with `projectService`, so **a new TS file outside `src/`, `tests/`, or the config files must be added to a `tsconfig.*.json` `include`** or lint fails to parse it · `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes` · **a field added to the `/api/projects/{id}` payload must land in three places** — the pydantic model, `fixtures/api-payload-keys.json`, and `frontend/src/api/types.ts` — or the seam guard fails on whichever stack runs next (CBN-EPIC-231.3; `docs/CONTRACT.md` §"Status API") · changed-line coverage ≥ 80 % (`diff-cover`, per stack; `just coverage` mirrors CI — the pin lives in both `justfile` `DC=` and `ci.yml` `DIFF_COVER_VERSION`, bump together) · Dependabot security-only (inert until the repo's "Dependabot security updates" setting is on) |

No separate Cloudflare Worker dir in v1. Status API: `GET /api/projects`, `GET /api/projects/{id}`, `GET /api/digest`, `GET /api/stats` — read-only.

## Task prefix

Use **`CBN-`** (`CBN-001`, …).

## Entry points

Caobunga has two entry points (`docs/CONTRACT.md` §"Entry points"): the registry
CLI (`caobunga run <slug>`, from anywhere) and `/caobunga`, the in-repo skill
that resolves the current repo to its registered slug and runs it. The skill is the only entry shipped in `.claude/skills/`.

`/caobunga` **refuses on an unregistered repo** and prints the `register`
command rather than running it. That is the "explicit registration only"
non-negotiable above, applied: reading the registry is fine, writing it is the
operator's deliberate act.
