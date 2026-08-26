'use strict';

/* Native input for an APPROVED, live Remote Desktop session.
 *
 * This module owns no network and no permission state. The renderer only calls it after the host
 * pressed Allow for the authenticated WebRTC peer. It still validates every packet here: an input
 * bridge is too privileged to trust coordinates/keycodes arriving from a renderer verbatim.
 */
const { execFile } = require('child_process');

const KEY_CODES = new Set([
  1,14,15,28,29,42,54,56,57,58,97,100,103,105,106,108,111,
  ...Array.from({length:10},(_,i)=>i+2),       // 1..0
  ...Array.from({length:10},(_,i)=>i+16),      // q..p
  ...Array.from({length:9}, (_,i)=>i+30),      // a..l
  ...Array.from({length:7}, (_,i)=>i+44),      // z..m
  12,13,26,27,39,40,41,43,51,52,53,           // punctuation
]);
const BUTTONS = {0:'0', 1:'1', 2:'2'};
let lastAt=0, started=false, queue=Promise.resolve();
const heldKeys=new Set(),heldButtons=new Set();

function run(args){
  return new Promise(resolve=>execFile('/usr/bin/ydotool', args, {timeout:1500},
    err=>resolve(!err)));
}
function enqueue(args){queue=queue.then(()=>run(args),()=>run(args));return queue;}
function start(){
  if(started) return;
  started=true;
  execFile('/usr/bin/systemctl',['--user','start','ydotool.service'],{timeout:3000},()=>{});
}

async function input(raw){
  const e=raw && typeof raw==='object' ? raw : {};
  const now=Date.now();
  // A remote browser can produce hundreds of pointermove events per second. Besides wasting CPU,
  // that can starve the release packet behind a move backlog and leave a button held down.
  if(e.type==='move' && now-lastAt<16) return false;
  lastAt=now; start();
  if(e.type==='move'){
    const dx=Math.round(Number(e.dx)),dy=Math.round(Number(e.dy));
    if(!Number.isFinite(dx)||!Number.isFinite(dy)||Math.abs(dx)>240||Math.abs(dy)>240) return false;
    return enqueue(['mousemove',String(dx),String(dy)]);
  }
  if(e.type==='button'){
    const b=BUTTONS[e.button]; if(b===undefined || typeof e.down!=='boolean') return false;
    const ok=await enqueue(['click',(e.down?'0x4':'0x8')+b]);
    if(ok){if(e.down)heldButtons.add(b);else heldButtons.delete(b);}return ok;
  }
  if(e.type==='key'){
    const code=Math.round(Number(e.code));
    if(!KEY_CODES.has(code)||typeof e.down!=='boolean') return false;
    const ok=await enqueue(['key',code+':'+(e.down?1:0)]);
    if(ok){if(e.down)heldKeys.add(code);else heldKeys.delete(code);}return ok;
  }
  return false;
}

async function release(){
  const keys=[...heldKeys],buttons=[...heldButtons];heldKeys.clear();heldButtons.clear();
  if(keys.length) await enqueue(['key',...keys.map(code=>code+':0')]);
  if(buttons.length) await enqueue(['click',...buttons.map(b=>'0x8'+b)]);
  return true;
}

module.exports={input,release,KEY_CODES};
