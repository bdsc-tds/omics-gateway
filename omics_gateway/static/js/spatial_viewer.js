// Script to hand spatial viewer page over to Vitessce and patch its UI

// Bundle offers no callback and renders either viewer or bare #message div
// on failure, so watch #root to tell which arrived
(() => {
  const root = document.getElementById('root');
  // Bootstrap resets inflate Vitessce's rows, so chrome's CSS leaves with it.
  // Collected before bundle runs, so only chrome's own links are taken
  const chromeStyles = Array.prototype.slice.call(
    document.querySelectorAll('head link[rel="stylesheet"]:not([data-keep])'),
  );

  const configUrl =
    new URLSearchParams(window.location.search).get('config') || '';

  // Bundle fetches same config, so this comes from browser cache
  const config = fetch(configUrl).then((response) => response.json());
  const extraTypes = config.then(extraChannelTypes).catch(() => []);

  // Extra segmentations' obsType differs from their layer's, so their data is
  // not found; Metric's featureType does resolve
  function extraChannelTypes(config) {
    const space = config.coordinationSpace || {};
    const values = space.obsType || {};
    const meta = space.metaCoordinationScopesBy || {};
    const layerTypes = [];
    const channelTypes = [];
    function collect(level, out) {
      const scopes = level?.obsType || {};
      const featureTypes = level?.featureType || {};
      Object.keys(scopes).forEach((scope) => {
        if (featureTypes[scope]) return;
        out.push(values[scopes[scope]]);
      });
    }
    Object.keys(meta).forEach((key) => {
      collect(meta[key].segmentationLayer, layerTypes);
      collect(meta[key].segmentationChannel, channelTypes);
    });
    return channelTypes.filter(
      (type) => type && layerTypes.indexOf(type) === -1,
    );
  }

  const logEl = document.getElementById('spatial-log');
  function log(text) {
    logEl.textContent += `${new Date().toTimeString().slice(0, 8)}  ${text}\n`;
  }
  log('Loading viewer bundle');

  new MutationObserver((_records, observer) => {
    const message = root.querySelector('#message');
    if (message) {
      log('Failed to load dataset');
      document.getElementById('spatial-loading').hidden = true;
      // Bundle's own wording is diagnostic, so it goes to Stderr and
      // Message says plainly what failed
      document.getElementById('spatial-error-message').textContent =
        'This spatial dataset could not be loaded in your browser.';
      document.getElementById('spatial-error-stderr').textContent =
        message.textContent;
      document.getElementById('spatial-error').hidden = false;
      observer.disconnect();
    } else if (root.children.length) {
      // Viewer mounted; hand whole page over to it
      log('Starting viewer');
      document.getElementById('chrome').remove();
      chromeStyles.forEach((link) => {
        link.remove();
      });
      observer.disconnect();
      patchSpatialLegends(root);
      config
        .then((json) => {
          patchMetricTooltips(json, configUrl);
        })
        .catch(() => {});
      orderLayerRows(root);
      limitLassoToCells(root);
      extraTypes.then((types) => {
        constrainExtraEncodings(root, types);
      });
    }
  }).observe(root, { childList: true, subtree: true });

  // Extra segmentations only get fixed colour: menu keeps Static Color alone
  function constrainExtraEncodings(root, extraTypes) {
    if (!extraTypes.length) return;
    let openedFor = null;
    // Portalled menu is outside its row, so get its channel from opener
    root.addEventListener(
      'click',
      (event) => {
        const button = event.target.closest(
          'button[aria-label="Open segmentation channel options menu"]',
        );
        if (!button) return;
        const name = button
          .closest('[class*="layerControllerGrid"]')
          .querySelector('[class*="imageLayerName"]');
        openedFor = name ? name.textContent.trim().toLowerCase() : null;
      },
      true,
    );

    new MutationObserver(() => {
      if (extraTypes.indexOf(openedFor) === -1) return;
      const options = document.querySelectorAll(
        'select[aria-label="Color encoding selector"] ' +
          'option:not([value="spatialChannelColor"])',
      );
      for (let i = 0; i < options.length; i++) {
        // Disable as well as hide: hidden alone may leave it selectable
        options[i].hidden = true;
        options[i].disabled = true;
      }
      // Colormap rows apply only to feature values, so they go too; inline
      // style, as row's class overrides [hidden]
      const labels = document.querySelectorAll(
        '[class*="imageLayerMenuLabel"]',
      );
      for (let j = 0; j < labels.length; j++) {
        if (labels[j].textContent.trim().indexOf('Colormap') !== 0) continue;
        const item = labels[j].closest('li') || labels[j].parentElement;
        item.style.display = 'none';
      }
    }).observe(document.body, { childList: true, subtree: true });
  }

  // Vitessce tooltips list only ids and sets: add selected metric from
  // generator's sidecar, fetched on first hover, while Metric layer is on
  function patchMetricTooltips(config, configUrl) {
    const featureTypes = config.coordinationSpace?.featureType || {};
    const hasMetrics = Object.keys(featureTypes).some(
      (scope) => featureTypes[scope] === 'metric',
    );
    const valuesUrl = configUrl.replace(/\.vitessce\.json$/, '.metrics.json');
    if (!hasMetrics || valuesUrl === configUrl) return;
    let values = null;
    let loaded = null;

    function format(value) {
      if (value === null) return 'n/a';
      return Number.isInteger(value) ? String(value) : value.toFixed(2);
    }

    // Row's eye icon never changes, but its swatch is blank while hidden
    function metricLayerVisible() {
      const names = document.querySelectorAll('[class*="imageLayerName"]');
      for (let i = 0; i < names.length; i++) {
        if (names[i].textContent.trim() !== 'Metric') continue;
        const row = names[i].closest('[class*="layerControllerGrid"]');
        return !!row?.querySelector(
          '[class*="colorIcon"] svg, [class*="colorIcon"][style*="background"]',
        );
      }
      return false;
    }

    // Metric list checks its selected row's hidden checkbox
    function selectedMetric() {
      return values.columns.findIndex((column) => {
        const input = document.querySelector(
          `input[type="checkbox"][value="${column}"]`,
        );
        return input?.checked;
      });
    }

    function patch() {
      // Tooltip is portalled, and React reuses it across hovered cells
      const headers = document.querySelectorAll('th');
      for (let i = 0; i < headers.length; i++) {
        const th = headers[i];
        const tbody = th.closest('tbody');
        // Hovered id is always first row
        if (!tbody || th !== tbody.querySelector('th')) continue;
        if (th.textContent === 'Metric ID') {
          // Metric layer's own, shown while Cell layer is hidden. React
          // never rewrites constant key, so rename sticks
          th.textContent = 'Cell ID';
        } else if (th.textContent !== 'Cell ID') {
          continue;
        }
        if (!values) {
          loaded =
            loaded ||
            fetch(valuesUrl)
              .then((response) => response.json())
              .then((json) => {
                values = json;
                patch();
              });
          continue;
        }
        const cellId = th.nextElementSibling.textContent;
        const metric = metricLayerVisible() ? selectedMetric() : -1;
        const key = `${cellId}|${metric}`;
        if (tbody.dataset.metricCell === key) continue;
        tbody.dataset.metricCell = key;
        const old = tbody.querySelector('tr[data-metric-row]');
        if (old) old.remove();
        const cellValues = values.cells[cellId];
        if (metric < 0 || !cellValues) continue;
        const row = document.createElement('tr');
        row.dataset.metricRow = '';
        const name = document.createElement('th');
        name.textContent = values.columns[metric];
        const value = document.createElement('td');
        value.textContent = format(cellValues[metric]);
        row.append(name, value);
        tbody.append(row);
      }
    }
    new MutationObserver(patch).observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  // Lasso takes every visible segmentation channel, adding empty selections
  // for those without quadtree; keep only Cell's while any layer is visible
  function limitLassoToCells(root) {
    const seen = new WeakSet();
    let warned = false;
    function warn(text) {
      if (warned) return;
      warned = true;
      console.warn(`Lasso override not applied: ${text}`);
    }

    // Nearest React fiber at or above element
    function fiberOf(element) {
      for (let el = element; el && el !== root; el = el.parentElement) {
        const key = Object.keys(el).find(
          (name) => name.indexOf('__reactFiber$') === 0,
        );
        if (key) return el[key];
      }
      return null;
    }

    // Class component above deck.gl canvas; UMAP's lacks segmentation props
    function spatialOf(canvas) {
      let fiber = fiberOf(canvas);
      if (!fiber) warn('no React fiber above canvas');
      for (; fiber; fiber = fiber.return) {
        const node = fiber.stateNode;
        if (node?.props && 'segmentationLayerScopes' in node.props) {
          return node;
        }
      }
      return null;
    }

    function wrap(spatial) {
      const original = spatial.createSelectionLayer;
      if (typeof original !== 'function') {
        warn('createSelectionLayer not found');
        return;
      }
      spatial.createSelectionLayer = function () {
        const props = this.props;
        const coordination = props.segmentationChannelCoordination;
        if (!coordination?.[0]) return original.call(this);
        const trees = this.obsSegmentationsQuadTree;
        // Renamed after upgrade: no channel would join, so keep original
        if (!trees || typeof trees !== 'object') {
          warn('obsSegmentationsQuadTree not found');
          return original.call(this);
        }
        const channels = coordination[0];
        const layers = (props.segmentationLayerCoordination || [])[0] || {};
        const anyVisible = Object.keys(channels).some(
          (layer) =>
            layers[layer]?.spatialLayerVisible &&
            Object.keys(channels[layer]).some(
              (channel) => channels[layer][channel].spatialChannelVisible,
            ),
        );
        // Copies, not store objects: only lasso sees changed visibility
        const values = {};
        Object.keys(channels).forEach((layer) => {
          values[layer] = {};
          Object.keys(channels[layer]).forEach((channel) => {
            const hasTree = !!trees[layer]?.[channel];
            values[layer][channel] = Object.assign(
              {},
              channels[layer][channel],
              { spatialChannelVisible: anyVisible && hasTree },
            );
          });
        });
        // Real instance underneath supplies state and quadtrees
        const view = Object.create(this, {
          props: {
            value: Object.assign({}, props, {
              segmentationChannelCoordination: [values, coordination[1]],
            }),
          },
        });
        return original.call(view);
      };
    }

    function scan() {
      const canvases = root.getElementsByTagName('canvas');
      for (let i = 0; i < canvases.length; i++) {
        if (seen.has(canvases[i])) continue;
        seen.add(canvases[i]);
        const spatial = spatialOf(canvases[i]);
        if (spatial && !seen.has(spatial)) {
          seen.add(spatial);
          wrap(spatial);
        }
      }
    }
    scan();
    // Canvas mounts once data loads, and again if view remounts
    new MutationObserver(scan).observe(root, {
      childList: true,
      subtree: true,
    });
  }

  // Metric draws below cells so they keep hover, but row stays listed
  // after Cell. Flex order is visual only; column-reverse sorts from bottom
  function orderLayerRows(root) {
    function reorder() {
      const names = root.querySelectorAll('[class*="imageLayerName"]');
      let cell = null;
      let metric = null;
      for (let i = 0; i < names.length; i++) {
        const name = names[i].textContent.trim();
        const row = names[i].closest('[class*="layerControllerGrid"]');
        if (name === 'Cell') cell = row;
        if (name === 'Metric') metric = row;
      }
      if (!cell || !metric || cell.parentNode !== metric.parentNode) return;
      const rows = Array.prototype.slice.call(cell.parentNode.children);
      const cellIndex = rows.indexOf(cell);
      const metricIndex = rows.indexOf(metric);
      rows[cellIndex] = metric;
      rows[metricIndex] = cell;
      rows.forEach((row, index) => {
        // Style changes are not observed, so no feedback loop
        if (row.style.order !== String(index)) row.style.order = index;
      });
    }
    reorder();
    new MutationObserver(reorder).observe(root, {
      childList: true,
      subtree: true,
    });
  }

  // Bundle uses "Points", leaves fixed-colour swatches grey, and redraws on
  // selection. Observer must outlive handover
  function patchSpatialLegends(root) {
    function rename() {
      const titles = root.querySelectorAll('[class*="legend"] text');
      for (let i = 0; i < titles.length; i++) {
        if (titles[i].textContent === 'Points') {
          titles[i].textContent = 'Transcript';
        }
      }
    }

    // Bundle leaves fixed-colour swatches grey: recolour them, shrinking
    // full-width bar (96, legend minus padding) to square
    function recolourSwatches() {
      const bars = root.querySelectorAll('[class*="legend"] rect[width="96"]');
      for (let i = 0; i < bars.length; i++) {
        const title = bars[i].ownerSVGElement.querySelector('text');
        const colour = layerColour(title?.textContent);
        if (!colour) continue;
        bars[i].setAttribute('width', '8');
        bars[i].setAttribute('fill', colour);
      }
    }

    // Layer row picker carries channel colour inline, tracking controller
    // changes. Its button shares class prefix, so find coloured swatch
    function layerColour(label) {
      const names = root.querySelectorAll('[class*="imageLayerName"]');
      for (let i = 0; i < names.length; i++) {
        if (names[i].textContent.trim() !== label) continue;
        const row = names[i].closest('[class*="layerControllerGrid"]');
        const icons = row ? row.querySelectorAll('[class*="colorIcon"]') : [];
        for (let j = 0; j < icons.length; j++) {
          if (icons[j].style.backgroundColor) {
            return icons[j].style.backgroundColor;
          }
        }
      }
      return null;
    }

    // Lifted title shares row with obsType label, which long metric names
    // reach: shrink to fit, then truncate
    function fitLiftedTitles() {
      const titles = root.querySelectorAll(
        '[class*="legend"] svg[height="36"] text[y="18"]',
      );
      for (let i = 0; i < titles.length; i++) {
        const title = titles[i];
        const label = title.parentNode.querySelector('text[y="0"]');
        if (!label) continue;
        const box = label.getBBox();
        const room = Number(title.getAttribute('x')) - box.x - box.width - 6;
        const length = title.getComputedTextLength();
        if (room <= 0 || length <= room) continue;
        const size = Math.max(7, (9 * room) / length);
        title.style.fontSize = `${size}px`;
        let text = title.textContent;
        while (text.length > 1 && title.getComputedTextLength() > room) {
          text = text.slice(0, -1);
          title.textContent = `${text}\u2026`;
        }
      }
    }

    // List Metric legend after Cell's, as in layer controller; extras matched
    // by title, since Cell's varies (set name when set-coloured)
    function orderLegends() {
      const legends = root.querySelectorAll('[class*="multiLegend"]');
      for (let i = 0; i < legends.length; i++) {
        const items = Array.prototype.slice.call(legends[i].children);
        // Colour bar tick labels precede titles, so search all texts
        const texts = items.map((item) =>
          Array.prototype.map.call(
            item.querySelectorAll('svg text'),
            (text) => text.textContent,
          ),
        );
        const metricIndex = texts.findIndex(
          (labels) => labels.indexOf('Metric') !== -1,
        );
        if (metricIndex === -1) continue;
        const metric = items.splice(metricIndex, 1)[0];
        texts.splice(metricIndex, 1);
        // Column-reverse: earlier in sorted order is lower on screen
        const cellIndex = texts.findIndex(
          (labels) =>
            !['Transcript', 'Points', 'Nucleus'].some(
              (title) => labels.indexOf(title) !== -1,
            ),
        );
        items.splice(cellIndex === -1 ? items.length : cellIndex, 0, metric);
        items.forEach((item, index) => {
          if (item.style.order !== String(index)) item.style.order = index;
        });
      }
    }

    function patch() {
      rename();
      recolourSwatches();
      fitLiftedTitles();
      orderLegends();
    }
    patch();
    new MutationObserver(patch).observe(root, {
      childList: true,
      subtree: true,
    });
  }
})();
