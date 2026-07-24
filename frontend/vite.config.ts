import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

const DJANGO = 'http://127.0.0.1:8000';

// Vite needs an entry index.html, but Django renders the real shell
// (templates/app/spa_base.html). Drop the built copy so there's exactly one.
const dropEntryHtml: Plugin = {
  name: 'drop-entry-html',
  enforce: 'post',
  apply: 'build',
  generateBundle(_options, bundle) {
    delete bundle['index.html'];
  },
};

// The build is committed to the repo and served by Django from
// project/app/static/frontend/, so `manage.py runserver` alone runs the whole
// app with no Node installed. Filenames are fixed (no content hash) so the
// Django shell template can reference them with a plain {% static %} tag
// instead of parsing a Vite manifest.
export default defineConfig({
  plugins: [react(), dropEntryHtml],
  base: '/static/frontend/',
  build: {
    outDir: '../project/app/static/frontend',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/index.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/index[extname]',
      },
    },
  },
  server: {
    proxy: {
      '/api': DJANGO,
      // Dev-only. Under `npm run dev` the HTML shell is served by Vite, so
      // Django's @ensure_csrf_cookie view never runs and no csrftoken cookie
      // exists for the first POST. api/client.ts GETs this path to mint one.
      '/__csrf': { target: DJANGO, rewrite: () => '/' },
    },
  },
});
