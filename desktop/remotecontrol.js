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
const BUTTONS = {0:'0', 1:'2', 2:'1'};
let positionCursor=null;
let lastAt=0, started=false, queue=Promise.resolve();
let pendingCursor=null;
const heldKeys=new Set(),heldButtons=new Set();

function run(args){
  return new Promise(resolve=>execFile('/usr/bin/ydotool', args, {timeout:1500},
    err=>resolve(!err)));
}
function enqueueJob(job){pendingCursor=null;queue=queue.then(job,job);return queue;}
function enqueue(args){return enqueueJob(()=>run(args));}
/* The shell supplies direct compositor positioning in logical layout coordinates. ydotool's
 * "absolute" mode resets to the top-left and emits relative motion, which applies acceleration
 * and cannot represent negative monitor origins. Keep it only for standalone legacy callers. */
function setCursor(x,y){
  if(positionCursor)return Promise.resolve().then(()=>positionCursor(x,y)).catch(()=>false);
  return run(['mousemove','--absolute','-x',String(x),'-y',String(y)]);
}
function enqueueCursor(x,y){
  if(pendingCursor){pendingCursor.x=x;pendingCursor.y=y;return pendingCursor.done;}
  const move={x,y};
  move.done=enqueueJob(async()=>{
    await new Promise(resolve=>setTimeout(resolve,16));
    if(pendingCursor===move)pendingCursor=null;
    return setCursor(move.x,move.y);
  });
  pendingCursor=move;return move.done;
}
function start(){
  if(started) return;
  started=true;
  execFile('/usr/bin/systemctl',['--user','start','ydotool.service'],{timeout:3000},()=>{});
}

async function input(raw){
  const e=raw && typeof raw==='object' ? raw : {};
  const now=Date.now();
  // Coalesce absolute motion without losing the final position. Relative deltas stay bounded.
  if(e.type==='move' && now-lastAt<16) return false;
  lastAt=now; start();
  if(e.type==='move'){
    const dx=Math.round(Number(e.dx)),dy=Math.round(Number(e.dy));
    if(!Number.isFinite(dx)||!Number.isFinite(dy)||Math.abs(dx)>240||Math.abs(dy)>240) return false;
    return enqueue(['mousemove',String(dx),String(dy)]);
  }
  if(e.type==='absolute'){
    const x=Math.round(Number(e.x)),y=Math.round(Number(e.y));
    if(!Number.isFinite(x)||!Number.isFinite(y)||Math.abs(x)>100000||Math.abs(y)>100000) return false;
    return enqueueCursor(x,y);
  }
  if(e.type==='wheel'){
    const dy=Math.round(Number(e.dy));
    if(!Number.isFinite(dy)||!dy||Math.abs(dy)>12) return false;
    return enqueue(['mousemove','--wheel','0',String(dy)]);
  }
  if(e.type==='button'){
    const b=BUTTONS[e.button]; if(b===undefined || typeof e.down!=='boolean') return false;
    /* A button packet carries the point from the same browser event. Pointermove is deliberately
       rate-limited, and IPC/native scheduling can otherwise let the click overtake the final
       absolute move. Put position and button on the same ordered native queue. */
    let x=null,y=null;
    if(e.x!=null||e.y!=null){
      x=Math.round(Number(e.x));y=Math.round(Number(e.y));
      if(!Number.isFinite(x)||!Number.isFinite(y)||Math.abs(x)>100000||Math.abs(y)>100000)return false;
    }
    return enqueueJob(async()=>{
      /* Ignore browser duplicate releases (pointercancel after pointerup) without emitting another
         native click. A duplicate press is equally non-actionable while the button is held. */
      if(e.down?heldButtons.has(b):!heldButtons.has(b))return true;
      if(x!=null&&!await setCursor(x,y))return false;
      const ok=await run(['click',(e.down?'0x4':'0x8')+b]);
      if(ok){if(e.down)heldButtons.add(b);else heldButtons.delete(b);}return ok;
    });
  }
  if(e.type==='key'){
    const code=Math.round(Number(e.code));
    if(!KEY_CODES.has(code)||typeof e.down!=='boolean') return false;
    return enqueueJob(async()=>{
      const ok=await run(['key',code+':'+(e.down?1:0)]);
      if(ok){if(e.down)heldKeys.add(code);else heldKeys.delete(code);}return ok;
    });
  }
  return false;
}

async function release(){
  return enqueueJob(async()=>{
    const keys=[...heldKeys],buttons=[...heldButtons];
    if(keys.length && await run(['key',...keys.map(code=>code+':0')]))heldKeys.clear();
    if(buttons.length && await run(['click',...buttons.map(b=>'0x8'+b)]))heldButtons.clear();
    return !heldKeys.size&&!heldButtons.size;
  });
}

module.exports={input,release,KEY_CODES,setPositioner:fn=>{positionCursor=fn;}};
