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
import { copyFileSync, mkdirSync, readdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, "..", "node_modules", "onnxruntime-web", "dist");
const DEST = join(here, "..", "public", "ort");

// We run the CPU backend (device: "wasm"), so we need the threaded builds and their
// loaders. `jsep` is the WebGPU backend — ~26 MB we would never execute, so it is
// excluded rather than shipped as dead weight.
const WANTED = /^ort-wasm-simd-threaded(\.asyncify|\.jspi)?\.(mjs|wasm)$/;

if (!existsSync(SRC)) {
  console.error(`[copy-ort] onnxruntime-web not found at ${SRC} — run npm ci first.`);
  process.exit(1);
}

mkdirSync(DEST, { recursive: true });

const copied = readdirSync(SRC).filter((f) => WANTED.test(f));
if (copied.length === 0) {
  console.error("[copy-ort] no matching ORT runtime files found — did onnxruntime-web change its layout?");
  process.exit(1);
}

for (const file of copied) {
  copyFileSync(join(SRC, file), join(DEST, file));
}

console.log(`[copy-ort] vendored ${copied.length} ONNX Runtime files into public/ort/`);
