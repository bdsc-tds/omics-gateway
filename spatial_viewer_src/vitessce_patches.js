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
 *
 * Point colours: transcript layer colours genes from featureColor, so its
 * swatches can be edited; each newly selected gene gets entry from
 * Vitessce's palette by gene position, colour its random-by-feature mode
 * gives same gene, instead of config storing one per gene.
 *
 * Gene chips: multi-select gene list gets row of selected genes, each with
 * button removing it, plus Clear, as list itself only shades selected rows.
 *
 * Set buttons: Cell Sets toolbar gets All (every set of hierarchy of last
 * ticked set, as clicking its name does) and None, which Vitessce has no
 * gesture for.
 */

import { createElement as h, useEffect, useRef } from 'react';

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

// Vitessce's PALETTE (@vitessce/utils), indexed by gene position
const POINT_PALETTE = [
  [68, 119, 170],
  [136, 204, 238],
  [68, 170, 153],
  [17, 119, 51],
  [153, 153, 51],
  [221, 204, 119],
  [204, 102, 119],
  [136, 34, 85],
  [170, 68, 153],
];

// Hook adding palette colour for selected genes still without one, from
// spatial view's point layer [values, setters] and per-layer gene index
export function useGatewayPointColours(pointCoordination, indicesData) {
  const [values, setters] = pointCoordination || [];
  useEffect(() => {
    for (const layer of Object.keys(values || {})) {
      const { obsColorEncoding, featureSelection, featureColor } =
        values[layer];
      const genes = indicesData?.[layer]?.featureIndex;
      const setFeatureColor = setters?.[layer]?.setFeatureColor;
      if (obsColorEncoding !== 'geneSelection' || !featureSelection) continue;
      if (!genes || !setFeatureColor) continue;
      const known = new Set((featureColor || []).map((entry) => entry.name));
      const added = featureSelection
        .filter((gene) => !known.has(gene) && genes.indexOf(gene) >= 0)
        .map((gene) => ({
          name: gene,
          color: POINT_PALETTE[genes.indexOf(gene) % POINT_PALETTE.length],
        }));
      if (added.length) setFeatureColor([...(featureColor || []), ...added]);
    }
  });
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

// Temporary switches from config's gatewayOptions, set by main.js
const gatewayOptions = {};

export function setGatewayOptions(options) {
  Object.assign(gatewayOptions, options);
}

// Chip row height, including margins; virtualised table below needs it
const GENE_CHIPS_HEIGHT = 30;

const CHIP_STYLE = {
  display: 'inline-flex',
  alignItems: 'center',
  flex: '0 0 auto',
  height: 22,
  marginRight: 4,
  padding: '0 2px 0 8px',
  border: '1px solid currentColor',
  borderRadius: 11,
  fontSize: 12,
  opacity: 0.85,
};

const CHIP_BUTTON_STYLE = {
  border: 0,
  padding: '0 4px',
  background: 'none',
  color: 'inherit',
  font: 'inherit',
  cursor: 'pointer',
};

function showsGeneChips(enabled, selection) {
  return (
    !!gatewayOptions.geneChips &&
    !!enabled &&
    Array.isArray(selection) &&
    selection.length > 0
  );
}

// Height taken from gene list's table by chip row
export function gatewayGeneChipsHeight(enabled, selection) {
  return showsGeneChips(enabled, selection) ? GENE_CHIPS_HEIGHT : 0;
}

// Multi-select list where plain click toggles row, Shift+click keeps only it;
// tied to chips, which show what accumulates
export function gatewayClickToggles(allowMultiple) {
  return !!gatewayOptions.geneChips && !!allowMultiple;
}

// Set by Shift+click, read once by gene list, which otherwise keeps
// selected genes hidden by search
let soloClickPending = false;

export function gatewayNoteSoloClick(isSolo) {
  soloClickPending = isSolo;
}

export function gatewayTakeSoloClick() {
  const pending = soloClickPending;
  soloClickPending = false;
  return pending;
}

// Selected genes as chips; x uses list's own setter, Clear one that leaves
// colour encoding alone
export function GatewayGeneChips({
  enabled,
  selection,
  setSelection,
  clearSelection,
  labels,
  cleanId,
}) {
  if (!showsGeneChips(enabled, selection)) return null;
  const chips = selection.map((gene) => {
    const label = labels?.get(gene) || labels?.get(cleanId(gene)) || gene;
    const remaining = selection.filter((other) => other !== gene);
    return h(
      'span',
      { key: gene, style: CHIP_STYLE, title: label },
      label,
      h(
        'button',
        {
          type: 'button',
          style: CHIP_BUTTON_STYLE,
          'aria-label': `Remove ${label} from selection`,
          onClick: () => setSelection(remaining.length ? remaining : null),
        },
        '\u00d7',
      ),
    );
  });
  const clear = h(
    'button',
    {
      key: 'clear',
      type: 'button',
      style: { ...CHIP_STYLE, ...CHIP_BUTTON_STYLE, padding: '0 8px' },
      title: 'Deselect all genes',
      onClick: () => clearSelection(),
    },
    'Clear',
  );
  return h(
    'div',
    {
      style: {
        display: 'flex',
        alignItems: 'center',
        height: GENE_CHIPS_HEIGHT,
        overflowX: 'auto',
        whiteSpace: 'nowrap',
        boxSizing: 'border-box',
        padding: '0 4px',
      },
    },
    clear,
    ...chips,
  );
}

const SET_BUTTON_STYLE = { fontSize: 12, lineHeight: '20px' };

// All and None ahead of Cell Sets' set operation buttons, sharing their style
export function GatewaySetButtons({
  sets,
  selection,
  onCheckLevel,
  setSelection,
}) {
  if (!gatewayOptions.setButtons) return null;
  const names = (sets?.tree || []).map((node) => node.name);
  // Hierarchy of last ticked set of dataset's own (ticks append; lasso
  // selections ignored), else first one
  const ticked = (selection || [])
    .map((path) => path[0])
    .filter((name) => names.includes(name));
  const hierarchy = ticked.length ? ticked[ticked.length - 1] : names[0];
  const none = !selection?.length;
  return [
    h(
      'button',
      {
        key: 'all',
        type: 'button',
        style: SET_BUTTON_STYLE,
        title: `Select all of ${hierarchy}`,
        disabled: !hierarchy || !onCheckLevel,
        onClick: () => onCheckLevel(hierarchy, 1),
      },
      'All',
    ),
    h(
      'button',
      {
        key: 'none',
        type: 'button',
        style: { ...SET_BUTTON_STYLE, opacity: none ? 0.4 : 1 },
        title: 'Deselect all sets',
        disabled: none || !setSelection,
        onClick: () => setSelection([]),
      },
      'None',
    ),
  ];
}
