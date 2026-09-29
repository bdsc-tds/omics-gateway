/**
 * Vite build config for self-hosted Vitessce viewer bundle.
 *
 * Builds main.js in library mode into ES module entry (spatial-viewer.js) plus
 * content-hashed chunks, written directly into Flask app's static directory
 * (omics_gateway/static/vitessce/), where templates/spatial_viewer.html
 * loads entry via url_for('static', ...).
 *
 * Vitessce and its Zarr/loader dependencies reference Node globals such as
 * Buffer and process, absent in browsers, so vite-plugin-node-polyfills
 * supplies browser shims for them.
 *
 * Run `npm run build` in `viewer-build` conda env (Node pinned in
 * viewer_build_env.yaml).
 */

import { defineConfig } from 'vite';
import { nodePolyfills } from 'vite-plugin-node-polyfills';

export default defineConfig({
  plugins: [nodePolyfills()],
  build: {
    outDir: '../omics_gateway/static/vitessce',
    emptyOutDir: true,
    // Single stylesheet rather than per-chunk CSS, so template has one
    // predictable file to link.
    cssCodeSplit: false,
    lib: {
      entry: 'main.js',
      formats: ['es'],
      fileName: () => 'spatial-viewer.js',
    },
  },
});
