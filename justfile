# Project command interface — uniform verbs across the fleet.
#
# The same verbs work in every repo (`just dev`, `just test`, …); each one
# delegates to whatever the stack actually uses underneath. Standardizes the
# *interface*, not the implementation — so muscle memory is portable when you
# jump between apps, while each app's stack stays free to grow organically.
#
# Prerequisite: `just` (one-time `brew install just`). Run `just` with no args
# to list recipes. This file auto-adapts to the layout present — the
# `frontend/` · `backend/` · `landing/` · `worker/` subdir convention, OR a
# single-stack app at the repo root (`package.json` → npm, `pyproject.toml` →
# uv). Nothing to trim; delete a recipe only if it genuinely doesn't apply.
#
# `worker/` is gated (setup · lint · typecheck · test) but not a `dev` target:
# run it with `npx wrangler dev` from `worker/`, per docs/STACK-TENDENCIES.md
# §Deployment.
#
# Note: `lint` / `test` / `typecheck` / `e2e` / `coverage` here call the native
# tools (eslint/vitest/ruff/pytest/tsc/pyright/playwright/diff-cover) so they
# work for humans, agents, and CI alike — `typecheck` and `coverage` mirror the
# deposited CI workflow's gates exactly, so a red CI run reproduces locally with
# one verb.

# list available recipes
default:
    @just --list

# install dependencies for every stack present
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    # `main` = a primary subdir stack was found, so the root-app fallback is
    # off. `worker/` is additive and must NOT set it — a root app can carry one.
    ran=0; main=0
    if [ -d backend ];  then echo "› backend: uv sync";     (cd backend  && uv sync); ran=1; main=1; fi
    if [ -d frontend ]; then echo "› frontend: npm install"; (cd frontend && npm install); ran=1; main=1; fi
    if [ -d landing ];  then echo "› landing: npm install";  (cd landing  && npm install); ran=1; main=1; fi
    if [ -d worker ];   then echo "› worker: npm install";   (cd worker   && npm install); ran=1; fi
    if [ "$main" = 0 ]; then
      if   [ -f package.json ];   then echo "› root: npm install"; npm install; ran=1
      elif [ -f pyproject.toml ]; then echo "› root: uv sync";     uv sync;     ran=1
      fi
    fi
    if [ "$ran" = 0 ]; then echo "nothing to set up"; fi

# run the primary dev surface (frontend → landing → backend → root app)
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    if   [ -d frontend ]; then cd frontend && npm run dev
    elif [ -d landing ];  then cd landing  && npm run dev
    elif [ -d backend ];  then cd backend  && uv run python main.py
    elif [ -f package.json ];   then npm run dev
    elif [ -f pyproject.toml ]; then uv run python main.py
    else echo "nothing to run (no frontend/, landing/, backend/, or root app)"; exit 1
    fi

# run the backend dev server (fullstack repos: pair with `just dev` in another terminal)
api:
    cd backend && uv run python main.py

# run the landing-page dev server
landing:
    cd landing && npm run dev

# run tests for every stack present
test:
    #!/usr/bin/env bash
    set -euo pipefail
    ran=0; main=0
    if [ -d backend ];  then echo "› backend: pytest";  (cd backend  && uv run pytest); ran=1; main=1; fi
    if [ -d frontend ]; then echo "› frontend: vitest"; (cd frontend && npm test); ran=1; main=1; fi
    if [ -d worker ];   then echo "› worker: vitest";   (cd worker   && npm test); ran=1; fi
    if [ "$main" = 0 ]; then
      if   [ -f package.json ];   then echo "› root: test";   npm test;      ran=1
      elif [ -f pyproject.toml ]; then echo "› root: pytest"; uv run pytest; ran=1
      fi
    fi
    if [ "$ran" = 0 ]; then echo "no tests"; fi

# run end-to-end tests (Playwright) — frontend subdir, else root, if present
e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    if   [ -d frontend ];     then cd frontend && npx playwright test
    elif [ -f package.json ]; then npx playwright test
    else echo "no e2e tests"; fi

# lint every stack present (type-checking is its own verb: `just typecheck`)
lint:
    #!/usr/bin/env bash
    set -euo pipefail
    ran=0; main=0
    # `&& uv run ruff format --check .` is a deliberate divergence from Fleet HQ's templates/justfile
    # (CBN-095) — Fleet HQ's layer-refresh skill must decline this file's per-file confirm, not re-copy it.
    if [ -d backend ];  then echo "› backend: ruff";        (cd backend  && uv run ruff check . && uv run ruff format --check .); ran=1; main=1; fi
    if [ -d frontend ]; then echo "› frontend: eslint";     (cd frontend && npm run lint); ran=1; main=1; fi
    if [ -d landing ];  then echo "› landing: astro check"; (cd landing  && npm run check); ran=1; main=1; fi
    if [ -d worker ];   then echo "› worker: eslint";       (cd worker   && npm run lint); ran=1; fi
    if [ "$main" = 0 ]; then
      if   [ -f package.json ];   then echo "› root: lint"; npm run lint;        ran=1
      elif [ -f pyproject.toml ]; then echo "› root: ruff"; uv run ruff check .; ran=1
      fi
    fi
    if [ "$ran" = 0 ]; then echo "nothing to lint"; fi

# type-check every stack present (tsc via `npm run typecheck`; pyright for Python)
typecheck:
    #!/usr/bin/env bash
    set -euo pipefail
    ran=0; main=0
    if [ -d backend ];  then echo "› backend: pyright"; (cd backend  && uv run pyright); ran=1; main=1; fi
    if [ -d frontend ]; then echo "› frontend: tsc";    (cd frontend && npm run typecheck); ran=1; main=1; fi
    if [ -d worker ];   then echo "› worker: tsc";      (cd worker   && npm run typecheck); ran=1; fi
    if [ "$main" = 0 ]; then
      if   [ -f package.json ];   then echo "› root: tsc";     npm run typecheck; ran=1
      elif [ -f pyproject.toml ]; then echo "› root: pyright"; uv run pyright;    ran=1
      fi
    fi
    if [ "$ran" = 0 ]; then echo "nothing to type-check"; fi

# changed-line coverage for every stack present (mirrors the CI gate)
# Usage: `just coverage` (vs. origin/main) or `just coverage HEAD~3`.
coverage BASE="origin/main":
    #!/usr/bin/env bash
    set -euo pipefail
    # Same gate CI runs, same tool, same threshold — see the coverage block in
    # .github/workflows/ci.yml. Pin matches the workflow's DIFF_COVER_VERSION;
    # bump both together or they drift apart.
    DC="diff-cover@10.5.1"; MIN=80
    ran=0; main=0
    if [ -d backend ]; then
      echo "› backend: pytest --cov + diff-cover"
      (cd backend && uv run pytest -q --cov=. --cov-report=xml >/dev/null)
      # Cobertura carries an absolute <source>, so no path rewrite is needed.
      uvx "$DC" backend/coverage.xml --compare-branch="{{BASE}}" --fail-under=$MIN --show-uncovered
      ran=1; main=1
    fi
    if [ -d frontend ]; then
      echo "› frontend: vitest --coverage + diff-cover"
      (cd frontend && npm test -- --run --coverage >/dev/null)
      # lcov SF: paths are stack-dir-relative; git diff paths are repo-root
      # relative, and diff-cover's --src-roots does not remap lcov. Without
      # this rewrite the gate passes everything silently (Fleet HQ NAT-229).
      sed -i.bak 's|^SF:|SF:frontend/|' frontend/coverage/lcov.info && rm -f frontend/coverage/lcov.info.bak
      uvx "$DC" frontend/coverage/lcov.info --compare-branch="{{BASE}}" --fail-under=$MIN --show-uncovered
      ran=1; main=1
    fi
    if [ -d worker ]; then
      echo "› worker: vitest --coverage + diff-cover"
      (cd worker && npm test -- --run --coverage >/dev/null)
      sed -i.bak 's|^SF:|SF:worker/|' worker/coverage/lcov.info && rm -f worker/coverage/lcov.info.bak
      uvx "$DC" worker/coverage/lcov.info --compare-branch="{{BASE}}" --fail-under=$MIN --show-uncovered
      ran=1
    fi
    if [ "$main" = 0 ]; then
      if [ -f package.json ]; then
        echo "› root: vitest --coverage + diff-cover"
        npm test -- --run --coverage >/dev/null
        # Root layout: SF: paths are already repo-root-relative, no rewrite.
        uvx "$DC" coverage/lcov.info --compare-branch="{{BASE}}" --fail-under=$MIN --show-uncovered
        ran=1
      elif [ -f pyproject.toml ]; then
        echo "› root: pytest --cov + diff-cover"
        uv run pytest -q --cov=. --cov-report=xml >/dev/null
        uvx "$DC" coverage.xml --compare-branch="{{BASE}}" --fail-under=$MIN --show-uncovered
        ran=1
      fi
    fi
    if [ "$ran" = 0 ]; then echo "nothing to measure"; fi

# build every stack present (backend has no build step by default)
build:
    #!/usr/bin/env bash
    set -euo pipefail
    ran=0
    if [ -d frontend ]; then echo "› frontend: build"; (cd frontend && npm run build); ran=1; fi
    if [ -d landing ];  then echo "› landing: build";  (cd landing  && npm run build); ran=1; fi
    if [ "$ran" = 0 ] && [ -f package.json ]; then echo "› root: build"; npm run build; fi

# Project-only recipes (CBN-367), absent from Fleet HQ's templates/justfile —
# another reason, beside `lint`, to decline layer-refresh's re-copy of this file.

# build a one-commit snapshot of the public ship set into OUT, then scan it
public-export OUT="dist/public-export" AUTHOR="fakeneuron <fakeneuron@users.noreply.github.com>":
    #!/usr/bin/env bash
    set -euo pipefail
    # Project-specific, not a fleet verb (CBN-367; docs/decisions/open-source-scrub.md).
    # Ship set (CBN-366): everything tracked outside `.flaitron/`, minus
    # `docs/history/` and the scrub doc, which still name private repos. Also
    # dropped: flaitron wiring that would dangle without `.flaitron/` —
    # `.gitmodules`, every symlink pointing into it, the `/audit` overlay,
    # which runs the submodule's scaffold (CBN-371), `.cursorrules` (flaitron
    # task instructions; Cursor reads AGENTS.md), and `.ignore` +
    # `.claude/settings.json`, whose only rule fences the submodule dogfood
    # archive (CBN-374). Reads committed HEAD via `git archive`, so working-tree
    # edits never ship.
    out="{{OUT}}"; author="{{AUTHOR}}"
    # Marker inside the snapshot's .git (never shipped): OUT is only ever
    # deleted when an earlier run of this recipe made it.
    marker=".git/caobunga-public-export"
    if [ -e "$out" ] && [ -n "$(ls -A "$out")" ] && [ ! -f "$out/$marker" ]; then
      echo "✗ $out exists and is not a previous public export — refusing to delete it"; exit 1
    fi
    rm -rf "$out"; mkdir -p "$out/.git"; touch "$out/$marker"  # before anything can fail
    git archive HEAD -- . ':(exclude).flaitron' ':(exclude)docs/history' \
      ':(exclude)docs/decisions/open-source-scrub.md' ':(exclude).gitmodules' \
      ':(exclude).claude/skills/audit' ':(exclude).cursorrules' ':(exclude).ignore' \
      ':(exclude).claude/settings.json' \
      | tar -x -C "$out"
    find "$out" -type l | while IFS= read -r link; do
      case "$(readlink "$link")" in *.flaitron|*.flaitron/*) rm "$link" ;; esac
    done
    find "$out" -type d -empty ! -path "$out/.git" -delete
    dangling="$(find "$out" -type l ! -exec test -e {} \; -print)"
    if [ -n "$dangling" ]; then echo "✗ dangling symlinks in the snapshot:"; echo "$dangling"; exit 1; fi
    # Shipped docs cite `.flaitron/` files and flaitron wiring the snapshot
    # drops (CBN-369, CBN-371): swap AGENTS.md's and CLAUDE.md's flaitron
    # `## Workflow` sections for a pointer, cut the inline citations, and unlink
    # every markdown link into `.flaitron/`, and cut `.cursorignore`'s
    # dogfood-archive block (CBN-374), then fail closed if the source grew a
    # reference these rewrites miss.
    agents="$out/AGENTS.md"; claude="$out/CLAUDE.md"; readme="$out/README.md"
    cursorignore="$out/.cursorignore"
    for f in "$agents" "$claude" "$readme" "$cursorignore"; do
      [ -f "$f" ] || { echo "✗ $f missing from the snapshot"; exit 1; }
    done
    # absent PATTERN [GREP-OPTION...] FILE...: fail on any match (grep exit 1 = clean).
    absent() {
      local pat="$1" rc=0 left; shift
      left="$(grep -HnE -e "$pat" "$@")" || rc=$?
      if [ "$rc" = 0 ]; then echo "✗ the snapshot still cites flaitron wiring:"; echo "$left"; exit 1; fi
      [ "$rc" = 1 ] || { echo "✗ grep failed (exit $rc) on $*"; exit 1; }
    }
    pointer='Planning and task tracking (flaitron) live in the private repo this snapshot is exported from; they are not shipped here.'
    POINTER="$pointer" perl -0pi -e 's/^## Workflow\n.*?(?=^## |\z)/## Workflow\n\n$ENV{POINTER}\n\n/ms; s/\n+\z/\n/' "$agents" "$claude"
    perl -0pi -e 's/ \(Flaitron \+ per-repo sections\)//' "$claude"
    perl -0pi -e 's/ and `\.flaitron\/PLAN\.md` \(folded from `SEED-PLAN\.md` on [0-9-]+\)//;
      s/ Registered in `\.flaitron\/tasknote\/README\.md`\.//;
      s/ and the cited CBN-tasknote archive//;
      s/^- `\/ft-audit` is forked as a \*\*thin overlay\*\*.*\n(?:[ \t]+\S.*\n)*//m;
      s/`\.pre-commit-config\.yaml`, `\.cursorrules`, and `\.editorconfig`(.*?)for all four\*\*/`.pre-commit-config.yaml`, and `.editorconfig`$1for all three**/;
      s/The skill and\s+the `\/audit` overlay are the only project-owned entries in `\.claude\/skills\/` —\s+everything else there is a symlink into the flaitron submodule, which is read-only\./The skill is the only entry shipped in `.claude\/skills\/`./' "$agents"
    perl -0pi -e 's/; active work is tracked in \[`\.flaitron\/PLAN\.md`\]\(\.flaitron\/PLAN\.md\)\./; active work is tracked in the private repo this snapshot is exported from./' "$readme"
    perl -0pi -e 's/^# Submodule dogfood archive\b.*\n(?:#.*\n)*\.flaitron\/core\/\.flaitron\/\n\n?//m' "$cursorignore"
    absent '\.flaitron|ft-audit|skills.audit|symlink into|CBN-tasknote|cursorrules' "$agents"
    absent '\.flaitron|ft-task|Handoff|Flaitron \+' "$claude"
    absent '\(\.flaitron/PLAN\.md\)' "$readme"
    absent '\.flaitron/core|MIGRATION' "$cursorignore"
    # Any other inline link into `.flaitron/` keeps its text and loses its dead
    # target; a reference-style definition is not rewritten, only failed on.
    find "$out" -name '*.md' ! -path "$out/.git/*" -print0 \
      | xargs -0 perl -pi -e 's/\[([^\]]*)\]\([^)]*\.flaitron[^)]*\)/$1/g'
    absent '\]\([^)]*\.flaitron|^[[:space:]]*\[[^]]+\]:[[:space:]]*[^[:space:]]*\.flaitron' -r --include='*.md' --exclude-dir=.git "$out"
    name="${author% <*}"; email="${author##*<}"; email="${email%>}"
    git -C "$out" init -q -b main
    # --force: ship what HEAD tracks even where a shipped or global ignore rule matches.
    git -C "$out" add -A --force
    GIT_AUTHOR_NAME="$name" GIT_AUTHOR_EMAIL="$email" GIT_COMMITTER_NAME="$name" GIT_COMMITTER_EMAIL="$email" \
      git -C "$out" -c commit.gpgsign=false -c core.hooksPath=/dev/null commit -q -m "Caobunga public snapshot"
    echo "› snapshot: $out ($(git -C "$out" ls-files | wc -l | tr -d ' ') files, one commit $(git -C "$out" rev-parse --short HEAD))"
    "{{just_executable()}}" public-export-scan "$out"

# re-run every open-source-scrub scan class on a snapshot repo; fails on any hit
public-export-scan DIR="dist/public-export":
    #!/usr/bin/env bash
    set -euo pipefail
    # Classes: docs/decisions/open-source-scrub.md §"Flip-time checklist" step 5.
    # The private-name denylist is that file's `public-export-denylist` block and
    # the email is read at run time — never written here, because this justfile
    # ships in the export and would leak them (and fail its own scan).
    dir="{{DIR}}"; doc="docs/decisions/open-source-scrub.md"
    [ -d "$dir/.git" ] || { echo "✗ $dir is not a git repo"; exit 1; }
    [ -f "$doc" ] || { echo "✗ $doc missing — run the scan from the private repo"; exit 1; }
    deny="$(sed -n '/^```public-export-denylist$/,/^```$/{/^```/d;p;}' "$doc")"
    [ -n "$deny" ] || { echo "✗ no public-export-denylist block in $doc"; exit 1; }
    email="$(git config user.email || true)"
    [ -n "$email" ] || { echo "✗ git config user.email is empty — nothing to scan for"; exit 1; }
    fail=0
    check() { if [ -n "$2" ]; then echo "✗ $1"; printf '%s\n' "$2" | sed 's/^/    /'; fail=1; else echo "✓ $1"; fi; }
    # Exit 1 is "no match"; anything higher (a bad denylist regex, say) fails
    # closed as a hit instead of reading as a clean pass.
    m() { local rc=0; "$@" || rc=$?; [ "$rc" -le 1 ] || echo "scan error: exit $rc from $1 $2"; }
    # Content checks grep every commit, so a clone of the pushed public repo is
    # covered even after later commits edit or delete a hit.
    revs="$(git -C "$dir" rev-list --all)"
    g() { m git -C "$dir" grep "$@" $revs; }
    check "home paths" "$(g -nE '[/](Users|home)[/][A-Za-z0-9._-]+' | grep -vE '[/]Users[/](dev|me)[/]' || true)"
    check "operator email" "$(g -niF "$email")"
    check "hostnames, non-loopback IPs, remote-shell targets" \
      "$(g -noiE '([0-9]{1,3}[.]){3}[0-9]{1,3}|ssh[ ]' | grep -vE ':(127[.]0[.]0[.]1|0[.]0[.]0[.]0)$' || true)"
    check "private repo names (content)" "$(g -niE -f <(printf '%s\n' "$deny"))"
    check "private repo names (paths)" \
      "$(git -C "$dir" log --all --format= --name-only | sort -u | m grep -iE -f <(printf '%s\n' "$deny"))"
    ids="$(git -C "$dir" log --all --format='%an <%ae>%n%cn <%ce>' | sort -u)"
    check "commit identities" \
      "$(printf '%s\n' "$ids" | m grep -iF "$email"; printf '%s\n' "$ids" | m grep -iE -f <(printf '%s\n' "$deny"))"
    # gitleaks finds `$dir/.gitleaks.toml` itself; any non-zero exit (leak or error) fails closed.
    leaks() { local o; o="$(gitleaks "$1" --no-banner --redact "$dir" 2>&1)" || printf '%s\n' "$o" | tail -n 20; }
    check "secrets (history)" "$(leaks git)"
    check "secrets (tree)" "$(leaks dir)"
    echo "  identities: $(printf '%s' "$ids" | tr '\n' ' ')"
    if [ "$fail" != 0 ]; then echo "✗ $dir: scan failed — do not publish"; exit 1; fi
    echo "✓ $dir: every scan class clean"
