import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Vite 8 (Rolldown) + React (Oxc Fast Refresh) + Tailwind v4 (CSS-first plugin).
// See Fleet HQ's docs/STACK-TENDENCIES.md and docs/CODING-STANDARDS.md.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    port: 5195,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8013', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8013', changeOrigin: true },
    },
  },
})
