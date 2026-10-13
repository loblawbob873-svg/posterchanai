/* A Music window that went away without its "gone" being heard must not keep the desk's widget for ever.
 *
 * TV, 2026-10-13: the Music window was closed, its pagehide "gone" never arrived, and the Now-playing widget kept
 * saying "playing" over silence -- and sent every Play/Shuffle press to a window that no longer existed, so the
 * widget did nothing at all. Runs the SHIPPED os.js music-channel block (from `const MUSIC_CH` to `routeMusic`)
 * against a fake BroadcastChannel and a fake clock. */
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const src=fs.readFileSync(process.env.PC_OS_SOURCE||new URL('../../static/js/client/os.js',import.meta.url),'utf8');
const a=src.indexOf("const MUSIC_CH = 'pc-music-window';"), b=src.indexOf('function routeMusic(){');
assert.ok(a>0&&b>a,'music channel block not found');
const block=src.slice(a,b);

let now=1_000_000; const channels=[];
class BC{constructor(n){this.name=n;this.sent=[];channels.push(this);} postMessage(m){this.sent.push(m);} close(){}}
const local={presses:[],now:()=>null,shuffling:()=>false,toggle(){this.presses.push('toggle');},
  shuffle(){this.presses.push('shuffle');},next(){},prev(){},seek(){},pause(){}};
const refreshed=[];
const ctx={console,Math,Number,JSON,Promise,Set,Map,
  Date:{now:()=>now}, BroadcastChannel:BC, setInterval:()=>({unref(){}}), clearInterval(){},
  window:{addEventListener(){}, PCOSWin:{isWindow:()=>false, viewOf:()=>''}},
  PC:()=>({music:()=>local}), _extraOpensAsWindow:()=>true,
  _mounted:new Map([['el',{w:{type:'music'}}]]), _wgtRefreshOne:(el)=>refreshed.push(el)};
ctx.window.window=ctx.window;
vm.createContext(ctx);
vm.runInContext(block+'\n;this.__wp=_widgetPlayer;',ctx);
const ch=channels[0]; assert.ok(ch,'the desk opened no music channel');

// The Music window says it is playing.
ch.onmessage({data:{state:{now:{title:'Song',playing:true,pos:0,total:3,d:100,t:5},shuffling:false}}});
let P=ctx.__wp();
assert.equal(P.now().playing,true,'a Music window that is playing is what the widget shows');

// The window is closed and its "gone" is lost. Nine seconds of silence.
now+=9000;
P=ctx.__wp();
assert.equal(P.now(),null,'a Music window silent for 9s is still shown as playing (the stuck "playing")');
P.toggle(); P.shuffle();
assert.deepEqual(local.presses,['toggle','shuffle'],'Play/Shuffle went to a window that no longer exists, not the desk player');

// A Music window that keeps talking is never dropped.
ch.onmessage({data:{state:{now:{title:'Song',playing:true,pos:0,total:3,d:100,t:5}}}});
now+=3000; ch.onmessage({data:{state:{now:{title:'Song',playing:true,pos:0,total:3,d:100,t:8}}}});
now+=3000;
assert.equal(ctx.__wp().now().playing,true,'a live Music window was dropped between heartbeats');
console.log('music widget: a silent Music window lets go; a live one is kept');
