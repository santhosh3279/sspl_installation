import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// One app, one bundle, fixed file names: templates/dashboard.html loads it with
// ?v=<panel version>. Keep it one chunk: a second chunk would import app.js
// without the ?v and run the app twice.
const devMarker = {
  name: 'panel-dev-marker',
  apply: 'serve',
  transformIndexHtml: () => [{
    tag: 'div', injectTo: 'body', children: 'DEV (Vite): fake data, no commands run',
    attrs: { style: 'position:fixed;top:6px;left:50%;transform:translateX(-50%);z-index:9999;background:#b45309;'
      + 'color:#fff;padding:2px 10px;border-radius:4px;font:12px sans-serif;pointer-events:none' },
  }],
}

export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/static/app/' : '/',
  plugins: [vue(), devMarker],
  build: {
    outDir: '../static/app',          // committed: servers have no Node
    emptyOutDir: true,
    rollupOptions: {
      input: { app: fileURLToPath(new URL('./index.html', import.meta.url)) },
      output: { entryFileNames: 'assets/app.js', assetFileNames: 'assets/[name][extname]' },
    },
  },
  server: {
    host: '0.0.0.0',                  // reachable from the LAN: fake data only
    port: 5173, strictPort: true,
    // Everything but the app itself comes from dev.py. Never a real panel.
    proxy: {
      '^/(install|auth|ram|login|logout|api/|download|upload|guide/|static/)':
        `http://127.0.0.1:${process.env.SSPL_DEV_PORT || 8091}`,
    },
  },
}))
