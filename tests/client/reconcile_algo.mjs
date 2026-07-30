// The timeline reconcile algorithm (app.js _reconcileNotes), driven against a minimal fake DOM.
//
// Run via tests/client/test_reconcile.py, or directly: node tests/client/reconcile_algo.mjs
//
// _reconcileNotes replaced `notesEl.innerHTML = notes.map(feedNoteHtml).join('')`. That one line is most of
// why the timeline looked unstable: entering home/global draws TWICE — once from the Store cache, then
// again on EOSE — so every card the user was already looking at was destroyed and rebuilt a moment later.
// Rebuilt <img>s go back to being unloaded, so all the media re-reserves and re-decodes, and
// `.note{animation:fade}` re-runs on all 200 cards at once, which is the "flash".
//
// The assertions that matter most are the REUSE ones: they check node IDENTITY survives a redraw, which is
// the property the whole fix rests on and the one a correct-looking-but-secretly-rebuilding implementation
// would fail. `full reversal` and `interleaved insert` are the cases a naive single-pass walk gets wrong.
//
// The algorithm below is a COPY of the one in app.js. Keep the two in step by hand — it is fifteen lines,
// and the alternative is loading 18k lines of browser-coupled app.js into node.

class El {
  constructor(key){ this.dataset = key ? {key} : {}; this.children=[]; this.parent=null; this.uid=El.n++; }
  get firstElementChild(){ return this.children[0] || null; }
  get nextElementSibling(){
    const c = this.parent && this.parent.children; if(!c) return null;
    return c[c.indexOf(this)+1] || null;
  }
  insertBefore(node, ref){
    if(node.parent){ const c=node.parent.children; c.splice(c.indexOf(node),1); }
    node.parent=this;
    const i = ref ? this.children.indexOf(ref) : this.children.length;
    this.children.splice(i<0 ? this.children.length : i, 0, node);
  }
  remove(){ if(this.parent){ const c=this.parent.children; c.splice(c.indexOf(this),1); this.parent=null; } }
  set innerHTML(v){ this.children.forEach(c=>{ c.parent=null; }); this.children=[]; this._html=v; }
}
El.n = 0;

// ---- verbatim from app.js ------------------------------------------------------------------------
const _noteKey = ev => ev.kind===6 ? ((ev.tags.find(t=>t[0]==='e')||[])[1]||ev.id) : ev.id;
const _noteNode = ev => new El(_noteKey(ev));

function _reconcileNotes(box, notes, emptyMsg){
  const have=new Map();
  for(const el of [...box.children]){
    const k=el.dataset && el.dataset.key;
    if(k && !have.has(k)) have.set(k, el); else el.remove();
  }
  const seen=new Set(), want=[];
  for(const ev of notes){ const k=_noteKey(ev); if(seen.has(k)) continue; seen.add(k); want.push([k,ev]); }
  if(!want.length){
    for(const el of have.values()) el.remove();
    box.innerHTML = `<div class="empty">${emptyMsg}</div>`;
    return;
  }
  let ref=box.firstElementChild;
  for(const [k,ev] of want){
    const node=have.get(k);
    if(node===ref){ ref=ref.nextElementSibling; continue; }
    const el = node || _noteNode(ev);
    if(el) box.insertBefore(el, ref);
  }
  for(const [k,el] of have) if(!seen.has(k)) el.remove();
}
// -------------------------------------------------------------------------------------------------

const n = (id, kind=1, tgt) => ({ id, kind, tags: tgt ? [['e', tgt]] : [] });
const keys = box => box.children.map(c=>c.dataset.key);
const uids = box => box.children.map(c=>c.uid);
let fails = 0;
function check(name, got, want){
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if(!ok) fails++;
  console.log(`${ok?'PASS':'FAIL'} ${name}` + (ok ? '' :
    `\n     got  ${JSON.stringify(got)}\n     want ${JSON.stringify(want)}`));
}

let box = new El();
_reconcileNotes(box, [n('a'),n('b'),n('c')], 'none');
check('fresh draw order', keys(box), ['a','b','c']);

// The EOSE redraw: an identical list must reuse every node. This is the whole point of the change.
let before = uids(box);
_reconcileNotes(box, [n('a'),n('b'),n('c')], 'none');
check('identical redraw reuses nodes', uids(box), before);
check('identical redraw keeps order', keys(box), ['a','b','c']);

before = uids(box);
_reconcileNotes(box, [n('z'),n('a'),n('b'),n('c')], 'none');
check('prepend order', keys(box), ['z','a','b','c']);
check('prepend reused the old three', uids(box).slice(1), before);

_reconcileNotes(box, [n('z'),n('a'),n('c')], 'none');
check('removal from the middle', keys(box), ['z','a','c']);

_reconcileNotes(box, [n('c'),n('z'),n('a')], 'none');
check('reorder', keys(box), ['c','z','a']);

box = new El(); _reconcileNotes(box, [n('1'),n('2'),n('3'),n('4')], 'none');
_reconcileNotes(box, [n('4'),n('3'),n('2'),n('1')], 'none');
check('full reversal', keys(box), ['4','3','2','1']);

// Two reposts of ONE note render identical content, so they must collapse to a single card.
box = new El();
_reconcileNotes(box, [n('r1',6,'orig'), n('r2',6,'orig'), n('x')], 'none');
check('repost dedupe by target', keys(box), ['orig','x']);

// Scaffolding (.empty, .load-sentinel) carries no data-key and must not survive as a phantom card.
box = new El(); box.insertBefore(new El(null), null);
_reconcileNotes(box, [n('a')], 'none');
check('unkeyed scaffolding removed', keys(box), ['a']);

box = new El(); _reconcileNotes(box, [n('a'),n('b')], 'none');
_reconcileNotes(box, [], 'nothing here');
check('empty list clears the cards', keys(box), []);
check('empty list shows the message', /nothing here/.test(box._html||''), true);

box = new El(); _reconcileNotes(box, [n('a'),n('c'),n('e')], 'none');
before = uids(box);
_reconcileNotes(box, [n('a'),n('b'),n('c'),n('d'),n('e')], 'none');
check('interleaved insert order', keys(box), ['a','b','c','d','e']);
check('interleaved insert reused a, c and e',
      [box.children[0].uid, box.children[2].uid, box.children[4].uid], before);

console.log(`\nFAILURES: ${fails}`);
process.exit(fails ? 1 : 0);
