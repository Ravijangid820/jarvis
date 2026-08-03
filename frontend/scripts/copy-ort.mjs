/**
 * copy-ort.mjs — vendor the ONNX Runtime WASM binaries into public/ort/.
 *
 * Why this exists: onnxruntime-web loads its backend as a PAIR of files — a .mjs
 * loader plus the .wasm binary. Vite's bundler only emits the .wasm, so at runtime
 * ORT falls back to its built-in default for the loader, which is
 * `https://cdn.jsdelivr.net/npm/onnxruntime-web@<ver>/dist/`. Our CSP blocks that
 * (correctly), and the failure surfaces as the very unhelpful
 * "no available backend found. ERR: [wasm] TypeError: Failed to fetch".
 *
 * So we serve both files ourselves and point env.backends.onnx.wasm.wasmPaths at
 * them (see whisper-worker.js). Unlike the Whisper weights — public model data, for
 * which huggingface.co is the preferred first-party source — this is executable
 * application code: fetching it from a third-party CDN at runtime would be a
 * supply-chain regression, so there is deliberately NO remote fallback here.
 *
 * Runs from package.json's prebuild/predev hooks. public/ort/ is gitignored: it is
 * a build artifact copied from the locked node_modules, not source.
 */
import { copyFileSync, mkdirSync, readdirSync, existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

// Locate onnxruntime-web wherever npm actually put it. The `overrides` pin in package.json
// leaves it NESTED under @huggingface/transformers rather than hoisted, and ORT's exports map
// does not expose ./package.json, so require.resolve is not usable here — check paths directly.
const NM = join(here, "..", "node_modules");
const SRC = [
  join(NM, "onnxruntime-web", "dist"),
  join(NM, "@huggingface", "transformers", "node_modules", "onnxruntime-web", "dist"),
].find((dir) => existsSync(dir));

// We run the CPU backend (device: "wasm"), so we need the threaded builds and their
// loaders. `jsep` is the WebGPU backend — ~26 MB we would never execute, so it is
// excluded rather than shipped as dead weight.
const WANTED = /^ort-wasm-simd-threaded(\.asyncify|\.jspi)?\.(mjs|wasm)$/;

if (!SRC || !existsSync(SRC)) {
  console.error("[copy-ort] could not locate onnxruntime-web's dist/ — run npm ci first.");
  process.exit(1);
}

// Files land under public/ort/<version>/ so the URL is content-addressed by ORT version.
// These filenames are stable across releases, so a flat path served `immutable` would pin one
// build into every browser for a year: upgrading ORT would swap the bytes on disk while clients
// kept executing the cached old binary. (That is exactly what happened with 1.26.0-dev — new JS
// glue running against a stale cached .wasm.) Versioning the path makes `immutable` honest.
const ORT_VERSION = JSON.parse(readFileSync(join(dirname(SRC), "package.json"), "utf8")).version;
const DEST = join(here, "..", "public", "ort", ORT_VERSION);

mkdirSync(DEST, { recursive: true });

const copied = readdirSync(SRC).filter((f) => WANTED.test(f));
if (copied.length === 0) {
  console.error("[copy-ort] no matching ORT runtime files found — did onnxruntime-web change its layout?");
  process.exit(1);
}

for (const file of copied) {
  copyFileSync(join(SRC, file), join(DEST, file));
}

console.log(`[copy-ort] vendored ${copied.length} ONNX Runtime files into public/ort/${ORT_VERSION}/`);
