# Caobunga — VISION.md

Caobunga is a discrete, multi-project orchestrator for Flaitron-using repos. It does not replace Flaitron, nor the agent runners it drives. It is the policy and routing layer that sits above them.

## Problem

Coding agents are useful one task at a time and expensive when a session is reused across tasks. Today the operator must:

- pick the next Flaitron task
- `/clear` or start a new CLI session
- choose a platform and model (Grok Build, Codex, Claude Code, Cursor CLI, later others)
- watch the work
- remember what shipped
- decide whether to continue

That loop does not scale across a fleet of registered repos under `~/code`.

## What it is

A Python/uv CLI plus a later status dashboard that:

1. **Registers** approved repos explicitly (no auto-adoption in v1).
2. **Reads** each repo’s Flaitron plan and open tasknotes.
3. **Routes** the next task to a platform + model using Flaitron’s task-level categories and best-effort leftover usage on available CLIs — no CLI exposes a safe non-interactive usage dump today, so ranking degrades to installed order (declared order under a registry allow-list; see [docs/CONTRACT.md](docs/CONTRACT.md) §Picker).
4. **Executes** through a pluggable worker seam: one fresh isolated session per task (the mechanical `/clear`). One adapter ships — a directly-spawned headless CLI subprocess, selected by `CAOBUNGA_WORKER`; the seam stays a Protocol so another adapter can land without privilege (a `cao` adapter did, and was retired in CBN-204).
5. **Captures** a standardized closure record when Flaitron’s task ends in a commit and a next-task suggestion. Split (CBN-003): the archived tasknote is the narrative (Goal, Acceptance, Final Summary); Caobunga writes a thin orchestrator-owned run record at 🏁 for SHA, outcome, provider/model, next-task, and the path to that note. Do not add a report section to Flaitron — the SHA does not exist until after archive, and next-task is forbidden in the recap. Field names, tiers, and CLI verbs: [docs/CONTRACT.md](docs/CONTRACT.md).
6. **Reports** to two surfaces: terminal and a caller-side last-run card (`$CAOBUNGA_HOME/status/{project}.md` — never committed into the repo it reports on).
7. **Reviews** through the worker's own in-run External review (flaitron `CORE-656`), which caobunga records on the run record rather than re-running (CBN-242 retired the Hermes supervisor that once reviewed each close).
8. **Honors** per-project execution tiers (`observe` → `gated` → `proceed-on-green`).

## What it is not

- Not a second task system. Flaitron remains the SSOT for work.
- Not a long-lived coding chat. Worker context is disposable.
- Not a key vault. Each provider keeps native auth; Caobunga only queries usage and launches already-authenticated CLIs.
- Not an unbounded autonomous coder in v1. Default tier is human-gated; failures escalate to the operator.

## Design principles

- Mechanical control plane. Caobunga’s own process stays thin; review happens inside the worker's session, before its commit.
- One task, one session. Isolation comes from the worker adapter (a one-shot local subprocess), not from compacting a mega-thread.
- File-native. Plans, tasknotes, and reports stay in git-shaped Markdown with structured frontmatter where it helps.
- Explicit fleet. A repo is in scope only after registration.
- Platform-honest routing. Prefer the smallest capable model that still has budget for that Flaitron task class.

## Success

A registered epic can be walked task-by-task with a clean worker each time, a durable report after each commit the operator can trust, and a dashboard that shows where every registered project stands — without the orchestrator saturating its own context window.
