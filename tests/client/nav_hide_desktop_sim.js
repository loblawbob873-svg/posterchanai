/* The DESKTOP half: does PosterChan OS's launcher drop a row switched off in Settings → Sidebar?
 *
 * Runs the SHIPPED static/js/client/os.js under node against a stub sidebar. This is its own file
 * rather than part of nav_hide_sim.js because os.js is a module that loads whole (the app.js block
 * has to be sliced), and because the failure it guards is a different one: the sidebar tidied
 * correctly while the desktop carried on showing every game, which is the preference not having
 * worked at all.
 *
 * Usage:  node nav_hide_desktop_sim.js '<json options>'   → prints a JSON transcript on stdout.
 */
const path = require('path');
const OS_JS = path.join(path.resolve(__dirname, '..', '..'), 'static', 'js', 'client', 'os.js');

const opt = JSON.parse(process.argv[2] || '{}');

class El {
  constructor(spec, parent){
    this._cls = new Set(String(spec.cls || '').split(/\s+/).filter(Boolean));
    this.id = spec.id || '';
    this.dataset = spec.view ? { view: spec.view } : {};
    this.label = spec.label || '';
    this.parent = parent || null;
    const self = this;
    this.classList = { contains: c => self._cls.has(c) };
  }
  // os.js reads the icon through `svg use` and the label through the row's <span>.
  querySelector(sel){
    if(sel === 'svg use') return { getAttribute: a => (a === 'href' ? '#i-x' : null) };
    if(sel === 'span'){
      const self = this;
      return { childNodes: [{ nodeType: 3, textContent: self.label }],
               cloneNode(){ return { querySelectorAll: () => [], textContent: self.label }; } };
    }
    throw new Error('stub querySelector: ' + sel);
  }
  closest(sel){
    const c = sel.replace('.', '');
    for(let n = this; n; n = n.parent) if(n._cls.has(c)) return n;
    return null;
  }
}

/* The sidebar, flattened: [{cls,id,view,label,group}] where `group` names the enclosing .nav-group's
 * classes (so a switched-off GROUP can be expressed, which is the case that was reported broken). */
const groups = new Map();
const rows = (opt.sidebar || []).map(spec => {
  let parent = null;
  if(spec.group != null){
    if(!groups.has(spec.group)) groups.set(spec.group, new El({ cls: 'nav-group ' + spec.group }, null));
    parent = groups.get(spec.group);
  }
  return new El(spec, parent);
});

global.window = {};
global.document = {
  addEventListener(){},
  querySelector(){ return null; },
  getElementById(id){ return rows.find(r => r.id === id) || null; },
  querySelectorAll(sel){
    if(sel === '.sidebar .nav .nav-item[data-view]') return rows.filter(r => r.dataset.view);
    return [];
  },
};
global.getComputedStyle = () => ({ zoom: '1' });

require(OS_JS);
const PCOS = window.PCOS;

/* EXTRAS are gated on `when()`, which needs a signed-in user and PC() methods; with neither they all
 * drop out, so what is asserted here is the sidebar-derived half — which is the half the switch
 * controls. `__music` / `__golive` shadow real rows and are covered by the static check in the
 * Python file. */
/* This drives the COMPOSITION (launchApps → computeLayout), which is what decides what a desktop
 * shows. It cannot drive `layout()` itself — that reads the account's document off the relay — so
 * the fact that the real call site passes launchApps() and not apps() is asserted separately, by
 * DesktopWiringTests.test_the_launcher_uses_launchApps_and_the_lookups_do_not. Both halves are
 * needed: this one would keep passing against `computeLayout(apps(), _doc)`. */
const launch = PCOS.__launchApps().map(a => a.view);
const lay = PCOS.__layout(PCOS.__launchApps(), opt.doc || {});
process.stdout.write(JSON.stringify({
  launch,
  desktop: lay.items.map(i => i.view),
  folders: lay.folders.map(f => ({ key: f.key, members: f.members.map(m => m.view) })),
}));
