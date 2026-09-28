/**
 * Entry point for self-hosted Vitessce spatial viewer.
 *
 * Vite bundles this file (with React, Vitessce and their dependencies) into
 * static assets under `cellxgene_gateway/static/vitessce/`, which
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
const configUrl = new URLSearchParams(window.location.search).get('config') || '';
const root = createRoot(document.getElementById('root'));

function showMessage(text) {
  root.render(
    React.createElement('div', { id: 'message' }, text)
  );
}

// Configs keep data URLs root-relative (/spatial-data/…), host-independent;
// Vitessce loaders call `new URL(url)` with no base, so resolve on origin.
function absolutizeUrls(obj) {
  if (Array.isArray(obj)) {
    obj.forEach(absolutizeUrls);
  } else if (obj && typeof obj === 'object') {
    for (const key of Object.keys(obj)) {
      const val = obj[key];
      if (key === 'url' && typeof val === 'string'
          && val.startsWith('/')) {
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
  const url = new URL(input instanceof Request ? input.url : input,
    window.location.origin);
  if (url.origin === window.location.origin
      && V2_METADATA.test(url.pathname)) {
    return Promise.resolve(new Response(null, { status: 404 }));
  }
  return nativeFetch(input, init);
};

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
    root.render(
      React.createElement(Vitessce, {
        config,
        theme: 'light',
        // Vitessce needs explicit pixel height; it does not fill its parent.
        height: window.innerHeight,
      })
    );
  } catch (err) {
    showMessage(`Failed to load config from ${configUrl}: ${err}`);
  }
}

main();
