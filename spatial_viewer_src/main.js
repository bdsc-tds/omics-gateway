/**
 * Entry point for self-hosted Vitessce spatial viewer.
 *
 * Vite bundles this file (with React, Vitessce and their dependencies) into
 * static assets under `omics_gateway/static/vitessce/`, which
 * `templates/spatial_viewer.html` loads. Self-hosting replaced CDN-loaded
 * Vitessce, so deployed gateway serves viewer code itself and needs no
 * outbound internet access.
 *
 * Reads Vitessce view-config URL from page's `?config=` query parameter,
 * fetches it, and mounts viewer into `#root`.
 *
 * Build: `npm run build` in `viewer-build` conda env (see README,
 * "Rebuilding the spatial viewer").
 */

import React from 'react';
import { createRoot } from 'react-dom/client';
import { Vitessce } from 'vitessce';

// Read from query string rather than server-side templating: bundle is
// static asset, so Jinja cannot render it.
const configUrl =
  new URLSearchParams(window.location.search).get('config') || '';
const root = createRoot(document.getElementById('root'));

function showMessage(text) {
  root.render(React.createElement('div', { id: 'message' }, text));
}

// Configs keep data URLs root-relative (/spatial-data/…), host-independent;
// Vitessce loaders call `new URL(url)` with no base, so resolve on origin.
function absolutizeUrls(obj) {
  if (Array.isArray(obj)) {
    obj.forEach(absolutizeUrls);
  } else if (obj && typeof obj === 'object') {
    for (const key of Object.keys(obj)) {
      const val = obj[key];
      if (key === 'url' && typeof val === 'string' && val.startsWith('/')) {
        obj[key] = window.location.origin + val;
      } else {
        absolutizeUrls(val);
      }
    }
  }
}

// Served stores are zarr v3, but zarrita probes v2 metadata first on each new
// store; answer those probes locally so they cost no request or console 404.
const V2_METADATA = /^\/spatial-data\/.*\/\.(zattrs|zarray|zgroup)$/;
const nativeFetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  const url = new URL(
    input instanceof Request ? input.url : input,
    window.location.origin,
  );
  if (url.origin === window.location.origin && V2_METADATA.test(url.pathname)) {
    return Promise.resolve(new Response(null, { status: 404 }));
  }
  return nativeFetch(input, init);
};

// Vitessce 4.0.1 grid geometry (VitessceGrid padding and margin, 12 columns)
// and card chrome around spatial canvas, measured headless.
const GRID_PADDING = 10;
const GRID_MARGIN = 5;
const GRID_COLS = 12;
const CARD_CHROME = [12, 44];
// Must match generate_spatial_config.fit_zoom's assumed panel.
const REFERENCE_PANEL = [800, 450];
const FIT_MARGIN = 0.95;

// Refit generator's zoom (800x450 panel, centred on image, so target is half
// image size) to real panel; other zooms, e.g. headless probe's, are kept.
function fitSpatialZoom(config, width, height) {
  const layout = config.layout || [];
  const view = layout.find((v) => v.component === 'spatialBeta');
  const space = config.coordinationSpace || {};
  const scopes = view?.coordinationScopes || {};
  const zooms = space.spatialZoom || {};
  const xs = space.spatialTargetX || {};
  const ys = space.spatialTargetY || {};
  const zoom = zooms[scopes.spatialZoom];
  const imageW = 2 * xs[scopes.spatialTargetX];
  const imageH = 2 * ys[scopes.spatialTargetY];
  if (typeof zoom !== 'number' || !(imageW > 0) || !(imageH > 0)) return;
  const reference = Math.log2(
    Math.min(REFERENCE_PANEL[0] / imageW, REFERENCE_PANEL[1] / imageH),
  );
  if (Math.abs(zoom - reference) > 1e-6) return;
  const rows = Math.max(...layout.map((v) => v.y + v.h));
  const colWidth =
    (width - 2 * GRID_PADDING - (GRID_COLS - 1) * GRID_MARGIN) / GRID_COLS;
  const rowHeight =
    (height - 2 * GRID_PADDING - (rows - 1) * GRID_MARGIN) / rows;
  const panelW =
    view.w * colWidth + (view.w - 1) * GRID_MARGIN - CARD_CHROME[0];
  const panelH =
    view.h * rowHeight + (view.h - 1) * GRID_MARGIN - CARD_CHROME[1];
  if (!(panelW > 0) || !(panelH > 0)) return;
  zooms[scopes.spatialZoom] = Math.log2(
    FIT_MARGIN * Math.min(panelW / imageW, panelH / imageH),
  );
}

async function main() {
  if (!configUrl) {
    showMessage('No config specified. Use ?config=<url>.');
    return;
  }
  try {
    const response = await fetch(configUrl);
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    const config = await response.json();
    absolutizeUrls(config);
    fitSpatialZoom(config, window.innerWidth, window.innerHeight);
    root.render(
      React.createElement(Vitessce, {
        config,
        theme: 'light',
        // Vitessce needs explicit pixel height; it does not fill its parent.
        height: window.innerHeight,
      }),
    );
  } catch (err) {
    showMessage(`Failed to load config from ${configUrl}: ${err}`);
  }
}

main();
