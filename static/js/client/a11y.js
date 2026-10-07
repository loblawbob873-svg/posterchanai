/* ACCESSIBILITY, ONE LAYER FOR EVERY SCREEN — the same shape as i18n.js, for the same reason.
 *
 * The client builds its controls inside HTML template literals across ~120 modules, and a control
 * drawn as an icon has always been NAMED by its `title` (the hover tooltip). That reaches a mouse
 * user and nobody else: a phone has no hover, and screen readers treat a lone title inconsistently
 * (TalkBack announces an icon-only button as just "button"). Rewriting ~250 call sites inside
 * template literals is the mechanical edit that breaks boots, so the name is completed here,
 * against the DOM, for every screen including next month's:
 *
 *   * an icon-only control (no letters or digits in its text) that has a `title` and no aria-label
 *     gets aria-label = title. The title was already the designed name; this only exposes it.
 *   * a non-native element that SAYS it is a control (role=button/link/tab/menuitem/option/
 *     checkbox/switch) and is focusable gets Enter (and Space, for the button-like roles) — the
 *     keys a real <button> answers to, so `role="button" tabindex="0"` is a complete control
 *     instead of half of one.
 *
 * It never invents a name: an icon with neither title nor aria-label is a bug at its source, and
 * tests/client/test_accessibility_every_view_full_app.py names it.
 */
(function(){
  'use strict';
  const LETTER = /[\p{L}\p{N}]/u;
  const CONTROLS = 'button,a[href],[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="option"],[role="checkbox"],[role="switch"]';

  /* `data-a11y-name` marks an aria-label THIS layer wrote, so a title changed in place later (a toggle
     that retitles itself: "react" → "remove your reaction") moves the name with it, while an
     aria-label written by the code that drew the control is never touched. */
  function nameIcon(el){
    const ours = el.hasAttribute('data-a11y-name');
    if(!ours && (el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby'))) return;
    const title = (el.getAttribute('title') || '').trim();
    if(!title || LETTER.test(el.textContent || '')){
      if(ours){ el.removeAttribute('aria-label'); el.removeAttribute('data-a11y-name'); }
      return;
    }
    if(el.getAttribute('aria-label') !== title) el.setAttribute('aria-label', title);
    if(!ours) el.setAttribute('data-a11y-name', '');
  }
  function sweep(root){
    if(!root || root.nodeType !== 1) return;
    if(root.matches && root.matches(CONTROLS)) nameIcon(root);
    const list = root.querySelectorAll ? root.querySelectorAll(CONTROLS) : [];
    for(let i = 0; i < list.length; i++) nameIcon(list[i]);
  }

  function start(){
    sweep(document.body);
    new MutationObserver(records => {
      for(const r of records){
        if(r.type === 'attributes'){ nameIcon(r.target); continue; }
        for(const n of r.addedNodes) sweep(n);
      }
    }).observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ['title']});
  }

  /* Enter/Space on a focused role-control presses it. Only the element that HAS focus and the role —
     never a key typed into a field inside one, and never a native control, which already does this. */
  const PRESS = /^(button|tab|menuitem|option|checkbox|switch)$/;
  document.addEventListener('keydown', e => {
    if(e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
    if(e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
    const t = e.target;
    if(!t || t.nodeType !== 1 || t.isContentEditable) return;
    if(/^(BUTTON|A|INPUT|SELECT|TEXTAREA|SUMMARY)$/.test(t.tagName)) return;
    const role = t.getAttribute('role') || '';
    const space = e.key !== 'Enter';
    if(role === 'link'){ if(space) return; }
    else if(!PRESS.test(role)) return;
    e.preventDefault();
    t.click();
  });

  if(document.body) start();
  else document.addEventListener('DOMContentLoaded', start, {once: true});
})();
