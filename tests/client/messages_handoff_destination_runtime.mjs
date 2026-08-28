/* Simulate the destination half of a real frame handoff before concord.js is loaded. */
import fs from 'node:fs';
import vm from 'node:vm';

/* A path argument lets packaging CI execute the same behavior against the bytes inside the
 * Electron bundle.  With no argument this remains the fast source-tree regression test. */
const sourcePath=process.argv[2]
  ? new URL(`file://${process.cwd()}/${process.argv[2].replace(/^\/+/, '')}`)
  : new URL('../../static/js/client/concord.js',import.meta.url);
const source=fs.readFileSync(sourcePath,'utf8');
const rooms=[{communityId:'community:armada',name:'Armada'}];
const local=new Map([['pc.concord.invites',JSON.stringify(rooms)]]),session=new Map();
const window={__pcConcordHandoff:{room:'community:armada',channel:'support',
  mobileChatOpen:true,mobileDrawerOpen:false,scroll:{top:417,height:1200,pinned:false}}};
const document={querySelector:()=>null,createElement:()=>({dataset:{}}),
  head:{appendChild(){}},documentElement:{appendChild(){}}};
const context={window,document,
  localStorage:{getItem:k=>local.get(k)||null,setItem:(k,v)=>local.set(k,String(v))},
  sessionStorage:{getItem:k=>session.get(k)||null,setItem:(k,v)=>session.set(k,String(v))},
  URL,Blob,fetch:async()=>({ok:false}),setTimeout:()=>0,clearTimeout(){},
  requestAnimationFrame:f=>f(),console};
vm.createContext(context);vm.runInContext(source,context);

if(window.__pcConcordHandoff!==undefined)throw Error('one-shot handoff was not cleared');
const adopted=window.PCConcord.handoffState();
if(adopted.room!=='community:armada'||adopted.channel!=='support')
  throw Error('destination reset the selected community/channel: '+JSON.stringify(adopted));
if(adopted.scroll.top!==417||adopted.scroll.pinned!==false)
  throw Error('destination reset Communities scroll state: '+JSON.stringify(adopted.scroll));
console.log('messages handoff destination runtime: ok');
