/**
 * Runtime side of build-time patches to Vitessce (see vite.config.js).
 *
 * Patches only insert calls to these functions into Vitessce's prebuilt
 * bundle, so their logic stays readable here and each patch stays one line.
 *
 * Vendored files: Vitessce otherwise imports its parquet-wasm build from
 * cdn.vitessce.io and fetches its Ensembl-to-gene-symbol table, used for gene
 * ID mapping, from data-1.vitessce.io.
 */

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
