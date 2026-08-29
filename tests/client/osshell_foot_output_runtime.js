'use strict';
const path=require('path');
let listener=null,reads=0;
global.window=global;
global.pcWM={windows:async()=>{reads++;return [];},subscribe:async()=>true,
  onEvent:fn=>{listener=fn;return()=>{listener=null;}}};
require(path.resolve(__dirname,'../../static/js/client/osshell.js'));
(async()=>{
  let paints=0;const off=await PCOSShell.watch(()=>{paints++;});
  const base={reads,paints};
  for(let i=0;i<100;i++)listener({name:'window',change:'title'});
  await Promise.resolve();
  if(reads!==base.reads||paints!==base.paints)
    throw Error(`Foot title burst caused duplicate refreshes: ${reads-base.reads}/${paints-base.paints}`);
  listener({name:'output',change:'unspecified'});await new Promise(r=>setTimeout(r,0));
  if(reads<=base.reads||paints<=base.paints)throw Error('non-window shell event no longer refreshes tray');
  off();console.log('Foot sustained-output watcher runtime: ok');
})().catch(e=>{console.error(e&&e.stack||e);process.exitCode=1;});
