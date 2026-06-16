"""Download the exact woff2 files Google serves for the fonts we already use,
keep only the Latin subset + the weights in use, and emit a local fonts.css.
Source: fonts.gstatic.com (Google's official font CDN). Both families are SIL OFL.
"""
import re
import urllib.request
from pathlib import Path

OUT = Path("/srv/jarvis/src/orchestrator/static/fonts")
OUT.mkdir(parents=True, exist_ok=True)

CSS_URL = ("https://fonts.googleapis.com/css2?"
           "family=Rajdhani:wght@400;500;600;700&"
           "family=JetBrains+Mono:wght@400;500;700&display=swap")
# Browser UA so the API returns woff2 (older UAs get ttf).
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
KEEP_SUBSETS = {"latin", "latin-ext"}


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def slug(family):
    return family.lower().replace(" ", "-")


css = fetch(CSS_URL, {"User-Agent": UA}).decode("utf-8")

# Each @font-face is preceded by a /* subset */ comment.
blocks = re.split(r"/\*\s*([\w-]+)\s*\*/", css)
# re.split keeps captures: ['', subset, block, subset, block, ...]
local_css = ["/* Self-hosted fonts (SIL OFL). Files from fonts.gstatic.com.",
             "   Latin subset only; generated, do not hand-edit. */", ""]
downloaded = []
for i in range(1, len(blocks), 2):
    subset = blocks[i].strip()
    body = blocks[i + 1]
    if "@font-face" not in body or subset not in KEEP_SUBSETS:
        continue
    fam = re.search(r"font-family:\s*'([^']+)'", body).group(1)
    weight = re.search(r"font-weight:\s*(\d+)", body).group(1)
    url = re.search(r"src:\s*url\(([^)]+)\)", body).group(1)
    urange = re.search(r"unicode-range:\s*([^;]+);", body).group(1).strip()

    fname = f"{slug(fam)}-{weight}-{subset}.woff2"
    data = fetch(url)
    (OUT / fname).write_bytes(data)
    downloaded.append((fname, len(data)))

    local_css.append("@font-face {")
    local_css.append(f"  font-family: '{fam}';")
    local_css.append("  font-style: normal;")
    local_css.append(f"  font-weight: {weight};")
    local_css.append("  font-display: swap;")
    local_css.append(f"  src: url('./{fname}') format('woff2');")
    local_css.append(f"  unicode-range: {urange};")
    local_css.append("}")
    local_css.append("")

(OUT / "fonts.css").write_text("\n".join(local_css))

total = sum(s for _, s in downloaded)
print(f"Downloaded {len(downloaded)} files, {total/1024:.1f} KB total:")
for fname, size in downloaded:
    print(f"  {fname:40s} {size/1024:6.1f} KB")
print(f"\nWrote {OUT/'fonts.css'}")
