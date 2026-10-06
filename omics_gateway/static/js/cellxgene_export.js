// Script adding PNG export of cellxgene's embedding, injected by gateway proxy

// Relies on cellxgene 1.3.0 internals (React 17 fibers, Graph component's
// regl state, sidebar category props); breakage mostly leaves button disabled
(() => {
  // Exported pixels per CSS pixel of embedding
  const EXPORT_SCALE = 4;
  let warned = false;
  function warn(text) {
    if (warned) return;
    warned = true;
    console.warn(`Image export: ${text}`);
  }

  // Nearest React fiber at or above element
  function fiberOf(element) {
    for (let el = element; el && el !== document.body; el = el.parentElement) {
      const key = Object.keys(el).find(
        (name) => name.indexOf('__reactFiber$') === 0,
      );
      if (key) return el[key];
    }
    return null;
  }

  // Graph component above embedding canvas, holding regl state
  function graphOf(canvas) {
    for (let fiber = fiberOf(canvas); fiber; fiber = fiber.return) {
      const node = fiber.stateNode;
      if (typeof node?.renderPoints === 'function' && node.state?.drawPoints) {
        return node;
      }
    }
    return null;
  }

  // Graph's react-async instance, pending while it refetches on colour or
  // layout change; canvas still shows old colours until then
  function loaderOf(graph) {
    const stack = [graph._reactInternals?.child];
    while (stack.length) {
      const fiber = stack.pop();
      if (!fiber) continue;
      if (fiber.stateNode?.props?.promiseFn === graph.fetchAsyncProps) {
        return fiber.stateNode;
      }
      stack.push(fiber.sibling, fiber.child);
    }
    return null;
  }

  // Props of category's sidebar entry, holding colour table even collapsed
  function categoryProps(field) {
    const el = document.querySelector(
      `[data-testid="category-${CSS.escape(field)}"]`,
    );
    for (let fiber = fiberOf(el); fiber; fiber = fiber.return) {
      const props = fiber.memoizedProps;
      if (props?.categorySummary && 'colorTable' in props) return props;
    }
    return null;
  }

  // Drawn with current colours, and category legend caught up with them
  function ready(graph) {
    if (!graph?.state.regl || !graph.cachedAsyncProps || !graph.reglCanvas) {
      return false;
    }
    if (loaderOf(graph)?.state.isPending !== false) return false;
    const { colorMode, colorAccessor } = graph.props.colors || {};
    if (colorMode !== 'color by categorical metadata') return true;
    return categoryProps(colorAccessor)?.colorAccessor === colorAccessor;
  }

  // Column-major 3x3 product, as gl-matrix's mat3.multiply
  function multiply(a, b) {
    const out = new Float32Array(9);
    for (let col = 0; col < 3; col++) {
      for (let row = 0; row < 3; row++) {
        let sum = 0;
        for (let k = 0; k < 3; k++) sum += a[k * 3 + row] * b[col * 3 + k];
        out[col * 3 + row] = sum;
      }
    }
    return out;
  }

  // Computed styles inlined into SVG clones, as stylesheets do not follow
  // them; margin keeps legend titles drawn above their box
  const STYLE_PROPS = [
    'fill',
    'fill-opacity',
    'stroke',
    'stroke-width',
    'stroke-opacity',
    'opacity',
    'font-family',
    'font-size',
    'font-weight',
    'font-style',
    'text-anchor',
    'dominant-baseline',
    'visibility',
    'display',
    'transform',
  ];
  const MARGIN = 20;
  const SVG_NS = 'http://www.w3.org/2000/svg';
  // Standalone SVG copy at CSS size, with margin added on each side
  function svgClone(svg, margin) {
    const clone = svg.cloneNode(true);
    const from = [svg].concat(Array.from(svg.querySelectorAll('*')));
    const to = [clone].concat(Array.from(clone.querySelectorAll('*')));
    from.forEach((el, i) => {
      const style = getComputedStyle(el);
      const rules = STYLE_PROPS.filter(
        (name) =>
          name !== 'transform' || style.getPropertyValue(name) !== 'none',
      ).map((name) => {
        // Computed url() is absolute, pointing outside clone: keep #id only
        const value = style
          .getPropertyValue(name)
          .replace(/url\("?[^#")]*(#[^")]+)"?\)/g, 'url($1)');
        return `${name}:${value}`;
      });
      to[i].setAttribute('style', rules.join(';'));
    });
    // Nested images draw blank inside SVG image; drawn separately instead
    clone.querySelectorAll('image').forEach((el) => {
      el.remove();
    });
    const box = svg.getBoundingClientRect();
    const width = box.width + 2 * margin;
    const height = box.height + 2 * margin;
    clone.setAttribute('xmlns', SVG_NS);
    clone.setAttribute('width', width);
    clone.setAttribute('height', height);
    clone.setAttribute('viewBox', `${-margin} ${-margin} ${width} ${height}`);
    return clone;
  }

  // Clone as image, rasterised at scale through its own size, not stretched
  function svgImage(clone, scale) {
    ['width', 'height'].forEach((name) => {
      clone.setAttribute(name, clone.getAttribute(name) * scale);
    });
    const text = new XMLSerializer().serializeToString(clone);
    return loadImage(
      `data:image/svg+xml;charset=utf-8,${encodeURIComponent(text)}`,
    );
  }

  function svgNode(name, attributes) {
    const node = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => {
      node.setAttribute(key, value);
    });
    return node;
  }

  // Embedded image; xlink:href, as older editors ignore plain href
  function svgPicture(href, box) {
    const node = svgNode('image', { ...box, preserveAspectRatio: 'none' });
    node.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', href);
    return node;
  }

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () =>
        reject(new Error(`cannot load ${src.slice(0, 40)}`));
      image.src = src;
    });
  }

  // Points redrawn once at scale into copy, then at screen size again, all in
  // one task: buffer lacks preserveDrawingBuffer, so it clears once composited
  function pointsImage(graph) {
    const {
      regl,
      drawPoints,
      colorBuffer,
      pointBuffer,
      flagBuffer,
      camera,
      projectionTF,
    } = graph.state;
    const { annoMatrix } = graph.props;
    const canvas = graph.reglCanvas;
    // Graph ignores devicePixelRatio, so buffer size is CSS size
    const width = canvas.width;
    const height = canvas.height;
    const gl = regl._gl;
    const limit = Math.min(
      gl.getParameter(gl.MAX_RENDERBUFFER_SIZE),
      ...gl.getParameter(gl.MAX_VIEWPORT_DIMS),
    );
    const wanted = Math.min(EXPORT_SCALE, limit / Math.max(width, height));
    const out = document.createElement('canvas');
    const ctx = out.getContext('2d');
    let scale;
    try {
      canvas.width = Math.floor(width * wanted);
      canvas.height = Math.floor(height * wanted);
      // Actual ratio: browser may clamp buffer below request
      scale = gl.drawingBufferWidth / width;
      out.width = gl.drawingBufferWidth;
      out.height = gl.drawingBufferHeight;
      // Background points are translucent, as on page's white
      ctx.fillStyle = '#fff';
      ctx.fillRect(0, 0, out.width, out.height);
      regl.poll();
      regl.clear({ depth: 1, color: [1, 1, 1, 1] });
      // Point size goes with sqrt(distance) only, so scale^2 enlarges points
      // by scale; screen-sized minViewportDimension keeps density-based size
      drawPoints({
        distance: camera.distance() * scale * scale,
        color: colorBuffer,
        position: pointBuffer,
        flag: flagBuffer,
        count: annoMatrix.nObs,
        projView: multiply(projectionTF, camera.view()),
        nPoints: annoMatrix.schema.dataframe.nObs,
        minViewportDimension: Math.min(width, height),
      });
      ctx.drawImage(canvas, 0, 0);
    } finally {
      // Resizing clears canvas, so redraw at screen size straight away
      canvas.width = width;
      canvas.height = height;
      graph.renderPoints(
        regl,
        drawPoints,
        colorBuffer,
        pointBuffer,
        flagBuffer,
        camera,
        projectionTF,
      );
    }
    return { out, scale };
  }

  // Values and colours of category colouring embedding
  function categoryLegend(field) {
    const props = categoryProps(field);
    if (!props?.colorTable?.scale) {
      warn(`no colour table found for ${field}, legend left out`);
      return null;
    }
    const summary = props.categorySummary;
    const entries = summary.categoryValues
      .map((value, i) => ({
        label: String(value),
        colour: String(props.colorTable.scale(i)),
        count: summary.categoryValueCounts[i],
      }))
      // Values absent from current view are not drawn either
      .filter((entry) => entry.count > 0);
    return { title: field, entries };
  }

  // Legend sizes in CSS pixels
  const FONT_SIZE = 13;
  const ROW = 18;
  const SWATCH = 12;
  const GAP = 6;
  const PAD = 12;
  // Cellxgene's stack minus Roboto Condensed, which SVG images cannot load,
  // so legend matches rasterised labels and axis
  const FONT_FAMILY = '"Helvetica Neue", Helvetica, Arial, sans-serif';
  // Category legend as column(s) beside embedding, as it has none on screen;
  // positions in CSS pixels, shared by PNG and SVG
  function legendLayout(legend, height) {
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.font = `${FONT_SIZE}px ${FONT_FAMILY}`;
    const labelWidth = Math.max(
      ...legend.entries.map((entry) => ctx.measureText(entry.label).width),
    );
    ctx.font = `bold ${FONT_SIZE}px ${FONT_FAMILY}`;
    const titleWidth = ctx.measureText(legend.title).width;
    const perColumn = Math.max(1, Math.floor((height - 2 * PAD - ROW) / ROW));
    const columns = Math.ceil(legend.entries.length / perColumn);
    const columnWidth = SWATCH + GAP + labelWidth + PAD;
    return {
      width: PAD + Math.max(columns * columnWidth, titleWidth + PAD),
      height,
      title: { text: legend.title, x: PAD, y: PAD + ROW / 2 },
      items: legend.entries.map((entry, i) => {
        const x = PAD + Math.floor(i / perColumn) * columnWidth;
        const y = PAD + ROW + (i % perColumn) * ROW;
        return {
          colour: entry.colour,
          label: entry.label,
          swatch: { x, y: y + (ROW - SWATCH) / 2 },
          text: { x: x + SWATCH + GAP, y: y + ROW / 2 },
        };
      }),
    };
  }

  function legendImage(layout, scale) {
    const out = document.createElement('canvas');
    out.width = Math.ceil(layout.width * scale);
    out.height = Math.ceil(layout.height * scale);
    const ctx = out.getContext('2d');
    ctx.scale(scale, scale);
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, layout.width, layout.height);
    ctx.textBaseline = 'middle';
    ctx.fillStyle = '#000';
    ctx.font = `bold ${FONT_SIZE}px ${FONT_FAMILY}`;
    ctx.fillText(layout.title.text, layout.title.x, layout.title.y);
    ctx.font = `${FONT_SIZE}px ${FONT_FAMILY}`;
    layout.items.forEach((item) => {
      ctx.fillStyle = item.colour;
      ctx.fillRect(item.swatch.x, item.swatch.y, SWATCH, SWATCH);
      ctx.fillStyle = '#000';
      ctx.fillText(item.label, item.text.x, item.text.y);
    });
    return out;
  }

  // Fill attributes for colour; editors may not read rgba() in fill
  function fillOf(colour) {
    const parts = /rgba\(([^,]+),([^,]+),([^,]+),([^)]+)\)/.exec(colour);
    if (!parts) return { fill: colour };
    return {
      fill: `rgb(${parts[1]},${parts[2]},${parts[3]})`,
      'fill-opacity': parts[4].trim(),
    };
  }

  function legendSvg(layout) {
    const group = svgNode('g', {
      'font-family': FONT_FAMILY,
      'font-size': FONT_SIZE,
      'dominant-baseline': 'central',
    });
    group.appendChild(
      svgNode('rect', {
        width: layout.width,
        height: layout.height,
        fill: '#fff',
      }),
    );
    const title = svgNode('text', {
      x: layout.title.x,
      y: layout.title.y,
      'font-weight': 'bold',
    });
    title.textContent = layout.title.text;
    group.appendChild(title);
    layout.items.forEach((item) => {
      group.appendChild(
        svgNode('rect', {
          ...item.swatch,
          width: SWATCH,
          height: SWATCH,
          ...fillOf(item.colour),
        }),
      );
      const text = svgNode('text', item.text);
      text.textContent = item.label;
      group.appendChild(text);
    });
    return group;
  }

  // Colour bar is one-pixel-wide canvas flipped by CSS; copied upright
  function barImage(bar) {
    const out = document.createElement('canvas');
    out.width = bar.width;
    out.height = bar.height;
    const ctx = out.getContext('2d');
    if (new DOMMatrix(getComputedStyle(bar).transform).d < 0) {
      ctx.translate(0, bar.height);
      ctx.scale(1, -1);
    }
    ctx.drawImage(bar, 0, 0);
    return out;
  }

  // Points copy at EXPORT_SCALE plus labels and legend, all read in one task,
  // as legend redraws on colour change
  function capture(graph) {
    const box = graph.reglCanvas.getBoundingClientRect();
    const label = document.querySelector('#graph-wrapper .centroid-label');
    const overlay = label?.closest('svg');
    const legendBox = document.getElementById('continuous_legend');
    const bar = legendBox?.querySelector('canvas');
    const axis = legendBox?.querySelector('svg');
    const { colorMode, colorAccessor } = graph.props.colors || {};
    const legend =
      colorMode === 'color by categorical metadata'
        ? categoryLegend(colorAccessor)
        : null;
    const { out, scale } = pointsImage(graph);
    return {
      out,
      box,
      scale,
      overlay: overlay && {
        clone: svgClone(overlay, 0),
        rect: overlay.getBoundingClientRect(),
        // Overlay washes points out while labels show
        wash: getComputedStyle(overlay).backgroundColor,
      },
      bar: bar && { image: barImage(bar), rect: bar.getBoundingClientRect() },
      axis: axis && {
        clone: svgClone(axis, MARGIN),
        rect: axis.getBoundingClientRect(),
      },
      layout: legend?.entries.length ? legendLayout(legend, box.height) : null,
    };
  }

  async function pngImage(captured) {
    const { out, box, scale, overlay, bar, axis, layout } = captured;
    const ctx = out.getContext('2d');
    function place(rect, margin) {
      return [
        (rect.left - box.left - margin) * scale,
        (rect.top - box.top - margin) * scale,
        (rect.width + 2 * margin) * scale,
        (rect.height + 2 * margin) * scale,
      ];
    }
    const [overlayImage, axisImage] = await Promise.all([
      overlay ? svgImage(overlay.clone, scale) : null,
      axis ? svgImage(axis.clone, scale) : null,
    ]);
    if (overlayImage) {
      ctx.fillStyle = overlay.wash;
      ctx.fillRect(...place(overlay.rect, 0));
      ctx.drawImage(overlayImage, ...place(overlay.rect, 0));
    }
    if (bar) ctx.drawImage(bar.image, ...place(bar.rect, 0));
    if (axisImage) ctx.drawImage(axisImage, ...place(axis.rect, MARGIN));
    if (!layout) return out;

    const side = legendImage(layout, scale);
    const full = document.createElement('canvas');
    full.width = out.width + side.width;
    full.height = out.height;
    const fullCtx = full.getContext('2d');
    fullCtx.drawImage(out, 0, 0);
    fullCtx.drawImage(side, out.width, 0);
    return full;
  }

  // Points embedded as PNG under vector labels and legends; WebGL draws no
  // vector form
  function svgDocument(captured) {
    const { out, box, overlay, bar, axis, layout } = captured;
    const width = box.width + (layout ? layout.width : 0);
    const doc = svgNode('svg', {
      xmlns: SVG_NS,
      width,
      height: box.height,
      viewBox: `0 0 ${width} ${box.height}`,
    });
    function place(rect, margin) {
      return {
        x: rect.left - box.left - margin,
        y: rect.top - box.top - margin,
        width: rect.width + 2 * margin,
        height: rect.height + 2 * margin,
      };
    }
    doc.appendChild(svgPicture(out.toDataURL('image/png'), place(box, 0)));
    if (overlay) {
      doc.appendChild(
        svgNode('rect', { ...place(overlay.rect, 0), ...fillOf(overlay.wash) }),
      );
      const { x, y } = place(overlay.rect, 0);
      overlay.clone.setAttribute('x', x);
      overlay.clone.setAttribute('y', y);
      doc.appendChild(overlay.clone);
    }
    if (bar) {
      doc.appendChild(
        svgPicture(bar.image.toDataURL('image/png'), place(bar.rect, 0)),
      );
    }
    if (axis) {
      const { x, y } = place(axis.rect, MARGIN);
      axis.clone.setAttribute('x', x);
      axis.clone.setAttribute('y', y);
      doc.appendChild(axis.clone);
    }
    if (layout) {
      const group = legendSvg(layout);
      group.setAttribute('transform', `translate(${box.width} 0)`);
      doc.appendChild(group);
    }
    return doc;
  }

  // Dataset name from /view/<path>/, as cellxgene runs under gateway's path
  function fileName(graph, format) {
    const path = decodeURIComponent(window.location.pathname);
    const descriptor = path.split('/view/').pop().replace(/\/+$/, '');
    const stem = (descriptor.split('/').pop() || 'dataset').replace(
      /\.h5ad$/,
      '',
    );
    const layout = graph.props.layoutChoice?.current || 'embedding';
    return `${stem}_${layout}.${format}`.replace(/[^\w.-]+/g, '_');
  }

  async function exportView(graph, format) {
    const captured = capture(graph);
    let blob;
    if (format === 'svg') {
      const text = new XMLSerializer().serializeToString(svgDocument(captured));
      blob = new Blob([text], { type: 'image/svg+xml' });
    } else {
      const out = await pngImage(captured);
      blob = await new Promise((resolve) => {
        out.toBlob(resolve, 'image/png');
      });
    }
    if (!blob) throw new Error('canvas could not be encoded');
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = fileName(graph, format);
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  }

  // Blueprint's 16 px "download" icon, as cellxgene's buttons draw theirs
  const ICON =
    '<span icon="download" aria-hidden="true" class="bp3-icon bp3-icon-download">' +
    '<svg data-icon="download" width="16" height="16" viewBox="0 0 16 16">' +
    '<path d="M7.99-.01c-4.42 0-8 3.58-8 8s3.58 8 8 8 8-3.58 8-8-3.58-8-8-8zM11.7 ' +
    '9.7l-3 3c-.18.18-.43.29-.71.29s-.53-.11-.71-.29l-3-3A1.003 1.003 0 015.7 ' +
    '8.28l1.29 1.29V3.99c0-.55.45-1 1-1s1 .45 1 1v5.59l1.29-1.29a1.003 1.003 0 ' +
    '011.71.71c0 .27-.11.52-.29.7z" fill-rule="evenodd"></path></svg></span>';
  // Blueprint popover arrow, pointing up, as on cellxgene's own menus
  const ARROW =
    '<svg viewBox="0 0 30 30" style="transform: rotate(90deg);">' +
    '<path class="bp3-popover-arrow-border" d="M8.11 6.302c1.015-.936 ' +
    '1.887-2.922 1.887-4.297v26c0-1.378-.868-3.357-1.888-4.297L.925 ' +
    '17.09c-1.237-1.14-1.233-3.034 0-4.17L8.11 6.302z"></path>' +
    '<path class="bp3-popover-arrow-fill" d="M8.787 7.036c1.22-1.125 ' +
    '2.21-3.376 2.21-5.03V0v30-2.005c0-1.654-.983-3.9-2.21-5.03l-7.183-6.616' +
    'c-.81-.746-.802-1.96 0-2.7l7.183-6.614z"></path></svg>';
  // Button group in cellxgene's menubar, built from its Blueprint classes
  const group = document.createElement('div');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'bp3-button gateway-export';
  button.title = 'Download embedding as image';
  button.setAttribute('aria-haspopup', 'menu');
  button.setAttribute('aria-expanded', 'false');
  button.innerHTML = ICON;
  group.appendChild(button);

  // Menubar lays children out reversed, so first child sits rightmost, beside
  // undo/redo; re-placed if menubar remounts
  function place() {
    const undo = document
      .querySelector('[data-testid="undo"]')
      ?.closest('.bp3-button-group');
    if (!undo) return;
    group.className = undo.className;
    const bar = undo.parentElement;
    if (bar.firstElementChild !== group)
      bar.insertBefore(group, bar.firstChild);
  }

  let busy = false;
  function currentGraph() {
    return graphOf(document.querySelector('canvas.graph-canvas'));
  }

  function runExport(format) {
    const graph = currentGraph();
    if (!ready(graph)) return;
    busy = true;
    update();
    exportView(graph, format)
      .catch((error) => {
        console.error('Image export failed', error);
      })
      .finally(() => {
        busy = false;
        update();
      });
  }

  // Format menu as Blueprint popover below button, on body so menubar's
  // re-renders cannot touch it
  let menu = null;
  function closeMenu() {
    if (!menu) return;
    menu.remove();
    menu = null;
    button.classList.remove('bp3-active');
    button.setAttribute('aria-expanded', 'false');
  }
  function openMenu() {
    menu = document.createElement('div');
    menu.className = 'bp3-popover gateway-export-menu';
    menu.innerHTML =
      `<div class="bp3-popover-arrow">${ARROW}</div>` +
      '<div class="bp3-popover-content"><ul class="bp3-menu" role="menu"></ul></div>';
    const list = menu.querySelector('ul');
    ['png', 'svg'].forEach((format) => {
      const item = document.createElement('a');
      item.className = 'bp3-menu-item';
      item.setAttribute('role', 'menuitem');
      item.tabIndex = 0;
      item.innerHTML =
        '<div class="bp3-fill bp3-text-overflow-ellipsis"></div>';
      item.firstChild.textContent = `Download ${format.toUpperCase()}`;
      item.addEventListener('click', () => {
        closeMenu();
        runExport(format);
      });
      // Anchor without href ignores Enter and Space
      item.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        item.click();
      });
      const row = document.createElement('li');
      row.appendChild(item);
      list.appendChild(row);
    });
    const r = button.getBoundingClientRect();
    menu.style.position = 'fixed';
    // Room for arrow above popover
    menu.style.top = `${r.bottom + 12}px`;
    menu.style.right = `${document.documentElement.clientWidth - r.right}px`;
    document.body.appendChild(menu);
    const box = menu.getBoundingClientRect();
    const arrow = menu.firstChild;
    arrow.style.left = `${r.left + r.width / 2 - box.left - 15}px`;
    arrow.style.top = '-11px';
    button.classList.add('bp3-active');
    button.setAttribute('aria-expanded', 'true');
    list.querySelector('a').focus();
  }
  button.addEventListener('click', () => {
    if (menu) closeMenu();
    else openMenu();
  });
  document.addEventListener('pointerdown', (event) => {
    if (!menu || menu.contains(event.target)) return;
    if (!button.contains(event.target)) closeMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !menu) return;
    closeMenu();
    button.focus();
  });
  window.addEventListener('resize', closeMenu);

  function update() {
    place();
    button.disabled = busy || !ready(currentGraph());
    button.classList.toggle('bp3-disabled', button.disabled);
    if (button.disabled) closeMenu();
  }
  // Graph mounts once data arrives and remounts on layout change
  update();
  setInterval(update, 500);
})();
