---
description: Run caobunga against the repo you are currently in — resolves the cwd to its registered slug, shows the tier, confirms, then shells out to `caobunga run <slug>`. Refuses on an unregistered repo.
argument-hint: (no arguments)
---

Invoke the `caobunga` skill. It resolves the current git repo to a registered
slug by reading `$CAOBUNGA_HOME/projects/*.yaml`, reports the tier it will
honor, confirms before launching anything that is not `observe`, and runs
`caobunga run <slug>`.

This is caobunga's **in-repo entry point**. The other one is the registry CLI —
`caobunga run <slug>` — which addresses a project by slug from anywhere. Both
run one project at a time and share the same run loop; they differ only in how
the project is named. See `docs/CONTRACT.md` §"Entry points".

If the repo is not registered, the skill refuses and prints the `caobunga
register` command for you to run yourself. It never registers anything on your
behalf — explicit registration is a v1 non-negotiable.
