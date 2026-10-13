/**
 * Custom Vitessce views registered by main.js as plugin view types.
 *
 * Cell type counts: composition by first cell set hierarchy of up to two
 * selected sets outside it (lasso regions, other groupings), side by side,
 * or of whole selection; Vitessce shows this nowhere, as Cell Set Sizes
 * only plots whole sets.
 *
 * Data hooks and TitleInfo are not exported by 'vitessce';
 * 'gateway-vitessce-internals' is Vitessce's own chunk with them re-exported
 * (see vite.config.js). @vitessce/* packages cannot stand in: they are
 * separate copies whose React contexts Vitessce never provides.
 */

import { createElement as h, useMemo } from 'react';
import {
  PluginViewType,
  useCoordination,
  useCoordinationScopes,
} from 'vitessce';
import {
  GatewayTitleInfo,
  gatewayMergeObsSets,
  gatewayUseLoaders,
  gatewayUseObsSetsData,
  gatewayUseReady,
  gatewayUseUrls,
} from 'gateway-vitessce-internals';

const COUNTS_COORDINATION_TYPES = [
  'dataset',
  'obsType',
  'obsSetSelection',
  'obsSetColor',
  'additionalObsSets',
];

// Cell ids of every leaf at or below node
function collectIds(node, out) {
  if (node.children) {
    node.children.forEach((child) => {
      collectIds(child, out);
    });
  } else {
    (node.set || []).forEach(([id]) => {
      out.add(id);
    });
  }
  return out;
}

// Node at name path, from list of top-level nodes
function findNode(tree, path) {
  let nodes = tree;
  let node = null;
  for (const name of path) {
    node = (nodes || []).find((child) => child.name === name);
    if (!node) return null;
    nodes = node.children;
  }
  return node;
}

// Selected sets compared side by side; more would crowd panel
const MAX_COLUMNS = 2;

// Cell counts per cell type (first hierarchy's sets), one column per selected
// set outside it, else one for whole selection (cell types only match self)
function countCellTypes(obsSets, additionalObsSets, selection, colours) {
  const hierarchy = obsSets?.tree?.[0];
  if (!hierarchy || !selection?.length) return null;
  const typeOf = new Map();
  (hierarchy.children || []).forEach((type) => {
    collectIds(type, new Set()).forEach((id) => {
      typeOf.set(id, type.name);
    });
  });
  const merged = gatewayMergeObsSets(obsSets, additionalObsSets);
  const idsOf = (paths) => {
    const ids = new Set();
    paths.forEach((path) => {
      const node = findNode(merged.tree, path);
      if (node) collectIds(node, ids);
    });
    return ids;
  };
  const others = selection.filter((path) => path[0] !== hierarchy.name);
  const sources = others.length
    ? others.slice(0, MAX_COLUMNS).map((path) => ({
        label: path[path.length - 1],
        ids: idsOf([path]),
      }))
    : [{ label: 'Selected cells', ids: idsOf(selection) }];
  const columns = sources.map(({ label, ids }) => {
    const counts = new Map();
    ids.forEach((id) => {
      const type = typeOf.get(id) ?? 'Unassigned';
      counts.set(type, (counts.get(type) || 0) + 1);
    });
    return { label, total: ids.size, counts };
  });
  const colourOf = (type) =>
    colours?.find(
      (entry) =>
        entry.path.length === 2 &&
        entry.path[0] === hierarchy.name &&
        entry.path[1] === type,
    )?.color;
  const first = columns[0].counts;
  const types = [
    ...new Set(columns.flatMap(({ counts }) => [...counts.keys()])),
  ];
  const rows = types
    .map((type) => ({ type, colour: colourOf(type) }))
    .sort(
      (a, b) =>
        (first.get(b.type) || 0) - (first.get(a.type) || 0) ||
        a.type.localeCompare(b.type),
    );
  return {
    hierarchy: hierarchy.name,
    columns,
    rows,
    hidden: Math.max(0, others.length - MAX_COLUMNS),
  };
}

const CELL_STYLE = { padding: '1px 6px', whiteSpace: 'nowrap' };
const NUMBER_STYLE = { ...CELL_STYLE, textAlign: 'right' };
// Bar opacity per column, so two columns' bars can be told apart
const BAR_OPACITY = [0.85, 0.4];

function bar(share, fill, opacity) {
  return h('div', {
    style: {
      width: `${share}%`,
      height: 5,
      margin: '1px 0',
      background: fill,
      opacity,
    },
  });
}

function countsTable(result) {
  const { columns } = result;
  const header = h(
    'tr',
    null,
    h('th', { style: { ...CELL_STYLE, textAlign: 'left' } }, result.hierarchy),
    columns.map((column, index) =>
      h(
        'th',
        { key: column.label, style: NUMBER_STYLE, title: column.label },
        columns.length > 1
          ? h('span', {
              style: {
                display: 'inline-block',
                width: 12,
                height: 5,
                marginRight: 4,
                verticalAlign: 'middle',
                background: 'currentColor',
                opacity: BAR_OPACITY[index],
              },
            })
          : null,
        column.label,
      ),
    ),
    h('th', { style: { ...CELL_STYLE, width: '35%' } }),
  );
  const rows = result.rows.map(({ type, colour }) => {
    const fill = colour ? `rgb(${colour.join(',')})` : 'currentColor';
    const shares = columns.map(
      ({ counts, total }) => (100 * (counts.get(type) || 0)) / (total || 1),
    );
    return h(
      'tr',
      { key: type },
      h(
        'td',
        { style: CELL_STYLE },
        h('span', {
          style: {
            display: 'inline-block',
            width: 10,
            height: 10,
            marginRight: 6,
            background: fill,
          },
        }),
        type,
      ),
      columns.map(({ label, counts }, index) =>
        h(
          'td',
          { key: label, style: NUMBER_STYLE },
          `${(counts.get(type) || 0).toLocaleString('en')} ` +
            `(${shares[index].toFixed(1)}%)`,
        ),
      ),
      h(
        'td',
        { style: CELL_STYLE },
        shares.map((share, index) =>
          h(
            'div',
            { key: columns[index].label },
            bar(share, fill, BAR_OPACITY[index]),
          ),
        ),
      ),
    );
  });
  const note = result.hidden
    ? h(
        'div',
        { style: { padding: '2px 6px' } },
        `Showing first ${columns.length} of ` +
          `${columns.length + result.hidden} selections.`,
      )
    : null;
  return h(
    'div',
    { style: { fontSize: 12 } },
    note,
    h(
      'table',
      { style: { width: '100%', borderCollapse: 'collapse' } },
      h('thead', null, header),
      h('tbody', null, rows),
    ),
  );
}

// Breakdown of selected cells (lasso region or sets) by cell type
function CellTypeCounts(props) {
  const {
    coordinationScopes: rawScopes,
    theme,
    removeGridComponent,
    closeButtonVisible,
    downloadButtonVisible,
    title = 'Cell Types in Selection',
  } = props;
  const loaders = gatewayUseLoaders();
  const scopes = useCoordinationScopes(rawScopes);
  const [
    { dataset, obsType, obsSetSelection, obsSetColor, additionalObsSets },
  ] = useCoordination(COUNTS_COORDINATION_TYPES, scopes);
  const [{ obsSets }, status, urls, error] = gatewayUseObsSetsData(
    loaders,
    dataset,
    true,
    {},
    {},
    { obsType },
  );
  const isReady = gatewayUseReady([status]);
  const allUrls = gatewayUseUrls([urls]);
  const result = useMemo(
    () =>
      countCellTypes(obsSets, additionalObsSets, obsSetSelection, obsSetColor),
    [obsSets, additionalObsSets, obsSetSelection, obsSetColor],
  );
  const body = result
    ? countsTable(result)
    : h(
        // Plain span, as dot plot's empty message, so card font applies
        'span',
        null,
        'Draw a region with the lasso, or select cell sets, to see their ' +
          'cell types.',
      );
  return h(
    GatewayTitleInfo,
    {
      title,
      info: result
        ? `${result.columns
            .map(({ total }) => total.toLocaleString('en'))
            .join(' vs ')} cells`
        : null,
      theme,
      isScroll: true,
      closeButtonVisible,
      downloadButtonVisible,
      removeGridComponent,
      isReady,
      urls: allUrls,
      errors: [error],
    },
    body,
  );
}

export const GATEWAY_VIEW_TYPES = [
  new PluginViewType(
    'gatewayCellTypeCounts',
    CellTypeCounts,
    COUNTS_COORDINATION_TYPES,
  ),
];
