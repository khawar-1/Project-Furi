import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(__dirname, './src'),
    },
  },
  optimizeDeps: {
    // onnxruntime-web (wake word) resolves its .wasm binary via
    // `new URL(..., import.meta.url)`. Vite's dep pre-bundling relocates the
    // JS into node_modules/.vite/deps/, where no .wasm exists — the dev
    // server's SPA fallback then serves index.html and WebAssembly aborts
    // with "expected magic word 00 61 73 6d, found 3c 21 64 6f" (<!do —
    // live failure 2026-07-16). Excluded, the package's own ESM is served
    // from node_modules via /@fs/ and the URL resolves to the real file.
    // Build output is unaffected (optimizeDeps is dev-only).
    exclude: ['onnxruntime-web'],
  },
  worker: {
    // The wake-word worker dynamic-imports onnxruntime-web, so its bundle is
    // code-split — Vite's default `iife` worker format can't do that. ES-module
    // workers can, and Electron's Chromium supports `new Worker(url, {type:
    // 'module'})`.
    format: 'es',
  },
  server: {
    port: 5173,
    strictPort: true,
    // Proxy API calls to the FastAPI backend in dev
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom', 'zustand'],
          markdown: ['react-markdown', 'remark-gfm'],
        },
      },
    },
  },
});
