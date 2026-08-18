/* Run the SHIPPED left-nav hiding code (static/js/client/app.js) against a stub sidebar.
 *
 * app.js is one 28k-line IIFE with no exports, so the block is CUT OUT of the real file between the
 * two banner comments it carries for that purpose and evaluated here. That is deliberately not a
 * copy: a copy would keep passing after the original changed, which is the failure mode this whole
 * file exists to prevent.
 *
 * The sidebar it runs against is built from templates/client.html by the Python caller, so the rows,
 * their ids and the group names are the REAL ones — a renamed `#disc-toggle` changes the key the
 * mobile sheets look up, and that is exactly the kind of silent drift a hand-written fixture hides.
 *
 * There is no DOM here, so one is stubbed: `classList`, `dataset`, `closest` and the three selectors
 * the block actually uses. Small enough to read, which matters — a selector engine that quietly
 * matches nothing would make every assertion below pass.
 *
 * Usage:  node nav_hide_sim.js '<json options>'   → prints a JSON transcript on stdout.
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const APP = path.join(ROOT, 'static', 'js', 'client', 'app.js');

const opt = JSON.parse(process.argv[2] || '{}');

/* ---- the block, cut out of the shipped file --------------------------------------------------- */
const SRC = fs.readFileSync(APP, 'utf8');
const A = SRC.indexOf('/* ===== HIDING ROWS FROM THE LEFT NAV');
const B = SRC.indexOf('/* ===== end of the left-nav hiding block =====');
if (A < 0 || B < 0 || B < A) {
  console.error('nav_hide_sim: the slice markers are gone from app.js — see the banner comments');
  process.exit(2);
}
const BLOCK = SRC.slice(A, B);

/* ---- the stub DOM ----------------------------------------------------------------------------- */
class El {
  constructor(cls, opts){
    opts = opts || {};
    this._cls = new Set(String(cls || '').split(/\s+/).filter(Boolean));
    this.id = opts.id || '';
    this.dataset = opts.view ? { view: opts.view } : {};
    this.children = [];
    this.parent = null;
    this.label = opts.label || '';
    this.disabled = !!opts.disabled;
    this.checked = !!opts.checked;
    const self = this;
    this.classList = {
      contains: c => self._cls.has(c),
      add: c => self._cls.add(c),
      remove: c => self._cls.delete(c),
      toggle(c, on){ if(on === undefined) on = !self._cls.has(c); on ? self._cls.add(c) : self._cls.delete(c); return on; },
    };
  }
  add(kid){ kid.parent = this; this.children.push(kid); return kid; }
  // Only the sidebar's own shape: a nav-item's <span> holds the label text plus badge elements.
  querySelector(sel){
    if(sel === '.nav-grouphd' || sel === 'use') return this._q1(sel);
    if(sel !== 'span') throw new Error('stub querySelector: ' + sel);
    if(!this._cls.has('nav-item')) return null;
    const self = this;
    return { childNodes: [{ nodeType: 3, textContent: self.label }],
             cloneNode(){ return { querySelectorAll: () => [], textContent: self.label }; } };
  }
  querySelectorAll(sel){
    if(sel !== '.nav-item.sub') throw new Error('stub querySelectorAll: ' + sel);
    return descend(this).filter(e => e._cls.has('nav-item') && e._cls.has('sub'));
  }
  // applyNavOrder resolves a group's key off its header; a use-element lookup answers null (the
  // stub carries no SVG and mobileNavChoices tolerates that).
  _q1(sel){
    if(sel === '.nav-grouphd') return descend(this).find(e => e._cls.has('nav-grouphd')) || null;
    if(sel === 'use') return null;
    return null;
  }
  closest(sel){
    const c = sel.replace('.', '');
    for(let n = this; n; n = n.parent) if(n._cls.has(c)) return n;
    return null;
  }
  get cls(){ return [...this._cls].join(' '); }
}
function descend(root){
  const out = [];
  (function walk(n){ for(const k of n.children){ out.push(k); walk(k); } })(root);
  return out;
}

/* The sidebar, as templates/client.html has it. `spec` is [{cls,id,view,label,children?}]. */
const NAV = new El('nav');
const SIDEBAR = new El('sidebar');
SIDEBAR.add(NAV);
function build(host, rows){
  for(const r of (rows || [])){
    const el = host.add(new El(r.cls, r));
    if(r.children) build(el, r.children);
  }
}
build(NAV, opt.sidebar || []);
const ALL = descend(SIDEBAR);

global.document = {
  querySelectorAll(sel){
    if(sel === '.sidebar .nav .nav-item') return ALL.filter(e => e._cls.has('nav-item'));
    // The bottom-bar choices: every plain view row (the block grew mobileNav* in 2026-08).
    if(sel === '.sidebar .nav .nav-item[data-view]')
      return ALL.filter(e => e._cls.has('nav-item') && e.dataset && e.dataset.view);
    throw new Error('stub document.querySelectorAll: ' + sel);
  },
  // applyNavOrder wants the nav CONTAINER; applyMobileNav wants the phone bar. The nav is the
  // stub's own, so ordering is exercised for real; there is no bar here and null makes that path
  // the no-op it is on any page without one.
  querySelector(sel){
    if(sel === '.sidebar .nav') return NAV;
    if(sel === '.mobilenav') return null;
    return null;
  },
  createComment(){ return { _comment: true }; },
  // The footer's Report-a-Bug button: absent from the stub sidebar unless a case plants one.
  getElementById(id){ return (global.__els && global.__els[id]) || null; },
};
// The container operations applyNavOrder performs on the stub nav.
NAV.insertBefore = function(el, anchor){
  const kids = NAV.children;
  const i = kids.indexOf(el); if(i >= 0) kids.splice(i, 1);
  const j = anchor ? kids.indexOf(anchor) : -1;
  if(j < 0) kids.push(el); else kids.splice(j, 0, el);
  el.parent = NAV; return el;
};
NAV.removeChild = function(el){ const i = NAV.children.indexOf(el); if(i >= 0) NAV.children.splice(i, 1); return el; };

/* ---- the globals the block reaches for -------------------------------------------------------- */
const store = Object.assign({}, opt.settings || {});
global.ClientSettings = { get: (k, d) => (store[k] === undefined ? d : store[k]), set: (k, v) => { store[k] = v; } };

global.VIEW = 'global';
global.switchView = () => {};
global.renderView = () => {};
global.toast = global.toast || (() => {});
const published = [];                       // every write that would go to pcai:client-prefs
global.saveClientPrefsNostr = patch => { published.push(patch); return Promise.resolve(); };
const _prefTouched = new Set();
global._prefTouched = _prefTouched;

global.enc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/* The editor's own DOM. `#nav-hide-list` is the one node `_wireNavHide` looks up, and the checkboxes
 * inside it are built by the caller from what `_navHideHtml` reported — so the wiring is driven by
 * the real markup's own key list rather than by a second list written here. */
let LIST = null;
global.$ = sel => (sel === '#nav-hide-list' ? LIST : null);
global.$$ = (sel, root) => {
  if(sel === '.nav-group') return ALL.filter(e => e._cls.has('nav-group'));
  if(sel === 'input[data-navkey]') return (root && root.boxes) || [];
  if(sel === '#nav-hide-list input[data-navkey]') return LIST ? LIST.boxes : [];
  throw new Error('stub $$: ' + sel);
};

eval(BLOCK);                                 // eslint-disable-line no-eval — the point of the harness

/* ---- the scenario ----------------------------------------------------------------------------- */
const out = { published, settings: store };

/* Boot applies the stored document before anything else — from localStorage, so the sidebar is tidy
 * before a relay has answered. Doing it here too is what makes a `settings`-only case a real test of
 * what the document can do, rather than a test of a sidebar nothing was ever applied to. */
applyNavHidden();
if(opt.hide) setNavHidden(opt.hide);

// What the sidebar looks like afterwards: a row is gone if it, or a container above it, is off.
function gone(el){
  for(let n = el; n; n = n.parent) if(n._cls.has('nav-off') || n._cls.has('hidden')) return true;
  return false;
}
out.visible = ALL.filter(e => e._cls.has('nav-item') && !gone(e)).map(e => _navKey(e) || e.label);
out.offClasses = ALL.filter(e => e._cls.has('nav-off')).map(e => e.id || _navKey(e) || e.cls);
out.rows = navRows().map(r => ({ key: r.key, label: r.label, locked: r.locked, off: r.off, group: r.group, sub: r.sub }));
out.hiddenSet = [...navHiddenSet()];
out.html = _navHideHtml();

/* Drive the EDITOR itself when asked: build its checkboxes from the keys `_navHideHtml` offered,
 * apply the caller's un-ticks, and fire the change handler the shipped code binds. */
if(opt.editor){
  const keys = [...out.html.matchAll(/data-navkey="([^"]*)"/g)].map(m => m[1]);
  const locked = new Set([...out.html.matchAll(/data-navkey="([^"]*)"[^>]*disabled/g)].map(m => m[1]));
  // `forge` adds switches the editor never drew — the DOM is not a guarantee about what the save
  // may write, and the locked rows have no switch at all now.
  const all = keys.concat((opt.editor.forge || []).filter(k => keys.indexOf(k) < 0));
  let handler = null;
  LIST = { boxes: all.map(k => ({ dataset: { navkey: k }, disabled: locked.has(k),
                                  checked: !navHiddenSet().has(k) })),
           addEventListener(ev, fn){ if(ev === 'change') handler = fn; } };
  _wireNavHide();
  for(const k of (opt.editor.uncheck || [])){ const b = LIST.boxes.find(x => x.dataset.navkey === k); if(b) b.checked = false; }
  for(const k of (opt.editor.check || [])){ const b = LIST.boxes.find(x => x.dataset.navkey === k); if(b) b.checked = true; }
  if(handler) handler();
  out.editorKeys = keys;
  out.editorLocked = [...locked];
  out.hiddenSet = [...navHiddenSet()];
  out.visible = ALL.filter(e => e._cls.has('nav-item') && !gone(e)).map(e => _navKey(e) || e.label);
}

/* Drive the ORDER when asked: the shipped setNavOrder against the stub sidebar, then read the nav
 * back as the sequence of movable units — exactly what a user sees top to bottom. */
function _navSequence(){
  const seq = [];
  for(const el of NAV.children){
    if(el._cls.has('nav-item') && !el._cls.has('sub')){ const k = _navKey(el); if(k) seq.push(k); }
    else if(el._cls.has('nav-group')){
      const hd = descend(el).find(e => e._cls.has('nav-grouphd'));
      const k = hd ? _navKey(hd) : ''; if(k) seq.push(k);
    }
  }
  return seq;
}
if(opt.order){
  setNavOrder(opt.order);
  out.navSequence = _navSequence();
  out.navOrderSaved = store.navOrder || null;
}
if(opt.tlHide){
  setTlHidden(opt.tlHide);
  out.tlHidden = [...tlHiddenSet()];
  out.tlSaved = store.tlHidden === undefined ? null : store.tlHidden;
}
if(opt.mobileNav){
  out.mobileNavChoices = mobileNavChoices().map(c => c.v);
  setMobileNav(opt.mobileNav);
  out.mobileNavSaved = store.mobileNav || null;
  out.mobileNavList = mobileNavList();
}

process.stdout.write(JSON.stringify(out));
