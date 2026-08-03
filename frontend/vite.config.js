import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE_PATH || '/',
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
