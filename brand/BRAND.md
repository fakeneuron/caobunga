---
tagline: "One task, one fresh session"
one_liner: "A multi-project orchestrator that picks each Flaitron repo's next task, runs it in a fresh coding-CLI session, and records what committed."
description: "Caobunga is the policy and routing layer above Flaitron and the coding agents it drives. It registers approved repos explicitly, picks each one's next task from its own plan, routes it to an authenticated coding CLI, runs it in a fresh isolated worker session, and records the closure on an orchestrator-owned run record. Failures escalate to the operator; Flaitron stays the work SSOT; the status dashboard is read-only."
---

# Brand

The three fields above are this project's public blurb — the one place a
card, a catalog entry, a README index, or a social post should quote from.
Fill all three; keep them public-safe. Caps are caps, not targets:

- `tagline` — ≤ 8 words. The catchphrase.
- `one_liner` — ≤ 25 words. One sentence: what it is and for whom.
- `description` — ≤ 80 words. The paragraph a card could carry whole.

Below this line the file is free — longer copy, voice notes, what not to
say. Standard: Fleet HQ's `docs/DESIGN-STANDARDS.md` §"Brand kit".

## Voice notes

- Say **orchestrator** / **routing layer**, never "agent platform" or "AI
  that codes for you" — the coding CLIs do the work; caobunga dispatches.
- Flaitron is always named as the SSOT it sits above; caobunga never
  "replaces" or "extends" it.
- Do not promise autonomy. The tiers are `observe` → `gated` →
  `proceed-on-green`; failures escalate to the operator, no retries.
- The mark is a dispatcher: three worker nodes fanning out of one hub.
  Read it as "one hub, many fresh sessions", not as a tree or a trident.
