/* THE DESKTOP'S MUSIC SURFACES ARE NOT OUR FLOATING PANEL, so they must not be gated on it.
 *
 * `html.os-on #music-player{display:none}` — the windowed desktop has no floating player at all.
 * Everything the desktop shows about music is the Now-playing WIDGET, and everything Android shows
 * on the lock screen is the media session. Both were updated from a block that sat below
 * `if(!this.el||this.el.classList.contains('hidden')||this.min) return;`, so on the desktop neither
 * ever ran: the widget's bar stood still for the length of the track.
 *
 * Runs the SHIPPED MusicPlayer._tick against a stub player. A source assertion cannot see this —
 * the block is present and correct either way, and only its POSITION decides whether it executes.
 */
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';

const code=fs.readFileSync(process.env.PC_APP_SOURCE||new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const tick=code.slice(code.indexOf('    _tick(){'),code.indexOf('    onChange:null,'));

function run({hidden,min=false,currentTime=7.5,cur='sha1',appMounted=false}){
  const calls={media:0,widget:0,tickApp:0,remember:0,unhid:0};
  const el={_cls:new Set(hidden?['hidden']:[]),
    classList:{contains:c=>el._cls.has(c),add:c=>el._cls.add(c),
               remove:c=>{if(el._cls.delete(c))calls.unhid++;}},
    querySelector:()=>null};
  const ctx={console,Math,Number,
    _audioEl:{currentTime,duration:200,paused:false},
    document:{getElementById:id=>id==='ma-lib'?(appMounted?{}:null):null},
    _fmtTime:()=>'0:00'};
  ctx.window=ctx;
  ctx.PCOS={musicChanged(){calls.widget++;}};
  vm.createContext(ctx);
  vm.runInContext('globalThis.P={\n'+tick+'\n_rememberLast(){calls.remember++;},'
    +'_tickApp(){calls.tickApp++;},_media(){calls.media++;},_render(){},'
    +'el:null,cur:null,min:false,_msSec:-1,_scrub:null};',ctx);
  Object.assign(ctx.P,{el,cur,min});
  ctx.calls=calls;
  vm.runInContext('P._tick();',ctx);
  return {calls,el};
}

/* ---- 1. THE DESKTOP CASE: our panel is hidden, and both surfaces still tick ---- */
{
  // appMounted:true — the Music app is up, which is exactly when the floating panel is hidden.
  const {calls}=run({hidden:true,appMounted:true});
  assert.equal(calls.widget,1,'the desktop Now-playing widget must be told, panel or no panel');
  assert.equal(calls.media,1,'and so must the OS media session — a lock screen is not our panel');
}

/* ---- 2. MINIMISED is the same argument ---- */
{
  const {calls}=run({hidden:false,min:true,appMounted:true});
  assert.equal(calls.widget,1,'minimising our panel does not minimise the desktop widget');
  assert.equal(calls.media,1);
}

/* ---- 3. THE VISIBLE CASE IS UNCHANGED — no double-tick ---- */
{
  const {calls}=run({hidden:false,appMounted:true});
  assert.equal(calls.widget,1,'still exactly once per second, not twice');
  assert.equal(calls.media,1);
}

/* ---- 4. STILL THROTTLED TO ONE A SECOND. The reason the block was where it was: timeupdate
        fires ~4x a second and this must not become a per-frame repaint. ---- */
{
  const calls={media:0,widget:0,tickApp:0,remember:0,unhid:0};
  const el={_cls:new Set(['hidden']),classList:{contains:c=>el._cls.has(c),add:()=>{},remove:()=>{}},
            querySelector:()=>null};
  const ctx={console,Math,Number,_audioEl:{currentTime:7.1,duration:200,paused:false},
    document:{getElementById:()=>({})},_fmtTime:()=>'0:00'};
  ctx.window=ctx;ctx.PCOS={musicChanged(){calls.widget++;}};
  vm.createContext(ctx);
  vm.runInContext('globalThis.P={\n'+tick+'\n_rememberLast(){},_tickApp(){},'
    +'_media(){calls.media++;},_render(){},el:null,cur:null,min:false,_msSec:-1,_scrub:null};',ctx);
  Object.assign(ctx.P,{el,cur:'sha1'});ctx.calls=calls;
  vm.runInContext('P._tick();',ctx);                       // 7.1 -> second 7
  vm.runInContext('_audioEl.currentTime=7.6;P._tick();',ctx);   // same second
  assert.equal(calls.widget,1,'four timeupdates inside one second are still one update');
  vm.runInContext('_audioEl.currentTime=8.2;P._tick();',ctx);   // next second
  assert.equal(calls.widget,2);
}

/* ---- 5. NOTHING PLAYING, NOTHING TICKS ---- */
{
  const {calls}=run({hidden:true,appMounted:true,currentTime:0});
  const again=run({hidden:true,appMounted:true,currentTime:0});
  assert.equal(calls.widget,1,'the first call establishes second 0');
  assert.equal(again.calls.widget,1);
}

console.log('music: the desktop widget and the OS media session tick with our panel hidden');
