/* Run the SHIPPED static/js/client/torrents.js against a minimal DOM and check the one thing that
 * cannot be checked by reading it: that `attach()` converges.
 *
 * torrents.js enhances a view rendered by app.js, so it runs from a MutationObserver — which means
 * every node it inserts wakes it up again. If a single insert is not guarded, the observer feeds
 * itself and the tab locks up with nothing in any log. The Torrents view also repaints every two
 * seconds (app.js rebuilds the rows), so "attach after a repaint" is the ordinary case, not an edge.
 *
 * The DOM here is a stub, not jsdom (this repo vendors no node_modules). It implements exactly what
 * attach() touches, and it COUNTS mutations, so an unguarded insert shows up as a number.
 */
import fs from 'node:fs';
import path from 'node:path';

const SRC = path.resolve(process.argv[2]);

// ------------------------------------------------------------------ tiny DOM
let MUTATIONS = 0;
let observer = null;

class El {
  constructor(tag){
    this.tagName = String(tag||'div').toUpperCase();
    this.children = []; this.parentNode = null;
    this.classList = new Set(); this.dataset = {};
    this.id = ''; this.textContent = ''; this.title = ''; this.onclick = null;
    this.style = {};
  }
  get className(){ return [...this.classList].join(' '); }
  set className(v){ this.classList = new Set(String(v||'').split(/\s+/).filter(Boolean)); }
  get firstChild(){ return this.children[0] || null; }
  appendChild(c){ c.parentNode = this; this.children.push(c); MUTATIONS++; if(observer) observer(); return c; }
  insertBefore(c, ref){
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if(i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    MUTATIONS++; if(observer) observer(); return c;
  }
  remove(){ if(this.parentNode){ this.parentNode.children = this.parentNode.children.filter(x=>x!==this); MUTATIONS++; } }
  _all(){ const out=[]; for(const c of this.children){ out.push(c, ...c._all()); } return out; }
  _is(sel){
    if(sel.startsWith('#')) return this.id === sel.slice(1);
    if(sel.startsWith('.')) return this.classList.has(sel.slice(1));
    return this.tagName === sel.toUpperCase();
  }
  querySelectorAll(sel){
    const parts = sel.trim().split(/\s+/);
    let pool = this._all();
    for(let i=0;i<parts.length;i++){
      const hits = pool.filter(e=>e._is(parts[i]));
      pool = (i === parts.length-1) ? hits : hits.flatMap(e=>e._all());
    }
    return pool;
  }
  querySelector(sel){ return this.querySelectorAll(sel)[0] || null; }
}

const root = new El('body');
const head = new El('head');
const document = {
  readyState: 'complete',
  head,
  body: root,
  createElement: t => new El(t),
  addEventListener(){},
  querySelector: s => root.querySelector(s),
  querySelectorAll: s => root.querySelectorAll(s),
  getElementById: id => root._all().find(e=>e.id===id) || null,
};
globalThis.document = document;
globalThis.window = { __PC: { enc: s=>String(s), toast(){}, modal(){}, closeModal(){} } };
globalThis.console = console;
let rafQ = [];
globalThis.requestAnimationFrame = fn => { rafQ.push(fn); return rafQ.length; };
globalThis.setTimeout = (fn)=>{ return 0; };   // boot() only uses it to retry a missing #feed
globalThis.MutationObserver = class { constructor(cb){ this._cb=cb; } observe(){ observer = ()=>this._cb(); } };
globalThis.fetch = async () => ({ ok:true, json: async()=>({}) });

function flush(){ const q=rafQ; rafQ=[]; q.forEach(f=>f()); }

// ------------------------------------------------------------------ the view app.js renders
function paintTorrentsView(hashes){
  root.children = [];
  const feed = new El('div'); feed.id = 'feed'; root.appendChild(feed);
  const tabs = new El('div'); tabs.className = 'notif-tabs tor-tabs'; feed.appendChild(tabs);
  const add = new El('button'); add.className = 'btn btn-neon small tor-act'; add.id = 'tm-add';
  tabs.appendChild(add);
  const ref = new El('button'); ref.className = 'btn btn-ghost small tor-act'; ref.id = 'tm-refresh';
  tabs.appendChild(ref);
  const list = new El('div'); list.className = 'tm-list'; list.id = 'tm-list'; feed.appendChild(list);
  for(const h of hashes){
    const row = new El('div'); row.className = 'tm-item'; row.dataset.h = h;
    const acts = new El('div'); acts.className = 'tm-acts'; row.appendChild(acts);
    const tg = new El('button'); tg.className = 'btn btn-ghost small tm-toggle'; acts.appendChild(tg);
    list.appendChild(row);
  }
  return feed;
}

// ------------------------------------------------------------------ load + run
eval(fs.readFileSync(SRC, 'utf8'));
const PCT = globalThis.window.PCTorrents;

const fails = [];
const ok = (cond, msg) => { if(!cond) fails.push(msg); };

// 1. the view is up: attach adds its controls
paintTorrentsView(['aa', 'bb']);
PCT.attach();
ok(document.getElementById('tmx-feeds'), 'no Feeds button was added to the tab strip');
ok(root.querySelectorAll('.tmx-fbtn').length === 2, 'expected one Files button per torrent row, got '
   + root.querySelectorAll('.tmx-fbtn').length);
ok(document.getElementById('tm-add').dataset.tmx === '1', 'the Add button was not re-bound');

// 2. IT CONVERGES. Re-running attach (which is what every observer wake-up does) must change nothing
//    — one unguarded insert here is an observer feeding itself for ever.
const before = MUTATIONS;
for(let i=0;i<25;i++) PCT.attach();
ok(MUTATIONS === before, 'attach() mutated the DOM on a re-run (' + (MUTATIONS-before)
   + ' change(s)) — the MutationObserver would loop for ever');
ok(root.querySelectorAll('#tmx-feeds').length === 1, 'the Feeds button was added more than once');
ok(root.querySelectorAll('.tmx-fbtn').length === 2, 'Files buttons were duplicated');

// 3. the 2s repaint rebuilds the rows; attach must put its button back, once.
paintTorrentsView(['aa', 'bb', 'cc']);
PCT.attach(); PCT.attach();
ok(root.querySelectorAll('.tmx-fbtn').length === 3, 'after a repaint the Files buttons did not come '
   + 'back exactly once (got ' + root.querySelectorAll('.tmx-fbtn').length + ')');

// 4. the Files button knows which torrent it belongs to
const btns = root.querySelectorAll('.tmx-fbtn');
ok(btns.every(b => typeof b.onclick === 'function'), 'a Files button has no handler');
ok(root.querySelectorAll('.tm-item').length === 3, 'stub sanity');

// 5. OTHER VIEWS ARE NOT TOUCHED. #feed is shared by every screen in this client, so a module that
//    injects without checking decorates Notes, Mail and the timeline too.
root.children = [];
const feed = new El('div'); feed.id = 'feed'; root.appendChild(feed);
const note = new El('article'); note.className = 'note'; feed.appendChild(note);
const m0 = MUTATIONS;
PCT.attach();
ok(MUTATIONS === m0, 'attach() mutated a view that is not Torrents');

// 6. the observer path runs attach through requestAnimationFrame and does not throw
paintTorrentsView(['aa']);
flush();
ok(true, '');

if(fails.length){ console.error(fails.join('\n')); process.exit(1); }
console.log('ok');
