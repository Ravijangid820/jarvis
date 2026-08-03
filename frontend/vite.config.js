import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))

// The ONNX Runtime is served from /ort/<version>/ (see scripts/copy-ort.mjs). Baking the
// version into the bundle keeps the fetch URL and the vendored files in lockstep: bumping ORT
// changes the URL, so browsers can never execute a cached older .wasm against newer JS glue.
// Resolved the same two ways as copy-ort.mjs — the `overrides` pin leaves ORT nested.
function ortVersion() {
  const nm = join(here, 'node_modules')
  const pkg = [
    join(nm, 'onnxruntime-web', 'package.json'),
    join(nm, '@huggingface', 'transformers', 'node_modules', 'onnxruntime-web', 'package.json'),
  ].find((p) => existsSync(p))
  if (!pkg) throw new Error('vite.config: cannot locate onnxruntime-web — run npm ci first.')
  return JSON.parse(readFileSync(pkg, 'utf8')).version
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE_PATH || '/',
  define: {
    __ORT_VERSION__: JSON.stringify(ortVersion()),
  },
  worker: {
    format: 'es',       // ES module workers for @huggingface/transformers
  },
  optimizeDeps: {
    exclude: ['@huggingface/transformers'],   // let Vite skip pre-bundling the WASM-heavy package
  },
  server: {
    headers: {
      // Required for SharedArrayBuffer (multi-threaded ONNX WASM inference).
      // These headers enable cross-origin isolation so the WASM runtime can use
      // multiple threads via SharedArrayBuffer.
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
    },
  },
})
