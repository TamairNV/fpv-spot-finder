import { defineConfig } from 'vite';

// Relative base so the built site works from any folder or GitHub Pages path.
// MapLibre and three.js get their own chunks: they rarely change, so a redeploy of the app code
// doesn't make phones download ~1 MB of map engine again.
export default defineConfig({
  base: './',
  build: {
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules/maplibre-gl')) return 'maplibre';
          if (id.includes('node_modules/three')) return 'three';
        },
      },
    },
  },
});
