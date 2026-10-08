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
 * patchVitessce() edits Vitessce's prebuilt bundle as it is read: each patch
 * replaces one exact snippet with call into vitessce_patches.js. Snippets
 * hold minified names specific to this Vitessce version, so build fails
 * unless each matches exactly once; after upgrade, find new snippets in
 * node_modules/vitessce/dist/ (README, "Rebuilding the spatial viewer").
 *
 * Run `npm run build` in `viewer-build` conda env (Node pinned in
 * viewer_build_env.yaml).
 */

import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import { nodePolyfills } from 'vite-plugin-node-polyfills';

const PATCH_MODULE = fileURLToPath(
  new URL('./vitessce_patches.js', import.meta.url),
);

const PATCHES = [
  {
    // Vendored copy instead of cdn.vitessce.io
    name: 'parquet-wasm',
    find: '"https://cdn.vitessce.io/parquet-wasm@2c23652/esm/parquet_wasm.js"',
    replace: '/* @vite-ignore */ gatewayParquetWasmUrl()',
  },
  {
    // Vendored copy instead of data-1.vitessce.io
    name: 'gene mapping',
    find: 'Vir = "https://data-1.vitessce.io/genes_filtered.json"',
    replace: 'Vir = gatewayGeneMappingUrl()',
  },
  {
    // Dot plot height grows with genes (f: dot plot data)
    name: 'dot plot height',
    find: 'Z = n ? Dl(A - E - b - 50, 10, 1 / 0) :',
    replace: 'Z = n ? gatewayDotPlotHeight(Dl(A - E - b - 50, 10, 1 / 0), f) :',
  },
  {
    // Matrix retry: zarr arrays of sparse X, opened once per loader
    name: 'matrix arrays retry',
    find: '{ kind: "array" }))), this.sparseArrays);',
    replace:
      '{ kind: "array" }))), gatewayForgetOnReject(this, "sparseArrays"));',
  },
  {
    // Matrix retry: dense copy of CSR X, built on first gene selection
    name: 'matrix CSR retry',
    find:
      's[a * i[1] + d] = u;\n        }\n        a += 1;\n' +
      '      }), s;\n    }), this._sparseMatrix);',
    replace:
      's[a * i[1] + d] = u;\n        }\n        a += 1;\n' +
      '      }), s;\n    }), gatewayForgetOnReject(this, "_sparseMatrix"));',
  },
  {
    // Point colours in beta spatial view (ft: point layer coordination, Ke:
    // point gene indices)
    name: 'point colours',
    find: '[Ke, it, Ct] = Nfe(f, S, B, p),',
    replace:
      '[Ke, it, Ct] = Nfe(f, S, B, p), ' +
      'gatewayPointColoursDone = useGatewayPointColours(ft, Ke),',
  },
  {
    // Transcript sub-rows only while layer is visible (a: per-feature rows,
    // Ce: genes selected, O: layer visible)
    name: 'point sub-rows visible',
    find: 'a && Ce ? Le(Ur, { children: [F.map((ee) => _(Xmi, {',
    replace: 'a && Ce && O ? Le(Ur, { children: [F.map((ee) => _(Xmi, {',
  },
  {
    // Auto-fill in beta spatial view (Xe: segmentation channel coordination)
    name: 'auto-fill',
    find: 'f, S, ie.SEGMENTATION_LAYER, ie.SEGMENTATION_CHANNEL), He = pl([',
    replace:
      'f, S, ie.SEGMENTATION_LAYER, ie.SEGMENTATION_CHANNEL), ' +
      'gatewayAutoFillDone = useGatewayAutoFill(Xe), He = pl([',
  },
];

// Function to apply PATCHES to Vitessce's prebuilt bundle
function patchVitessce() {
  const applied = new Set();
  return {
    name: 'patch-vitessce',
    enforce: 'pre',
    transform(code, id) {
      if (!id.includes('/node_modules/vitessce/dist/')) return null;
      let patched = code;
      for (const patch of PATCHES) {
        const count = patched.split(patch.find).length - 1;
        if (count === 0) continue;
        if (count > 1) {
          this.error(`Vitessce patch '${patch.name}' matches ${count} times`);
        }
        patched = patched.replace(patch.find, () => patch.replace);
        applied.add(patch.name);
      }
      if (patched === code) return null;
      const names =
        'useGatewayAutoFill, gatewayDotPlotHeight, gatewayForgetOnReject, ' +
        'gatewayGeneMappingUrl, gatewayParquetWasmUrl, useGatewayPointColours';
      const header = `import { ${names} } from ${JSON.stringify(PATCH_MODULE)};\n`;
      return { code: header + patched, map: null };
    },
    buildEnd() {
      const missing = PATCHES.filter((patch) => !applied.has(patch.name));
      if (missing.length) {
        const list = missing.map((patch) => patch.name).join(', ');
        this.error(`Vitessce patches not applied: ${list}`);
      }
    },
  };
}

export default defineConfig({
  plugins: [patchVitessce(), nodePolyfills()],
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
