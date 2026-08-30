import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['icon-180.png', 'icon-192.png', 'icon-512.png'],
      manifest: {
        name: 'AI Control',
        short_name: 'AI Control',
        description: 'Remote command center for AI coding agents',
        theme_color: '#0b0d10',
        background_color: '#0b0d10',
        display: 'standalone',
        orientation: 'any',
        start_url: '/',
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        // The app shell is cached so the iPad shows the UI (and cached session
        // metadata) when the Mac is unreachable. API responses are never cached:
        // stale agent state is worse than an honest "offline".
        globPatterns: ['**/*.{js,css,html,png,svg,woff2}'],
        navigateFallbackDenylist: [/^\/api/],
      },
    }),
  ],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8787', changeOrigin: true, ws: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false, target: 'es2020' },
  test: { environment: 'jsdom', globals: true, setupFiles: ['src/__tests__/setup.ts'] },
})
