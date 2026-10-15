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
 *
 * Click toggles: plain click on multi-select list row adds or removes it,
 * Shift+click keeps only that row, genes hidden by search included.
 *
 * Row-group extents: to find row groups covering transcript tile, Vitessce
 * bisects them, downloading each probed row group whole for its first and
 * last Morton code, one after another. Same min/max sit in Parquet footer
 * statistics, already downloaded but unexposed by parquet-wasm, so they are
 * decoded here; Vitessce's own path runs whenever they are missing.
 *
 * Scale bar: beta spatial view reads one world unit as 1 µm, while gateway
 * stores use image pixels, so length comes from config's
 * gatewayOptions.micronsPerUnit (set by main.js) and bar is hidden without
 * it. Viv draws bar light grey, unreadable on H&E; it is drawn black on
 * translucent white box instead, readable on dark and light images alike.
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

// Microns per world unit of spatial view; null hides scale bar
let micronsPerUnit = null;

// Store config's microns per world unit; anything but positive number clears
export function setGatewayMicronsPerUnit(value) {
  micronsPerUnit = typeof value === 'number' && value > 0 ? value : null;
}

// Microns per world unit for scale bar, or null when config gives none
export function gatewayMicronsPerUnit() {
  return micronsPerUnit;
}

const SCALE_BAR_COLOUR = [0, 0, 0];
const SCALE_BAR_BOX = [255, 255, 255, 110];
// Box margin around bar and label, and label glyph size, in screen pixels
const SCALE_BAR_PADDING = 8;
const SCALE_BAR_LABEL_SIZE = 12;
const SCALE_BAR_CHAR_WIDTH = 0.6 * SCALE_BAR_LABEL_SIZE;

// Viv's scale bar layers (bar, two end ticks, label) redrawn dark on light
// box. Box is copy of bar's line layer, as thick as box is tall, since patch
// has no polygon layer class at hand; extents come from layers' own data
export function gatewayScaleBarLayers(layers) {
  const [bar, leftTick, rightTick, label] = layers;
  // Label is sized in world units scaled by 2^-zoom: one screen pixel
  const pixel = label.props.sizeScale;
  const [[barLeft], [barRight]] = bar.props.data[0];
  const tickTop = Math.min(...leftTick.props.data[0].map((point) => point[1]));
  const { text, position } = label.props.data[0];
  const halfText = (text.length * SCALE_BAR_CHAR_WIDTH * pixel) / 2;
  const pad = SCALE_BAR_PADDING * pixel;
  const left = Math.min(barLeft, position[0] - halfText) - pad;
  const right = Math.max(barRight, position[0] + halfText) + pad;
  const top = tickTop - pad;
  const bottom = position[1] + (SCALE_BAR_LABEL_SIZE / 2) * pixel + pad;
  const middle = (top + bottom) / 2;
  return [
    bar.clone({
      id: `${bar.id}-box`,
      data: [
        [
          [left, middle],
          [right, middle],
        ],
      ],
      getWidth: (bottom - top) / pixel,
      getColor: SCALE_BAR_BOX,
    }),
    ...[bar, leftTick, rightTick].map((line) =>
      line.clone({ getColor: SCALE_BAR_COLOUR }),
    ),
    label.clone({ getColor: [...SCALE_BAR_COLOUR, 255] }),
  ];
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
  return !!enabled && Array.isArray(selection) && selection.length > 0;
}

// Height taken from gene list's table by chip row
export function gatewayGeneChipsHeight(enabled, selection) {
  return showsGeneChips(enabled, selection) ? GENE_CHIPS_HEIGHT : 0;
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

// Thrift compact protocol type ids (Parquet footer encoding)
const THRIFT_TRUE = 1;
const THRIFT_FALSE = 2;
const THRIFT_BYTE = 3;
const THRIFT_I16 = 4;
const THRIFT_I32 = 5;
const THRIFT_I64 = 6;
const THRIFT_DOUBLE = 7;
const THRIFT_BINARY = 8;
const THRIFT_LIST = 9;
const THRIFT_SET = 10;
const THRIFT_MAP = 11;
const THRIFT_STRUCT = 12;

// Cursor over footer bytes; integers as Numbers, exact up to 2^53
function thriftReader(bytes) {
  const reader = {
    pos: 0,
    byte() {
      if (reader.pos >= bytes.length) throw new Error('Footer ended early');
      return bytes[reader.pos++];
    },
    varint() {
      let value = 0;
      let scale = 1;
      let byte;
      do {
        byte = reader.byte();
        value += (byte & 0x7f) * scale;
        scale *= 128;
      } while (byte & 0x80);
      return value;
    },
    zigzag() {
      const value = reader.varint();
      return value % 2 ? -(value + 1) / 2 : value / 2;
    },
    bytes(length) {
      if (reader.pos + length > bytes.length) {
        throw new Error('Footer ended early');
      }
      reader.pos += length;
      return bytes.subarray(reader.pos - length, reader.pos);
    },
  };
  return reader;
}

// Booleans are field types outside collections, one byte each inside them
function readThriftValue(reader, type, inCollection) {
  switch (type) {
    case THRIFT_TRUE:
    case THRIFT_FALSE:
      return inCollection
        ? reader.byte() === THRIFT_TRUE
        : type === THRIFT_TRUE;
    case THRIFT_BYTE:
      return reader.byte();
    case THRIFT_I16:
    case THRIFT_I32:
    case THRIFT_I64:
      return reader.zigzag();
    case THRIFT_DOUBLE:
      return reader.bytes(8);
    case THRIFT_BINARY:
      return reader.bytes(reader.varint());
    case THRIFT_LIST:
    case THRIFT_SET: {
      const header = reader.byte();
      const size = header >> 4 === 15 ? reader.varint() : header >> 4;
      return Array.from({ length: size }, () =>
        readThriftValue(reader, header & 0x0f, true),
      );
    }
    case THRIFT_MAP: {
      const size = reader.varint();
      const types = size ? reader.byte() : 0;
      return Array.from({ length: size }, () => [
        readThriftValue(reader, types >> 4, true),
        readThriftValue(reader, types & 0x0f, true),
      ]);
    }
    case THRIFT_STRUCT:
      return readThriftStruct(reader);
    default:
      throw new Error(`Unknown Thrift type ${type}`);
  }
}

// Struct as object keyed by Thrift field id
function readThriftStruct(reader) {
  const fields = {};
  let fieldId = 0;
  for (;;) {
    const header = reader.byte();
    if ((header & 0x0f) === 0) return fields;
    fieldId = header >> 4 ? fieldId + (header >> 4) : reader.zigzag();
    fields[fieldId] = readThriftValue(reader, header & 0x0f, false);
  }
}

// Decoded FileMetaData per footer; Vitessce caches footer bytes for good
const decodedFooters = new WeakMap();

// Plain-encoded statistic as value of Arrow's type for column (Numbers for
// 32-bit, BigInts for 64-bit, as row group's own column would return)
function decodeStatistic(bytes, type) {
  if (!bytes || typeof type?.isSigned !== 'boolean') return undefined;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (type.bitWidth === 32 && bytes.length === 4) {
    return type.isSigned ? view.getInt32(0, true) : view.getUint32(0, true);
  }
  if (type.bitWidth === 64 && bytes.length === 8) {
    return type.isSigned
      ? view.getBigInt64(0, true)
      : view.getBigUint64(0, true);
  }
  return undefined;
}

// Min and max of column in one row group of one part, or null if footer
// lacks them; numbers are Thrift field ids of Parquet's footer structs
function partRowGroupExtent(part, columnName, index) {
  let footer = decodedFooters.get(part.schemaBytes);
  if (!footer) {
    footer = readThriftStruct(thriftReader(part.schemaBytes));
    decodedFooters.set(part.schemaBytes, footer);
  }
  // FileMetaData 4: row groups; RowGroup 1: column chunks, 3: row count
  const rowGroup = footer[4]?.[index];
  if (rowGroup?.[3] === 0) return { min: null, max: null };
  const decoder = new TextDecoder();
  // ColumnChunk 3: metadata; ColumnMetaData 3: column path, 12: statistics
  const chunk = rowGroup?.[1]?.find(
    (candidate) =>
      candidate[3]?.[3]?.map((name) => decoder.decode(name)).join('.') ===
      columnName,
  );
  const statistics = chunk?.[3]?.[12];
  // Statistics 3: null count, 5: max, 6: min
  if (statistics?.[3] !== 0) return null;
  const field = part.schema.schema.fields.find(
    (candidate) => candidate.name === columnName,
  );
  const min = decodeStatistic(statistics[6], field?.type);
  const max = decodeStatistic(statistics[5], field?.type);
  return min === undefined || max === undefined ? null : { min, max };
}

let extentFallbackLogged = false;

// Extent of row group across parts (metadata: Vitessce's per-part footers),
// or null so Vitessce downloads row group as before
export function gatewayRowGroupExtent(metadata, columnName, rowGroupIndex) {
  let extent = null;
  let failure = '';
  try {
    let first = 0;
    for (const part of metadata.parts) {
      const count = part.metadata.numRowGroups();
      if (rowGroupIndex < first + count) {
        extent = partRowGroupExtent(part, columnName, rowGroupIndex - first);
        break;
      }
      first += count;
    }
  } catch (error) {
    failure = error;
  }
  if (!extent && !extentFallbackLogged) {
    extentFallbackLogged = true;
    console.warn(
      `No footer statistics for ${columnName}, row groups load whole`,
      failure,
    );
  }
  return extent;
}
