# brand/

This project's public identity kit — four files with fixed names, so any
consumer can copy them without asking what they are called here.

| File | What | Minimum |
|---|---|---|
| `BRAND.md` | The blurb: `tagline` / `one_liner` / `description` front-matter + free body | Three fields filled |
| `logo.svg` | The mark, transparent ground | Legible at 32 px; no embedded raster |
| `favicon.svg` | Tab icon (optional `favicon.png` fallback, not counted) | Square; reads at 16 px |
| `promo-16x9.webp` | One hero still for cards, link previews, posts | 1600 × 900 |

`PROMPTS.md` is provenance for mined or generated assets (origin date,
optional model, per-asset source path or prompt, what not to change).
Present, **not counted** — same class as this README, never a fifth kit
file.

Only `BRAND.md` and this README are deposited; the three assets and
`PROMPTS.md` are yours to add when the kit is filled. Presence of the
four counted files is reported (never enforced) by Fleet HQ's
layer-drift skill. Standard (SSOT):
Fleet HQ's `docs/DESIGN-STANDARDS.md` §"Brand kit". Tab-trio magick:
Fleet HQ's `docs/STACK-TENDENCIES.md` §Favicons.

## How to fill (producer session)

Run this from **this repo**, attended. Do not run it from Fleet HQ.
Do not run it unattended. Caobunga is not the runner.

1. **Classify.** Mine an in-repo mark; generate if there is none; no-op
   if the four counted files plus `PROMPTS.md` already exist. Leave a
   working tab trio in place even when the names are pre-standard.
2. **Never** mine `fakeneuron.com/public/projects/` (cropped, recolored,
   640×360). Never embed a raster inside `logo.svg` / `favicon.svg`.
3. **Mine.** Convert the producer original → `logo.svg` (legible at
   32 px) + `favicon.svg` (square, reads at 16 px, **filled shapes** if
   ImageMagick will cut the trio). Promo only at a real 1600 × 900.
4. **Generate.** Still → 👁️ CONFIRM → **code-drawn** SVG (paths/shapes,
   filled, no PNG inside). Converter or operator-trace are fallbacks.
   Promo stays webp or is omitted.
5. **Copy** Fleet HQ's `templates/brand/PROMPTS.md` to `brand/PROMPTS.md`
   and fill origin + per-asset `source:` or prompt. Caps on `BRAND.md`
   still apply (tagline ≤ 8 / one_liner ≤ 25 / description ≤ 80).
6. **Derive** the tab trio from `brand/favicon.svg` only if this repo
   has a frontend (STACK-TENDENCIES recipe). Skip if there is no
   frontend. Fleet HQ's dashboard `data:` favicon is not drift.
7. 👁️ CONFIRM new pixels. Commit here.
