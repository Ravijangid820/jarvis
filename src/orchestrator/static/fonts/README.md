# Self-hosted fonts

These fonts are bundled so the UI renders correctly **offline / on a private
server** with no request to any third party (e.g. Google Fonts).

| Family         | Use                      | Weights        | License        | Author              |
|----------------|--------------------------|----------------|----------------|---------------------|
| Rajdhani       | HUD display / headings   | 400/500/600/700| SIL OFL 1.1    | Indian Type Foundry |
| JetBrains Mono | Data / monospace / code  | 400/500/700    | SIL OFL 1.1    | JetBrains           |

## Provenance

The `.woff2` files were downloaded from **`fonts.gstatic.com`** — Google's
official font CDN — i.e. the exact files a browser fetches from Google Fonts.
Only the **Latin** and **Latin-Ext** subsets are included (the UI is English);
Rajdhani's Devanagari glyphs are intentionally omitted to keep the size small.

The [SIL Open Font License 1.1](https://openfontlicense.org/) explicitly permits
bundling and redistribution. `fonts.css` is generated — see `scripts` below to
regenerate.

## Regenerate

Re-run the generator (fetches from gstatic and rewrites `fonts.css`):

```
uv run python src/scripts/fetch_fonts.py
```
