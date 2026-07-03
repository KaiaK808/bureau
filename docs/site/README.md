# Bureau docs site

A static HTML rendering of `docs/*.md`, styled in the **Brainhuggers Bureau** design system (hot pink on near-black, system sans, hard edges, glow shadows, status taxonomy).

## What's here

```
docs/site/
├── index.html           # landing page — hero + 5 bureau-file cards + quickstart
├── configuration.html   # full .bureau.json + BUREAU_* reference (FILE #01)
├── exit-codes.html      # exit code table + Telegram alerts + supervisor (FILE #02)
├── recipes.html         # config patterns: single-flight, mixed models, dry-run (FILE #03)
├── troubleshooting.html # symptom → fix (FILE #04)
└── assets/
    ├── colors_and_type.css   # design system tokens — copied verbatim from handoff
    └── docs.css              # docs-specific layer: layout, sidebar, tables, callouts
```

The pages pull **JetBrains Mono** from Google Fonts at runtime and use a system-sans stack for display and body copy. The tokens in `colors_and_type.css` still name `'Babcock'` and `'Silka'` as the first font-family so a downstream site with a valid Atipo Foundry license can restore them transparently via its own `@font-face` block.

## Source of truth

`docs/*.md` is canonical — those files are what Claude reads via `SKILL.md`. The HTML is hand-rendered from the markdown for human consumption. When updating a config knob:

1. Edit `bureau-config.sh` (the code).
2. Update `docs/configuration.md` (canonical reference).
3. Update `docs/site/configuration.html` to match.

If the markdown and HTML drift, the markdown wins.

## Viewing locally

Open any HTML file directly in a browser. There's no build step, no server required:

```sh
open docs/site/index.html
```

Or serve with any static server:

```sh
python3 -m http.server -d docs/site 8000
# → http://localhost:8000
```

## Design system origin

Tokens lifted from the [Brainhuggers Bureau design system handoff bundle](https://claude.ai/design). The raw `.tar.gz` is gitignored — only the extracted CSS + woff2 fonts ship here.

House rules (don't break these):

- **One pink moment per viewport.** `#ff2d55` is signal, never fill.
- **Near-black `#0a0a0b` everywhere.** No alternating bands.
- **Hard edges.** `border-radius: 0` on cards, tags, rules. Only `<button>` (8px) and brain-burger circle round.
- **Glow shadows, not drops.** Colour-matched to `--accent-glow`.
- **Uppercase + 0.10em tracking** on every headline, status tag, button, nav label.
- **No emoji. No icons.** Bracket vocabulary (`[OPEN FILE]`, `[ACTIVE BUILD]`, `[CLASSIFIED]`) is the iconography.
- **Status taxonomy is the semantic colour system.** Pink = ACTIVE BUILD, Yellow = FIELD REPORT, Cyan = DOCUMENTING, Green = FIELDWORK, Neutral = INCUBATING / CLASSIFIED.
