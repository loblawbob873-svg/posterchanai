import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { clientSource, clientSourceAt, installStateGlobals } from './client_source.mjs';

const source = fs.readFileSync(process.env.PC_OFFICE_TEST_SOURCE || new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const start = source.indexOf('  async function _officeSession(');
const end = source.indexOf('  async function _officeStoreDrive(', start);
assert(start >= 0 && end > start);
const mode = process.argv[2];
const controls = new Map();
const node = () => ({classList:{add(){}}, appendChild(){}, innerHTML:''});
const feed = node(), host = node();
const calls = {views:[], drops:0, home:0, closed:0, submitted:0};
const state = {tab:mode, folder:'Documents/Reports', syncRoot:'folder-1', syncPath:'Reports', hostPath:'/home/test/Reports'};
const ctx = {
  console, FormData, Blob, Promise, Date, encodeURIComponent,
  VIEW:mode === 'office' ? 'office' : 'blossom',
  _filesTab:state.tab, _filesFolder:state.folder, _syncRoot:state.syncRoot,
  _syncPath:state.syncPath, _hostPath:state.hostPath,
  _instanceBase:()=> 'https://instance.example', ensureAiSession:async()=>{}, enc:String,
  fetch:async(_url, options)=>{assert.equal(options.method,'DELETE');calls.drops++;return {ok:true};},
  toast:message=>{throw Error(message);},
  renderOfficeHome:()=>{calls.home++;},
  switchView:view=>{ctx.VIEW=view;calls.views.push(view);},
  document:{getElementById:()=>mode === 'modal' ? null : feed, createElement:()=>host},
  $:selector=>{
    if(!controls.has(selector)) controls.set(selector,{submit(){calls.submitted++;}});
    return controls.get(selector);
  },
  modal:(_html,mount)=>mount(host), closeModal:()=>{calls.closed++;},
};
ctx.window = {__PC:{authFetch:async()=>({ok:true,json:async()=>({id:'session-1',token:'token',editor_url:'https://office.example/editor',expires:123})})}};
if(mode === 'native') {
  ctx.PCOS = ctx.window.PCOS = {isOn:()=>true, openDoc:()=>({slot:host}), closeDoc:()=>{calls.closed++;}};
}
vm.createContext(installStateGlobals(ctx) && ctx);
vm.runInContext(source.slice(start,end),ctx);
const file = new Blob(['test document']);file.name='report.odt';
await ctx._officeSession(file,async()=>{});
assert.equal(calls.submitted,1,'editor was not opened');
assert.equal(typeof controls.get('#office-close').onclick,'function');
await controls.get('#office-close').onclick();
if(mode === 'native' || mode === 'modal') {
  assert.equal(calls.closed,1,'Close must dismiss the editor surface');
  assert.deepEqual(calls.views,[],'independent editor must leave Files in place');
} else if(mode === 'office') {
  assert.equal(calls.home,1,'Office launcher must still return to Office');
} else {
  assert.deepEqual(calls.views,['office','blossom'],'Close must return to the Files launcher');
  assert.equal(calls.home,0,'Files must not be replaced by the Office splash');
  assert.equal(ctx._filesTab,state.tab);
  assert.equal(ctx._filesFolder,state.folder);
  assert.equal(ctx._syncRoot,state.syncRoot);
  assert.equal(ctx._syncPath,state.syncPath);
  assert.equal(ctx._hostPath,state.hostPath);
}
assert.equal(calls.drops,1,'Close must release its Office session once');
console.log('Office Close returns to its launcher:',mode);
