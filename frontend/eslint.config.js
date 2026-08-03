import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['vite.config.js', 'scripts/**/*.mjs'],
    languageOptions: { globals: globals.node },
  },
  {
    // Injected at build time by vite's `define` (see vite.config.js).
    files: ['src/whisper-worker.js'],
    languageOptions: { globals: { __ORT_VERSION__: 'readonly' } },
  },
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    rules: {
      // React Compiler diagnostics — only meaningful when building with
      // babel-plugin-react-compiler, which this app does not use. They flag legitimate
      // patterns here (window.location reads, local reassignment, data-fetch-on-mount),
      // so turn them off while keeping the valuable rules-of-hooks + exhaustive-deps.
      'react-hooks/immutability': 'off',
      'react-hooks/purity': 'off',
      'react-hooks/set-state-in-effect': 'off',
    },
  },
])
