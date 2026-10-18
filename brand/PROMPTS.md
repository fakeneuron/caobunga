---
origin: generated
date: 2026-09-21
model: "" # code-drawn in-session; no image model (CBN-229)
---

# Brand prompts

Provenance for this kit's assets so a later model or a redesign does not
start from a chat transcript. Present, not counted — same class as
`README.md`. Keep prompts here, never in `public/`. Standard:
Fleet HQ's `docs/DESIGN-STANDARDS.md` §"Brand kit".

Fill **one** origin. Per asset, record either a `source:` path in this
repo plus the converter, or the prompt text plus any reference-image
notes. Omit a section when that asset is absent (promo is optional
until a real 1600 × 900 exists).

## logo.svg

- generated: no Imagine pass — the session had no image surface, so the
  raster 👁️ CONFIRM ran on magick renders (256 / 32 / 16 px, light and
  dark ground) of a **code-drawn** SVG, then the SVG was kept as-is.
  Design brief that stood in for the prompt: "A flat single-colour app
  icon: three equal filled circles in a straight row across the top
  (the worker CLIs), each joined by a straight filled bar to one larger
  ringed circle centred below (the dispatcher hub). Solid teal, no
  strokes, no text, no gradient, no shadow, transparent ground. Must
  read at 16 px as a fan-out."
- reference: caobunga's own model — one hub routing one task at a time
  to a fresh worker session (`VISION.md` §What it is). Operator chose
  this dispatcher fan-out over a conductor-baton and a task-card→slot
  concept (CBN-229 Discovery).
- vector pass: geometry in a 100-unit box — workers `r=12` at
  `(16,22) (50,22) (84,22)`, hub outer `r=17` / hole `r=6` at `(50,70)`,
  spokes are filled quads of half-width 6 anchored on the hub rim so the
  hole stays clean. Hub ring is one `fill-rule="evenodd"` path built from
  cubic quarter-circles (`κ = 0.5522847498`) — no masks, no `<image>`.
  Fill `#1b6b6b`. Square `viewBox` with 9-unit padding.

## favicon.svg

- generated: same mark and path data as `logo.svg`, tighter square
  `viewBox` (3-unit padding). Same fill `#1b6b6b`.
- Filled shapes throughout, so an ImageMagick tab-trio cut will not go
  blank. The trio in `frontend/public/` was **not** re-cut in CBN-229 —
  it is still the NAT-279 placeholder derivation and stays in place until
  a favicon-derive task cuts it from this file.

## promo-16x9.webp

- absent — no real 1600 × 900 hero exists; kit lands 3/4 by design.

## What not to change

- Icon only, no wordmark. Single colour `#1b6b6b` — it is the teal the
  existing tab trio's apple-touch background already uses.
- Fill, never stroke. Hub hole stays an evenodd path, never a mask or a
  light-coloured disc (the ground must stay transparent).
- Three workers, one hub. Do not add a fourth node or rotate it into a
  tree; the row-over-hub layout is what keeps it legible at 16 px.
- Never promote `frontend/public/favicon.svg` (the NAT-279 placeholder
  grid) into this kit.
