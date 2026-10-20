/**
 * Custom Vitessce views registered by main.js as plugin view types.
 *
 * Cell type counts: composition by cell-type list (first hierarchy, or first
 * of rowHierarchies prop with ticked sets) of up to two selected sets
 * outside it (lasso regions, other groupings), side by side, or of whole
 * selection, listing list's every type (absent ones as 0, behind toggle);
 * Vitessce shows this nowhere, as Cell Set Sizes only plots whole sets.
 *
 * Tabs: two or more existing views sharing one grid slot, one shown at
 * once, switched by buttons after shown view's title. Grid passes every view
 * registered view types, so inner components come from same bundle copy.
 * Each tab keeps own coordination scopes, from view's props, over tab view's
 * auto-initialised ones.
 *
 * Sample thumbnails: study config's samples as static H&E images made by
 * config generator (live spatial views would load every sample at once);
 * clicking one switches to that sample's config.
 *
 * Data hooks, TitleInfo and coordination type lists are not exported by
 * 'vitessce'; 'gateway-vitessce-internals' is Vitessce's own chunk with
 * them re-exported (see vite.config.js). @vitessce/* packages cannot stand
 * in: they are separate copies whose React contexts Vitessce never provides.
 */

import {
  createElement as h,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import {
  PluginViewType,
  useCoordination,
  useCoordinationScopes,
} from 'vitessce';
import {
  GatewayTitleInfo,
  gatewayComponentCoordinationTypes,
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

// Views offered as tabs; tab view needs union of their coordination types
const TAB_VIEW_TYPES = [
  'dotPlot',
  'obsSetFeatureValueDistribution',
  'scatterplot',
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

// Counts per type of first cell-type list with ticked sets, one column per set
// outside it, else whole selection; other samples' cells are skipped
function countCellTypes(
  obsSets,
  additionalObsSets,
  selection,
  colours,
  rowHierarchies,
) {
  const tree = obsSets?.tree || [];
  const lists = tree.filter((node) => rowHierarchies?.includes(node.name));
  if (!lists.length && tree[0]) lists.push(tree[0]);
  if (!lists.length || !selection?.length) return null;
  const hierarchy =
    lists.find((list) => selection.some((path) => path[0] === list.name)) ||
    lists[0];
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
  const outside = new Set();
  const columns = sources.map(({ label, ids }) => {
    const counts = new Map();
    let total = 0;
    ids.forEach((id) => {
      const type = typeOf.get(id);
      if (type === undefined) {
        outside.add(id);
        return;
      }
      counts.set(type, (counts.get(type) || 0) + 1);
      total += 1;
    });
    return { label, total, counts };
  });
  const colourOf = (type) =>
    colours?.find(
      (entry) =>
        entry.path.length === 2 &&
        entry.path[0] === hierarchy.name &&
        entry.path[1] === type,
    )?.color;
  const first = columns[0].counts;
  // Every type of list, absent ones as 0, as when all types are ticked
  const types = (hierarchy.children || []).map((type) => type.name);
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
    outside: outside.size,
  };
}

const CELL_STYLE = { padding: '1px 6px', whiteSpace: 'nowrap' };
const NUMBER_STYLE = { ...CELL_STYLE, textAlign: 'right' };
// Bar opacity per column, so two columns' bars can be told apart
const BAR_OPACITY = [0.85, 0.4];
const TOGGLE_STYLE = {
  padding: '2px 6px',
  border: 'none',
  background: 'none',
  color: 'inherit',
  font: 'inherit',
  opacity: 0.7,
  textDecoration: 'underline',
  cursor: 'pointer',
};

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

function countsTable(result, showEmpty, toggleEmpty) {
  const { columns } = result;
  // Small lasso leaves most types at 0 in every column, burying others
  const isEmpty = ({ type }) =>
    columns.every(({ counts }) => !counts.get(type));
  const empty = result.rows.filter(isEmpty).length;
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
  const rows = result.rows
    .filter((row) => showEmpty || !isEmpty(row))
    .map(({ type, colour }) => {
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
  const notes = [
    result.hidden
      ? `Showing first ${columns.length} of ` +
        `${columns.length + result.hidden} selections.`
      : null,
    result.outside
      ? `${result.outside.toLocaleString('en')} selected cells from other ` +
        'samples not counted.'
      : null,
  ]
    .filter(Boolean)
    // Card's own div rule otherwise sets 16px, above table's size
    .map((text) =>
      h(
        'div',
        { key: text, style: { padding: '2px 6px', fontSize: 'inherit' } },
        text,
      ),
    );
  return h(
    'div',
    { style: { fontSize: 12 } },
    notes,
    h(
      'table',
      { style: { width: '100%', borderCollapse: 'collapse' } },
      h('thead', null, header),
      h('tbody', null, rows),
    ),
    empty
      ? h(
          'button',
          { type: 'button', style: TOGGLE_STYLE, onClick: toggleEmpty },
          `${showEmpty ? 'Hide' : 'Show'} ${empty} ` +
            `${empty === 1 ? 'type' : 'types'} with no cells`,
        )
      : null,
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
    rowHierarchies,
  } = props;
  const [showEmpty, setShowEmpty] = useState(false);
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
      countCellTypes(
        obsSets,
        additionalObsSets,
        obsSetSelection,
        obsSetColor,
        rowHierarchies,
      ),
    [obsSets, additionalObsSets, obsSetSelection, obsSetColor, rowHierarchies],
  );
  const body = result
    ? countsTable(result, showEmpty, () => setShowEmpty(!showEmpty))
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

const SWITCH_STYLE = {
  marginLeft: 8,
  padding: '0 8px',
  border: '1px solid currentColor',
  borderRadius: 10,
  background: 'none',
  color: 'inherit',
  font: 'inherit',
  fontSize: '0.85em',
  lineHeight: 1.4,
  opacity: 0.7,
  cursor: 'pointer',
  verticalAlign: 'middle',
};

// Existing views in one slot; active view's title bar holds buttons
// switching to others, so its own title stays only title
function Tabs(props) {
  const {
    tabs = [],
    viewTypes,
    uuid,
    coordinationScopes,
    coordinationScopesBy,
    ...rest
  } = props;
  const [active, setActive] = useState(0);
  const container = useRef(null);
  // Own element for buttons: title text update rewrites title element's
  // content, which deleted buttons portalled straight into it
  const holder = useRef(null);
  if (!holder.current) holder.current = document.createElement('span');
  /* biome-ignore lint/correctness/useExhaustiveDependencies: inner view
  remounts on switch, so its title element is found again */
  useLayoutEffect(() => {
    const title = container.current?.querySelector('[class*="titleLeft"]');
    if (!title) return undefined;
    const attach = () => {
      if (holder.current.parentNode !== title) title.append(holder.current);
    };
    attach();
    const observer = new MutationObserver(attach);
    observer.observe(title, { childList: true });
    return () => {
      observer.disconnect();
      holder.current.remove();
    };
  }, [active]);
  const tab = tabs[active];
  const Component = viewTypes?.find(
    (viewType) => viewType.name === tab?.component,
  )?.component;
  const switches = tabs
    .map((other, index) => ({ other, index }))
    .filter(({ index }) => index !== active)
    .map(({ other, index }) =>
      h('button', {
        key: other.label,
        type: 'button',
        style: SWITCH_STYLE,
        title: `Show ${other.label}`,
        'aria-label': `Show ${other.label}`,
        // Label drawn by CSS: export file names come from title text
        'data-gateway-label': other.label,
        className: 'gateway-tab-switch',
        // Title bar is panel's drag handle
        onMouseDown: (event) => event.stopPropagation(),
        onClick: () => setActive(index),
      }),
    );
  // Inactive view unmounts: views measure their size only on mount and resize
  const view = Component
    ? h(Component, {
        ...rest,
        ...tab.props,
        key: active,
        viewTypes,
        uuid: `${uuid}-${active}`,
        coordinationScopes: {
          ...coordinationScopes,
          ...tab.coordinationScopes,
        },
        coordinationScopesBy: tab.coordinationScopesBy ?? coordinationScopesBy,
      })
    : null;
  return h(
    'div',
    {
      ref: container,
      style: { display: 'flex', flexDirection: 'column', height: '100%' },
    },
    view,
    createPortal(switches, holder.current),
  );
}

// Static H&E thumbnail per sample of study config; click asks main.js to
// switch, as sample drop-down does
function SampleThumbnails(props) {
  const {
    theme,
    removeGridComponent,
    closeButtonVisible,
    title = 'Samples',
    samples = [],
  } = props;
  // Thumbnails and configs are named relative to current config
  const base = new URL(
    new URLSearchParams(window.location.search).get('config') || '',
    window.location.origin,
  );
  const cards = samples.map((sample) =>
    h(
      'button',
      {
        key: sample.config,
        type: 'button',
        className: 'gateway-thumbnail',
        title: `Show ${sample.label}; genes and selections are kept`,
        onClick: () =>
          window.dispatchEvent(
            new CustomEvent('gateway-switch-sample', { detail: sample.config }),
          ),
      },
      h('img', {
        src: new URL(sample.thumbnail, base).href,
        alt: `H&E of ${sample.label}`,
        loading: 'lazy',
      }),
      h('span', { className: 'gateway-thumbnail-label' }, sample.label),
      sample.disease
        ? h('span', { className: 'gateway-thumbnail-disease' }, sample.disease)
        : null,
    ),
  );
  return h(
    GatewayTitleInfo,
    {
      title,
      info: `${samples.length} samples`,
      theme,
      isScroll: true,
      closeButtonVisible,
      downloadButtonVisible: false,
      removeGridComponent,
      isReady: true,
      urls: [],
      errors: [],
    },
    h('div', { className: 'gateway-thumbnails' }, cards),
  );
}

const tabCoordinationTypes = [
  ...new Set(
    TAB_VIEW_TYPES.flatMap(
      (name) => gatewayComponentCoordinationTypes[name] || [],
    ),
  ),
];

export const GATEWAY_VIEW_TYPES = [
  new PluginViewType(
    'gatewayCellTypeCounts',
    CellTypeCounts,
    COUNTS_COORDINATION_TYPES,
  ),
  new PluginViewType('gatewayTabs', Tabs, tabCoordinationTypes),
  new PluginViewType('gatewaySampleThumbnails', SampleThumbnails, ['dataset']),
];
