/**
 * check-no-inline-scripts.mjs — postbuild guard against a CSP-blocked build.
 *
 * The orchestrator serves `script-src 'self' 'wasm-unsafe-eval'` with no 'unsafe-inline', so an
 * inline <script> in index.html is blocked outright. Crucially it fails QUIETLY: the page still
 * renders and only a console violation marks it, which makes the breakage easy to ship and hard
 * to notice. The GitHub Pages redirect shim sat broken here exactly that way.
 *
 * Runs as a postbuild hook, so it covers CI (which runs the production build) as well as local
 * builds. Startup code belongs in src/main.jsx, where it is served from /assets under 'self'.
 */
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const index = join(dirname(fileURLToPath(import.meta.url)), "..", "dist", "index.html");

if (!existsSync(index)) {
  console.error("[check-csp] dist/index.html not found — did the build run?");
  process.exit(1);
}

// Strip comments first: a comment legitimately mentioning scripts is not an inline script.
const html = readFileSync(index, "utf8").replace(/<!--[\s\S]*?-->/g, "");
const inline = html.match(/<script(?![^>]*\bsrc=)[^>]*>/g) ?? [];

if (inline.length > 0) {
  console.error(
    `[check-csp] ${inline.length} inline <script> in dist/index.html — the CSP will block it ` +
    `silently at runtime. Move that code into src/main.jsx.`
  );
  process.exit(1);
}

console.log("[check-csp] no inline scripts — build is CSP-clean.");
