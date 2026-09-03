'use strict';
/* Drive the shipped focus lease against every surface class and adversarial completion order. */
const fs=require('fs'),path=require('path');
const src=fs.readFileSync(path.resolve(__dirname,'../../static/js/client/os.js'),'utf8');
const lease=src.slice(src.indexOf('  let _focusGeneration = 0;'),src.indexOf('  const _domCoveredNative'));
const at=src.indexOf('  function _focusNativeDecorated(id, token){');
let depth=0,end=-1;
for(let i=src.indexOf('{',at);i<src.length;i++){if(src[i]==='{')depth++;else if(src[i]==='}'&&--depth===0){end=i+1;break;}}
const nativeFocus=src.slice(at,end);
const pending=new Map(), focused=[];
const pcWM={
  focus:async id=>{focused.push(Number(id));return true;},
  decorate:id=>new Promise(resolve=>pending.set(Number(id),resolve)),
};
const window={pcWM};
const api=new Function('window','pcWM',`${lease}\n${nativeFocus}\nreturn {claim:_claimFocus,current:_focusCurrent,focus:_focusCompositorCurrent,native:_focusNativeDecorated};`)(window,pcWM);
const surfaces=[
  ['Social',10],['Profile',10],['Texts',10],['Concord',10],['Monero',10],['Office',10],
  ['Drafts',10],['Settings',10],['Notifications',10],['Start',10],['Tray',10],['Dialog',10],
  ['PosterChan popout',101],['Firefox',102],['Telegram',103],['Steam fullscreen game',104],['Terminal',105],
];
const native=id=>id!==10;
const flush=()=>new Promise(r=>setTimeout(r,0));
function ok(n,v){if(!v)throw new Error(n);console.log('  ok   '+n);}
(async()=>{
  for(let a=0;a<surfaces.length;a++)for(let b=0;b<surfaces.length;b++){
    focused.length=0;pending.clear();
    const [an,aid]=surfaces[a],[bn,bid]=surfaces[b];
    const old=api.claim();
    if(native(aid))api.native(aid,old);else await api.focus(aid,old);
    const newest=api.claim();
    if(native(bid)){api.native(bid,newest);pending.get(bid)();await flush();}
    else await api.focus(bid,newest);
    /* Relay rerenders, taskbar paint, reconnect updates, popup-close broadcasts and adoption events
       carry no explicit focus claim. Completing the older decoration afterwards is adversarial. */
    const stale=pending.get(aid);if(stale)stale();await flush();
    ok(an+' -> '+bn,focused.at(-1)===bid&&api.current(newest));
  }
  console.log('OK focus invariant '+surfaces.length+'x'+surfaces.length);
})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
