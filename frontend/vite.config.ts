import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies /api and /health to the backend so the app uses same-origin
// relative URLs everywhere. That keeps the production path (FastAPI serving the built
// bundle, DK_SERVE_FRONTEND) byte-identical to development, and means no base URL has to
// be baked into the bundle at build time.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Loopback only, matching the API's own default. This app reads a personal knowledge
    // base; it has no business being reachable from the local network.
    host: '127.0.0.1',
    proxy: {
      '/api': { target: 'http://127.0.0.1:8787', changeOrigin: false },
      '/health': { target: 'http://127.0.0.1:8787', changeOrigin: false },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
