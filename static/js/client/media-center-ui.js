/* Presentation layer for the existing Media Center controls; preserves their handlers. */
(() => {
  'use strict';
  let scheduled = false;
  function enhance() {
    scheduled = false;
    for (const gallery of document.querySelectorAll('.mc-gallery')) {
      const tools = gallery.querySelector('.mc-tools'), heading = gallery.querySelector('.xdc-gal-top');
      if (tools && heading && !tools.closest('.mc-actions-menu')) {
        const menu = document.createElement('details'); menu.className = 'mc-actions-menu';
        const toggle = document.createElement('summary');
        toggle.setAttribute('aria-label', 'Media Center menu');
        toggle.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><span>Manage</span>';
        menu.append(toggle, tools); heading.append(menu);
        menu.addEventListener('keydown', event => {
          if (event.key === 'Escape') { event.preventDefault(); menu.open = false; toggle.focus(); }
        });
      }
      const trail = gallery.querySelector('.mc-folder-trail');
      if (trail) {
        trail.setAttribute('aria-label', 'Folder breadcrumb');
        const crumbs = [...trail.querySelectorAll('button')];
        for (const [index, crumb] of crumbs.entries()) {
          if (index === crumbs.length - 1) crumb.setAttribute('aria-current', 'page');
          else crumb.removeAttribute('aria-current');
        }
      }
    }
  }
  const observer = new MutationObserver(records => {
    if (scheduled || !records.some(record => record.target.closest?.('.mc-gallery') ||
      [...record.addedNodes].some(node => node.nodeType === 1 && (node.matches('.mc-gallery') || node.querySelector('.mc-gallery'))))) return;
    scheduled = true; requestAnimationFrame(enhance);
  });
  observer.observe(document.body, {childList: true, subtree: true});
  document.addEventListener('pointerdown', event => {
    for (const menu of document.querySelectorAll('.mc-actions-menu[open]'))
      if (!menu.contains(event.target)) menu.open = false;
  });
  enhance();
})();
