/**
 * Runtime side of build-time patches to Vitessce (see vite.config.js).
 *
 * Patches only insert calls to these functions into Vitessce's prebuilt
 * bundle, so their logic stays readable here and each patch stays one line.
 *
 * Vendored files: Vitessce otherwise imports its parquet-wasm build from
 * cdn.vitessce.io and fetches its Ensembl-to-gene-symbol table, used for gene
 * ID mapping, from data-1.vitessce.io.
 *
 * Dot plot: Vitessce keeps transposed plot's height fixed (panel minus label
 * margins) while circle size stays constant, so each added gene squeezed rows
 * until circles overlapped; height now grows by DOT_PLOT_ROW_HEIGHT per gene.
 *
 * Matrix retry: AnnData loader caches CSR matrix promise on first gene
 * selection, rejected ones included, so one failed fetch broke every later
 * selection until reload; dropping rejected promise lets next one fetch again.
 *
 * Auto-fill: segmentation channels are filled when they switch to gene
 * colouring and unfilled when they switch away. Acting on transitions only
 * leaves Filled ticked or unticked by hand until next switch, and respects
 * initial config.
 */

import { useEffect, useRef } from 'react';

// Relative to built chunk in static/vitessce/; variables, not literals, so
// Vite leaves them for runtime instead of bundling them as assets
const PARQUET_WASM_PATH = '../vendor/parquet-wasm-2c23652/parquet_wasm.js';

// Absolute URL of vendored parquet_wasm.js; its wasm file loads relative to it
export function gatewayParquetWasmUrl() {
  return new URL(PARQUET_WASM_PATH, import.meta.url).href;
}

const GENE_MAPPING_PATH =
  '../vendor/vitessce-data-2025-06-16/genes_filtered.json';

// Absolute URL of vendored genes_filtered.json
export function gatewayGeneMappingUrl() {
  return new URL(GENE_MAPPING_PATH, import.meta.url).href;
}

// Vega-Lite's default step for discrete axes, which also sizes dot plot's
// largest circle (19 px measured), so rows this tall never overlap
const DOT_PLOT_ROW_HEIGHT = 20;

// Dot plot height (genes as rows) grown from Vitessce's defaultHeight
export function gatewayDotPlotHeight(defaultHeight, rows) {
  const genes = new Set(rows.map((row) => row.keyFeature)).size;
  return Math.max(defaultHeight, genes * DOT_PLOT_ROW_HEIGHT);
}

// Cached load promise, dropped if it rejects so next call fetches again
export function gatewayForgetOnReject(owner, key) {
  const promise = owner[key];
  promise.catch(() => {
    if (owner[key] === promise) owner[key] = undefined;
  });
  return promise;
}

// Gene colouring needs filled polygons to be readable; outlines suit sets
function isGeneColoured(values) {
  return (
    values?.obsColorEncoding === 'geneSelection' &&
    values.featureSelection?.length > 0
  );
}

// Hook filling channels on gene colouring, from spatial view's
// [values, setters] by layer and channel scope
export function useGatewayAutoFill(channelCoordination) {
  const [values, setters] = channelCoordination;
  const previous = useRef({});
  useEffect(() => {
    for (const layer of Object.keys(values || {})) {
      for (const channel of Object.keys(values[layer] || {})) {
        const key = `${layer}/${channel}`;
        const now = isGeneColoured(values[layer][channel]);
        const before = previous.current[key];
        previous.current[key] = now;
        if (before === undefined || before === now) continue;
        setters?.[layer]?.[channel]?.setSpatialSegmentationFilled?.(now);
      }
    }
  });
}
