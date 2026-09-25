import vm from 'node:vm';
import assert from 'node:assert/strict';
import { clientSourceAt, installStateGlobals } from './client_source.mjs';

/* Files → search → click a result. Runs the SHIPPED `_renderFilesEverywhere` (cut from the client
 * source, not copied) with the three sources stubbed, then clicks the hit the way a person does.
 * The report was "clicking on a search result does not open it": a My Computer FILE hit only
 * moved Files to the folder above, so the thing you searched for never opened. */
const source = clientSourceAt(process.env.PC_SEARCH_TEST_SOURCE
  || new URL('../../static/js/client/app.js', import.meta.url));
const start = source.indexOf('  async function _renderFilesEverywhere(');
const end = source.indexOf('  async function renderPublicFiles(', start);
assert(start >= 0 && end > start, 'the unified search function was not found');

const HITS = [
  { name:'report.pdf', path:'/home/me/Documents/report.pdf', size:10, mtime:5 },
  { name:'Documents', path:'/home/me/Documents', dir:true },
];
const calls = { opened:[], entered:[], renders:0, hostOpen:[] };
const results = { innerHTML:'' };
const status = { textContent:'' };
const pane = { innerHTML:'', isConnected:true };
const ctx = {
  console, Promise, String, Number, Object, Array, JSON,
  _filesQ:'report', ME:{ pubkey:'ab'.repeat(32) }, _filesGridList:[],
  _hostOn:false, _syncRoot:'', _syncPath:'', _filesFolder:null, _fxMobileSource:'',
  _fxSearchSeq:0, _fxHist:[], _syncPairs:[],
  _fxSideHTML:()=>'', _fxBarHTML:()=>'', _fxWhere:()=>'', _fxBindSide(){}, _fxBindBar(){},
  mediaServer:()=>'', _adoptSyncPairs(){}, _syncManifest:async()=>({}),
  _fxBlobName:b=>b.name, _fxMatch:()=>true, FilesIdx:{ folderOf:()=>'' },
  _fxCompare:()=>(a,b)=>String(a.name).localeCompare(String(b.name)), _fxSearchKey:()=>'',
  enc:String, _fxIcon:()=>'', _fxBytes:n=>String(n), _fxWhen:()=>'', _fxRemember(){},
  mimeForName:n=>/\.pdf$/.test(n) ? 'application/pdf' : '',
  toast:m=>{ throw new Error('toast: ' + m); },
  _hostFs:()=>({ enter:p=>calls.entered.push(p), parentPath:p=>p.replace(/\/[^/]*$/,'') || '/' }),
  renderBlossom:()=>{ calls.renders++; },
  _openHostFile:(p, name, openHere, mime)=>{ calls.opened.push({ p, name, mime, openHere }); },
  $:sel=>sel === '#fx-search-results' ? results : sel === '.fx-search-status' ? status : null,
  $$:(sel)=>{
    if(sel !== '.fx-search-hit') return [];
    const n = (results.innerHTML.match(/data-hit="/g) || []).length;
    ctx._buttons = Array.from({ length:n }, (_, i) => ({ dataset:{ hit:String(i) } }));
    return ctx._buttons;
  },
};
ctx.window = ctx;
ctx.pcHost = { search:async()=>HITS.map(h=>({ ...h })), open:async p=>{ calls.hostOpen.push(p); return { ok:true }; } };
vm.createContext(installStateGlobals(ctx) && ctx);
vm.runInContext('let _fxSearchSeq=0;' + source.slice(start, end) + ';globalThis._run=_renderFilesEverywhere;', ctx);

await ctx._run(pane);
assert.match(status.textContent, /^2 results/, status.textContent);
// Rows are sorted folders-first, so find each hit by what it is, not by position.
const order = (results.innerHTML.match(/data-hit="\d+"[^]*?fx-search-name">([^<]*)</g) || [])
  .map(s => s.replace(/^[^]*fx-search-name">/, '').replace(/<$/, ''));
const fileBtn = ctx._buttons[order.indexOf('report.pdf')], dirBtn = ctx._buttons[order.indexOf('Documents')];
assert(fileBtn && dirBtn, 'both hits rendered: ' + JSON.stringify(order));

fileBtn.onclick();
assert.equal(calls.opened.length, 1, 'clicking a My Computer FILE result must open the file, not just its folder');
assert.equal(calls.opened[0].p, '/home/me/Documents/report.pdf');
assert.equal(calls.opened[0].name, 'report.pdf');
assert.equal(calls.opened[0].mime, 'application/pdf');
assert.deepEqual(calls.entered, ['/home/me/Documents'], 'Files still lands on the folder holding it');
assert.equal(ctx._hostOn, true);
await calls.opened[0].openHere();
assert.deepEqual(calls.hostOpen, ['/home/me/Documents/report.pdf'], '"This computer" hands over the same path');

dirBtn.onclick();
assert.equal(calls.opened.length, 1, 'a FOLDER result browses into the folder; it is not "opened" as a file');
assert.deepEqual(calls.entered.slice(-1), ['/home/me/Documents']);
console.log('unified search: a file hit opens, a folder hit browses');
