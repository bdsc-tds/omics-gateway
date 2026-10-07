// Script setting cellxgene's default colouring and sidebar layout, injected by
// gateway proxy

// Relies on cellxgene 1.3.0 internals (React 17 fibers, Redux store, sidebar
// markup); breakage leaves cellxgene's own defaults
(() => {
  // Obs column coloured on load, when dataset has it
  const DEFAULT_COLOR = 'pred_cell_type';
  // Obs columns always shown, in this order; rest go in collapsed panel
  const MAIN_METADATA = ['Disease', 'Tissue', 'Sex', 'Donor', 'pred_cell_type'];

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

  // Redux store, from Provider above app's root element
  function findStore() {
    for (
      let fiber = fiberOf(document.getElementById('root')?.firstElementChild);
      fiber;
      fiber = fiber.return
    ) {
      const store = fiber.memoizedProps?.store;
      if (typeof store?.dispatch === 'function') return store;
    }
    return null;
  }

  // Colour by DEFAULT_COLOR once schema loads, unless user already chose
  function colourByDefault(store) {
    const unsubscribe = store.subscribe(apply);
    apply();
    function apply() {
      const state = store.getState();
      const schema = state.annoMatrix?.schema;
      if (!schema) return;
      unsubscribe();
      // Reducer toggles: same action on current accessor resets colouring
      if (!schema.annotations.obsByName[DEFAULT_COLOR]) return;
      if (state.colors?.colorAccessor) return;
      store.dispatch({
        type: 'color by categorical metadata',
        colorAccessor: DEFAULT_COLOR,
      });
    }
  }

  // Obs field of sidebar item: category's metadataField or histogram's field
  function fieldOf(element) {
    for (let fiber = fiberOf(element); fiber; fiber = fiber.return) {
      const props = fiber.memoizedProps;
      if (typeof props?.metadataField === 'string') return props.metadataField;
      if (props?.isObs && typeof props.field === 'string') return props.field;
    }
    return null;
  }

  // Scroll container holding categorical and continuous sections; Categories
  // component is only one keeping expandedCats in state
  function findSidebar() {
    for (const div of document.querySelectorAll(
      '#root div[style*="overflow-y: auto"]',
    )) {
      for (
        let fiber = fiberOf(div.firstElementChild);
        fiber && fiber.stateNode !== div;
        fiber = fiber.return
      ) {
        if (fiber.stateNode?.state?.expandedCats instanceof Set) return div;
      }
    }
    return null;
  }

  // Tag each section item with its group, which stylesheet orders and hides
  function tagItems(sidebar, store) {
    const obsByName =
      store.getState().annoMatrix?.schema?.annotations.obsByName;
    for (const section of sidebar.children) {
      if (section.classList.contains('cxg-other-toggle')) continue;
      for (const item of section.children) {
        if (item.querySelector('[data-testid="open-annotation-dialog"]')) {
          item.dataset.cxgGroup = 'top';
          continue;
        }
        const field = fieldOf(item);
        const rank = MAIN_METADATA.indexOf(field);
        if (rank >= 0) {
          item.dataset.cxgGroup = 'main';
          item.dataset.cxgRank = String(rank);
        } else if (obsByName?.[field]?.writable) {
          // User-created categories stay visible, else new one vanishes
          item.dataset.cxgGroup = 'user';
        } else {
          item.dataset.cxgGroup = 'other';
        }
      }
    }
  }

  // Header toggling collapsed panel of non-main metadata
  function addToggle(sidebar) {
    const toggle = document.createElement('div');
    toggle.className = 'cxg-other-toggle';
    toggle.setAttribute('role', 'button');
    toggle.tabIndex = 0;
    toggle.textContent = 'Other metadata';
    const flip = () => {
      const collapsed = sidebar.classList.toggle('cxg-collapsed');
      toggle.setAttribute('aria-expanded', String(!collapsed));
    };
    toggle.addEventListener('click', flip);
    toggle.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        flip();
      }
    });
    toggle.setAttribute('aria-expanded', 'false');
    sidebar.appendChild(toggle);
  }

  // Order: create button, MAIN_METADATA, user categories, toggle, rest
  function addStyles() {
    const style = document.createElement('style');
    const ranks = MAIN_METADATA.map(
      (_, rank) =>
        `.cxg-sidebar [data-cxg-rank="${rank}"] { order: ${rank - 100}; }`,
    ).join('\n');
    // Sections flattened so categories and histograms order as one list;
    // categorical section's own padding goes with it, hence item padding
    style.textContent = `
      .cxg-sidebar { display: flex; flex-direction: column; padding-top: 10px; }
      .cxg-sidebar > :not(.cxg-other-toggle) { display: contents; }
      .cxg-sidebar > * > * { flex-shrink: 0; }
      .cxg-sidebar > :first-child > * { padding-left: 10px; padding-right: 10px; }
      .cxg-sidebar [data-cxg-group="top"] { order: -1000; }
      ${ranks}
      .cxg-sidebar [data-cxg-group="user"] { order: -1; }
      .cxg-other-toggle {
        order: 0; flex-shrink: 0; cursor: pointer; font-weight: 700;
        margin: 10px 0; padding: 6px 10px; border-top: 1px solid #ccc;
        user-select: none;
      }
      .cxg-other-toggle::after { content: " \\25BE"; }
      .cxg-sidebar.cxg-collapsed .cxg-other-toggle::after { content: " \\25B8"; }
      .cxg-sidebar [data-cxg-group="other"] { order: 1; }
      .cxg-sidebar.cxg-collapsed [data-cxg-group="other"] { display: none; }
    `;
    document.head.appendChild(style);
  }

  // Flatten sidebar, then retag items as React swaps their root nodes
  function arrangeSidebar(sidebar, store) {
    addStyles();
    sidebar.classList.add('cxg-sidebar', 'cxg-collapsed');
    addToggle(sidebar);
    tagItems(sidebar, store);
    const observer = new MutationObserver(() => tagItems(sidebar, store));
    for (const section of sidebar.children) {
      if (!section.classList.contains('cxg-other-toggle')) {
        observer.observe(section, { childList: true });
      }
    }
  }

  // Script runs before deferred bundle, so wait for store, then sidebar
  let store = null;
  const root = document.getElementById('root');
  if (!root) return;
  const watcher = new MutationObserver(() => {
    if (!store) {
      store = findStore();
      if (store) colourByDefault(store);
    }
    if (!store) return;
    const sidebar = findSidebar();
    if (!sidebar) return;
    watcher.disconnect();
    arrangeSidebar(sidebar, store);
  });
  watcher.observe(root, { childList: true, subtree: true });
})();
