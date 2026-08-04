/**
 * check-build.mjs — postbuild guards for failures that are SILENT at runtime.
 *
 * Everything checked here breaks without an exception: the page still renders, so the damage is
 * easy to ship and only shows up as a console line or a spinner that never finishes.
 *
 * The orchestrator serves `script-src 'self' 'wasm-unsafe-eval'` with no 'unsafe-inline', so an
 * inline <script> in index.html is blocked outright. Crucially it fails QUIETLY: the page still
 * renders and only a console violation marks it, which makes the breakage easy to ship and hard
 * to notice. The GitHub Pages redirect shim sat broken here exactly that way.
 *
 * Runs as a postbuild hook, so it covers CI (which runs the production build) as well as local
 * builds. Startup code belongs in src/main.jsx, where it is served from /assets under 'self'.
 */
import { readFileSync, existsSync, readdirSync } from "node:fs";
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

// --- ORT runtime is present, and addressed with the right base -----------------------------
// A missing or wrong-pathed ORT file 404s inside onnxruntime, where it does not raise — the load
// promise simply never settles and the UI hangs at "Preparing…". Both halves are checked: the
// files exist where the build put them, and the worker addresses them through Vite's BASE_URL
// rather than a hardcoded "/", which would 404 on the /jarvis/ GitHub Pages build.
const distDir = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const ortRoot = join(distDir, "ort");

if (!existsSync(ortRoot)) {
  console.error("[check-ort] dist/ort is missing — the speech runtime would 404 and hang.");
  process.exit(1);
}
const [version] = readdirSync(ortRoot);
const ortFiles = readdirSync(join(ortRoot, version)).filter((f) => f.endsWith(".wasm"));
if (ortFiles.length < 4) {
  console.error(
    `[check-ort] only ${ortFiles.length} .wasm variants in dist/ort/${version} — ORT picks its ` +
    `variant at runtime from feature detection, and a missing one hangs instead of erroring.`
  );
  process.exit(1);
}

// Derive the deploy base from the entry script Vite emitted (e.g. "/jarvis/assets/index-x.js").
const base = (html.match(/src="([^"]*)assets\/index-/) ?? [, "/"])[1];
const [workerBundle] = readdirSync(join(distDir, "assets")).filter((f) => f.startsWith("whisper-worker-"));
const workerSrc = readFileSync(join(distDir, "assets", workerBundle), "utf8");
if (base !== "/" && /["'`]\/ort\//.test(workerSrc)) {
  console.error(
    `[check-ort] worker hardcodes "/ort/" but this build is served from "${base}" — it would 404 ` +
    `on Pages and hang. Use import.meta.env.BASE_URL.`
  );
  process.exit(1);
}

console.log(`[check-ort] ${ortFiles.length} wasm variants at ${base}ort/${version}/ — runtime is reachable.`);
