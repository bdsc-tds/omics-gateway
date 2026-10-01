// Script to handle filtering menu toggle

document.addEventListener('DOMContentLoaded', () => {
  // Get references to sidebar, toggle button, and main content area
  const sidebarCol = document.getElementById('sidebarCol');
  const btn = document.getElementById('formToggle');
  const mainContent = document.getElementById('mainContent');
  // Matches filecrawl.css breakpoint, below which sidebar stacks above content
  const narrow = window.matchMedia('(max-width: 1199.98px)');

  // Update arrow direction to reflect current visibility
  const updateArrow = () => {
    const isHidden = getComputedStyle(sidebarCol).display === 'none';
    btn.innerHTML = isHidden ? '▶' : '◀';
  };

  btn.addEventListener('click', () => {
    if (narrow.matches) {
      // Stacked sidebar: open or close above content
      sidebarCol.classList.remove('d-none');
      mainContent.classList.remove('full-width');
      sidebarCol.classList.toggle('sidebar-open');
    } else {
      // Check current visibility state before toggling
      const isHidden = sidebarCol.classList.contains('d-none');
      // Show/hide sidebar
      sidebarCol.classList.toggle('d-none');
      // Expand main content when sidebar is hidden
      mainContent.classList.toggle('full-width', !isHidden);
    }
    updateArrow();
  });

  // Stacked sidebar closes when crossing breakpoint; arrow follows
  narrow.addEventListener('change', () => {
    sidebarCol.classList.remove('sidebar-open');
    updateArrow();
  });
  updateArrow();
});
