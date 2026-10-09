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
    // Hidden Transcript layer fetches nothing; deck.gl loads tiles of
    // invisible layers too (l: layer visible)
    name: 'point hidden no fetch',
    find:
      'getTileData: async (v) => {\n' +
      '        const { index: G, signal: k, bbox: X, zoom: Y } = v,',
    replace:
      'getTileData: async (v) => {\n' +
      '        if (!l) return { src: { x: [], y: [], featureIndices: [] }, length: 0 };\n' +
      '        const { index: G, signal: k, bbox: X, zoom: Y } = v,',
  },
  {
    // Showing or hiding Transcript layer reloads its tiles
    name: 'point visibility reload',
    find:
      'updateTriggers: {\n        getTileData: [\n          p,\n          d,\n' +
      '          I,\n          C,\n          c,\n          m\n        ]',
    replace:
      'updateTriggers: {\n        getTileData: [\n          p,\n          d,\n' +
      '          I,\n          C,\n          c,\n          m,\n          l\n        ]',
  },
  {
    // Lookup rectangles past data's far edge clamp instead of throwing
    // 'Rectangle out of bounds' (Vitessce's own TODO; het: max code value)
    name: 'point rect clamp',
    find:
      '    Math.max(Math.floor((o - e) / s * het), 0),\n' +
      '    Math.max(Math.floor((A - i) / a * het), 0)\n',
    replace:
      '    Math.min(Math.max(Math.floor((o - e) / s * het), 0), het),\n' +
      '    Math.min(Math.max(Math.floor((A - i) / a * het), 0), het)\n',
  },
  {
    // Morton code past last row group's max bisects to one past last index
    // (l: row group count)
    name: 'point row group clamp',
    find:
      '      return g;\n    },\n    meta: { queryClient: t, store: e }\n' +
      '  });\n}\nasync function _er(',
    replace:
      '      return Math.min(g, l - 1);\n    },\n' +
      '    meta: { queryClient: t, store: e }\n  });\n}\nasync function _er(',
  },
  {
    // Transcript sub-rows only while layer is visible (`a`: per-feature rows,
    // `Ce`: genes selected, `O`: layer visible)
    name: 'point sub-rows visible',
    find: 'a && Ce ? Le(Ur, { children: [F.map((ee) => _(Xmi, {',
    replace: 'a && Ce && O ? Le(Ur, { children: [F.map((ee) => _(Xmi, {',
  },
  {
    // Served data never changes: later views skip refetching shared queries
    // (failed ones retry on mount); unused kept 10 min as gene changes reuse
    name: 'queries never stale',
    find: 'refetchOnWindowFocus: !1,',
    replace: 'refetchOnWindowFocus: !1, staleTime: 1 / 0, gcTime: 6e5,',
  },
  {
    // Gene chips: raw selection setter, so Clear keeps colour encoding
    name: 'gene chips clear',
    find:
      'setGeneSelection: be, setGeneFilter: b, setGeneHighlight: y, ' +
      'enableMultiSelect: A,',
    replace:
      'setGeneSelection: be, gatewayClearSelection: () => E(null), ' +
      'setGeneFilter: b, setGeneHighlight: y, enableMultiSelect: A,',
  },
  {
    // Gene chips between search box and table, which shrinks to fit (A:
    // selection, a: list's setter, l: multi-select, o: labels)
    name: 'gene chips',
    find:
      'onChange: p }), _(nmi, { columns: m, columnLabels: R, data: S, ' +
      'hasColorEncoding: i, idKey: "key", selectedIds: A, onChange: f, ' +
      'allowMultiple: l, allowUncheck: l, showTableHead: R.length > 1, ' +
      'width: e, height: n - 34 })',
    replace:
      'onChange: p }), _(GatewayGeneChips, { enabled: l, selection: A, ' +
      'setSelection: a, clearSelection: t.gatewayClearSelection, ' +
      'labels: o, cleanId: hS }), _(nmi, { columns: m, columnLabels: R, ' +
      'data: S, hasColorEncoding: i, idKey: "key", selectedIds: A, ' +
      'onChange: f, allowMultiple: l, allowUncheck: l, ' +
      'showTableHead: R.length > 1, width: e, ' +
      'height: n - 34 - gatewayGeneChipsHeight(l, A) })',
  },
  {
    // Set buttons: subscriber passes selection setter to sets manager
    name: 'set buttons setter',
    find: 'onCheckLevel: Y, onNodeSetColor: ne,',
    replace:
      'onCheckLevel: Y, gatewaySetObsSetSelection: F, onNodeSetColor: ne,',
  },
  {
    // Set buttons in toolbar (n: sets, A: selection, S: onCheckLevel)
    name: 'set buttons',
    find:
      '_("div", { className: he.setOperationButtons, children: _(VGn, { ' +
      'onUnion: v, onIntersection: G, onComplement: k, operatable: u, ' +
      'hasCheckedSetsToUnion: X, hasCheckedSetsToIntersect: Y, ' +
      'hasCheckedSetsToComplement: L }) })',
    replace:
      'Le("div", { className: he.setOperationButtons, children: [' +
      '_(GatewaySetButtons, { sets: n, selection: A, ' +
      'onCheckLevel: S, setSelection: t.gatewaySetObsSetSelection }), ' +
      '_(VGn, { onUnion: v, onIntersection: G, onComplement: k, ' +
      'operatable: u, hasCheckedSetsToUnion: X, ' +
      'hasCheckedSetsToIntersect: Y, hasCheckedSetsToComplement: L })] })',
  },
  {
    // Internals for gateway_views.js, imported as gateway-vitessce-internals
    // (Ps: useLoaders, Pp: useObsSetsData, js: TitleInfo, ka: useReady, Td:
    // useUrls, md: mergeObsSets, xo: coordination types by view)
    name: 'view internals',
    find: 'export {\n  AUn as $,',
    replace:
      'export {\n  Ps as gatewayUseLoaders, Pp as gatewayUseObsSetsData, ' +
      'js as GatewayTitleInfo, ka as gatewayUseReady, Td as gatewayUseUrls, ' +
      'md as gatewayMergeObsSets, xo as gatewayComponentCoordinationTypes,\n' +
      '  AUn as $,',
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
        'gatewayGeneMappingUrl, gatewayParquetWasmUrl, useGatewayPointColours, ' +
        'GatewayGeneChips, gatewayGeneChipsHeight, GatewaySetButtons';
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

// Chunk holding Vitessce's internals; same module as 'vitessce' imports, so
// one React context. Name changes with Vitessce version
const VITESSCE_CHUNK = fileURLToPath(
  new URL('./node_modules/vitessce/dist/index-CDVgyDq2.js', import.meta.url),
);

export default defineConfig({
  plugins: [patchVitessce(), nodePolyfills()],
  resolve: { alias: { 'gateway-vitessce-internals': VITESSCE_CHUNK } },
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
