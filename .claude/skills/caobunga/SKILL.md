---
name: caobunga
description: Run caobunga against the repo you are currently sitting in — the in-repo entry point, as opposed to `caobunga run <slug>` which addresses a project by registry slug from anywhere. Use when the user asks to run caobunga here, run the next task in this repo, or check what caobunga would do in this project. Resolves the current git repo to its registered slug, shows the tier it will honor, confirms before launching, and shells out to the CLI. Refuses on an unregistered repo — it never registers anything itself.
---

# caobunga — in-repo entry point

Caobunga has **two entry points** (`docs/CONTRACT.md` §"Entry points"). This
skill is the second one:

| Entry point | Addressed by | Invoked from |
|---|---|---|
| Registry CLI | a registry slug — `caobunga run webapp` | anywhere |
| **This skill** | the current working directory | inside the repo |

Both run **one project, one task at a time** and share the same run loop. They
differ only in how the project is named. This skill re-implements nothing: it
resolves a slug and shells out to `caobunga run <slug>`.

## Non-negotiables this skill must not bend

- **Explicit registration only.** Reading the registry is fine; writing it is
  not. If the repo is unregistered, **refuse** — print the `register` command
  and stop. Never run `caobunga register` yourself, and never offer to as a
  side effect of a run request.
- **No auto-scan of `~/code`.** Resolution looks at the registry and the
  current directory, nothing else.
- **Not a second orchestrator.** Every decision about which task, which
  provider, and when to stop belongs to `caobunga run`. This skill picks no
  task and writes no run record.

## Step 0 — Locate the caobunga CLI

Prefer `caobunga` on `PATH`. If `command -v caobunga` finds nothing, fall back
to running it out of the caobunga repo, which is three levels above this
skill's own base directory (resolving symlinks — in an adopting repo this
skill is a symlink back into the caobunga checkout):

```sh
CAO_REPO=$(cd -P "<this skill's base directory>" && cd ../../.. && pwd -P)
uv run --project "$CAO_REPO/backend" caobunga <args>
```

**`cd -P` first, then `..` — the order matters.** In an adopting repo this
directory is a symlink, and `cd <base>/../../..` normalizes `..` lexically
before following it, landing you back in the *adopting* repo instead of the
caobunga checkout. `cd -P` resolves the symlink physically first, so the
subsequent `..` walks the real tree. The same command is correct from the real
directory inside caobunga itself.

Use whichever form works for every invocation below. If neither resolves, stop
and say so — do not guess at a third path.

## Step 1 — Resolve the current repo to a registered slug

The registry lives at `$CAOBUNGA_HOME/projects/{id}.yaml` (default
`~/.caobunga/`), one record per file, `id` being the filename stem — the layout
published in `docs/CONTRACT.md` §"On-disk layout". Read it; do not write it.

```sh
CAOBUNGA_HOME="${CAOBUNGA_HOME:-$HOME/.caobunga}"
root=$(git rev-parse --show-toplevel) || exit 1   # not a git repo -> stop
root=$(cd "$root" && pwd -P)
slug=""
for f in "$CAOBUNGA_HOME"/projects/*.yaml; do
  [ -e "$f" ] || break
  p=$(sed -n 's/^path: *//p' "$f" | head -1 | sed "s/^['\"]//;s/['\"]$//")
  if [ "$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$root" | tr '[:upper:]' '[:lower:]')" ]; then
    slug=$(basename "$f" .yaml); break
  fi
done
```

Three details that are load-bearing, not incidental:

- **Compare case-insensitively.** macOS is case-insensitive but
  case-*preserving*, and `Path.resolve()` does not normalize case — a repo
  registered as `~/code/foo` and entered as `~/Code/foo` is the same repo and
  must match.
- **`pwd -P` / `git rev-parse --show-toplevel`.** `register` resolves symlinks
  before storing the path, so resolve here too.
- **Do not assume slug == directory basename.** `register <path> --id charts`
  is legal (`docs/CONTRACT.md` §"Register identity"), so the slug must come
  from the matched file's name, never from the directory.

If `git rev-parse` fails, the cwd is not a git repo. Say so and stop.

## Step 2 — Unregistered repo: refuse, and say exactly how to fix it

If `slug` is empty, **stop here**. Report that the repo is not registered and
print the exact command the operator can run themselves:

```text
<repo-name> is not registered with caobunga, so there is nothing to run.

Register it explicitly (default tier is `gated` — one task, then it stops
for you):

    caobunga register <resolved-root> --tier gated

Then invoke /caobunga again.
```

Substitute the real resolved root. Do **not** run that command. Do **not** ask
whether to run it. Registration is the operator's deliberate act; making it a
side effect of asking for a run is exactly the slope the non-negotiable guards.

## Step 3 — Registered repo: show the tier, then confirm

Read the matched record's `tier` and `enabled`. If `enabled` is `false`, say so
and stop — `caobunga run` will refuse anyway, and failing here is cheaper.

State what the tier will do before doing it:

| Tier | What `caobunga run` will do |
|---|---|
| `observe` | Read the plan and last run record. Launches no worker. |
| `gated` | Run **one** next task in a fresh worker session, write a run record, publish the last-run card, and stop. |
| `proceed-on-green` | Keep running tasks while outcomes are `closed` and each close's suite is `green`. Stops on any non-`closed` outcome (`failed` / `blocked` / `de-scoped` / `no-tasknote`), and on a red / timed-out / refused suite. |

For `observe`, go straight ahead — it launches nothing.

For every other tier, **ask the operator to confirm before launching**, naming
the slug and the tier. This is the cautious half of why the in-repo entry point
exists: `caobunga run <slug>` from the shell is a deliberate act of typing a
slug, while `/caobunga` can be invoked by reflex in whatever repo happens to be
open. The confirmation restores the deliberateness. Do not skip it, and do not
substitute a summary for it.

## Step 4 — Run, and report what the CLI reported

This bare command runs through the local-subprocess worker adapter
(`docs/CONTRACT.md` §"Worker interface") — `CAOBUNGA_WORKER=local`, the default
and, since CBN-204, the only value:

```sh
caobunga run "$slug"
```

Surface the run record the CLI prints (`exclude_none` YAML) and the exit code.
Do not re-narrate the worker session, and do not go looking for its transcript
— `docs/CONTRACT.md` §"Surfaces" keeps transcripts off every report surface.

Exit codes worth naming back to the operator (`docs/CONTRACT.md` §"Exit codes"):

| Code | Meaning |
|---|---|
| `0` | Success — including `observe`, and `gated` after a run record is written |
| `2` | Usage / validation |
| `3` | No next task — the PLAN is exhausted or nothing is eligible |
| `4` | Worker or orchestrator failure — **escalate to the operator**; do not retry |
| `5` | Unlaunchable — no installed or pickable provider, or `git` hung or is missing; fix this machine, not the repo |

`4` escalates. v1 has no automatic retries, and adding one here would be
inventing policy the contract does not grant this skill. This is unchanged by
CBN-213: on a clean-tree, HEAD-unchanged usage/session-limit refusal, `caobunga run` may
re-pick the next allow-list provider and launch it *before* returning a
result — that pivot is the CLI's own in-invocation picker overlay, not a
retry of the same worker, and it happens entirely inside the `caobunga run`
call this skill already treats as opaque. Whatever exit code and run record
that call finally returns, this skill still never launches a second attempt
of its own.

## What this skill does not do

- Register, unregister, enable, or disable a project
- Change a tier, pin a provider, or override a model — those are `register`
  flags, and the registry is the only place that decision lives
- Pick the task, choose the worker, or decide when to stop
- Run more than one project. Fleet-wide work is the registry CLI plus
  `caobunga digest`; the batch run verb is `caobunga fleet` (CBN-312), and this skill is not one
