/* Execute the shipped This Computer renderer and click a video tile.
 * A source-string assertion cannot catch a variable declared in a sibling function: the list
 * paints successfully and the ReferenceError occurs only after the person clicks a file. */
'use strict';
const path = require('path');
const root = path.resolve(__dirname, '../..');

let nativeOpens = 0, chooser = null;
global.pcHost = {
  list: async () => ({ parent:null, entries:[{
    path:'/home/test/Videos/demo.mp4', name:'demo.mp4', dir:false,
    mime:'video/mp4', size:1234, mtime:10,
  }]}),
  open: async () => { nativeOpens++; return {ok:true}; },
};

function button(){ return {disabled:false, onclick:null}; }
const select = button();
const card = {
  dataset:{p:'/home/test/Videos/demo.mp4', d:''}, onclick:null, oncontextmenu:null,
  querySelector: s => s === '.hf-select' ? select : null,
};
const controls = {'.hf-hidden':button(), '.hf-new':button()};
const grid = { querySelectorAll: () => [card] };
const pane = {
  isConnected:true, _html:'',
  set innerHTML(v){ this._html=String(v); }, get innerHTML(){ return this._html; },
  querySelector(s){ if(s==='#hf-grid')return grid; return controls[s]||null; },
  querySelectorAll(s){ return s==='#hf-grid .file-card[data-p]'?[card]:[]; },
};

const hostfiles = require(path.join(root, 'static/js/client/hostfiles.js'));
hostfiles.enter('/home/test/Videos');
hostfiles.render(pane, {
  view:'tiles', cmp:()=>((a,b)=>String(a.name).localeCompare(String(b.name))),
  fmtBytes:n=>String(n), icon:()=>'🎬', typeName:()=> 'MP4 video',
  openable:(name,mime)=>name==='demo.mp4'&&mime==='video/mp4',
  openFile:(file,name,openHere)=>{ chooser={file,name,openHere}; },
  toast:why=>{ throw new Error('unexpected toast: '+why); },
  prompt:async()=>'', confirm:async()=>false,
}).then(async()=>{
  if(typeof card.onclick!=='function')throw new Error('video tile was not bound');
  card.onclick({ctrlKey:false,metaKey:false,shiftKey:false,preventDefault(){}});
  if(!chooser)throw new Error('video did not reach the open-with selector');
  if(chooser.file!=='/home/test/Videos/demo.mp4'||chooser.name!=='demo.mp4')
    throw new Error('selector received the wrong file');
  if(nativeOpens!==0)throw new Error('native opener ran before a choice was made');
  await chooser.openHere();
  if(nativeOpens!==1)throw new Error('This computer choice did not open the file');
  console.log('This Computer video click holds');
}).catch(e=>{ console.error(e&&e.stack||e); process.exitCode=1; });
