'use strict';
/* THE BRANCH EVERY ONE-SCREEN MACHINE TAKES, WHICH NO TEST HAD EVER RUN.
 *
 * alt_tab_switcher_sim.js gives the renderer a `cycleOutput` that always resolves TRUE — a desk with
 * a second monitor. The main process answers FALSE for a single output (`_shellSurfaces.size < 2`),
 * and on that answer the old code had already removed the chooser and then rebuilt it from scratch
 * at index 0. So on a one-screen desktop the switcher vanished on every wrap and the selection went
 * back to the start instead of moving on by one — "alt tab is complete garbage and disappears each
 * time you switch to a new window" — while every assertion in the multi-monitor sim stayed green.
 *
 * This file is that sim with one value changed, and it asserts the two things a person sees: the
 * chooser STAYS on screen, and the highlight moves by exactly one and wraps.
 */
const fs = require('fs');
const path = require('path');
const source = fs.readFileSync(process.env.PC_INSTALLED_OS_JS ||
  path.resolve(__dirname, '../../static/js/client/os.js'), 'utf8');

class Classes {
  constructor(names='') { this.s = new Set(names.split(/\s+/).filter(Boolean)); }
  add(...xs) { xs.forEach(x => this.s.add(x)); }
  contains(x) { return this.s.has(x); }
}
class El {
  constructor(name='') { this.children=[];this.parent=null;this.style={backgroundImage:'',
    getPropertyValue:()=>''};this.attributes={};this._className='';this.className=name;this.isConnected=true; }
  set className(v){this._className=v;this.classList=new Classes(v);}
  get className(){return this._className;}
  set innerHTML(v){this.children=[];this._html=String(v);}
  appendChild(x){x.parent=this;this.children.push(x);return x;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);this.isConnected=false;}
  setAttribute(k,v){this.attributes[k]=String(v);}
  get childElementCount(){return this.children.length;}
  get textContent(){return '';}
  scrollIntoView(){this.scrolled=true;}
  querySelectorAll(){return [];}
  cloneNode(){return new El(this.className);}
}
const listeners = {};
global.document={body:new El('body'),createElement:()=>new El(),
  addEventListener:(k,f)=>(listeners[k]=listeners[k]||[]).push(f)};
const toggleStart=()=>{},hideCtx=()=>{},enc=String,iconSvg=x=>'<svg>'+x+'</svg>';
/* This fixture is the HOSTED desktop (`pc_os_host_native` on): native windows live in
 * `wins` as adopted frames, so nothing is in `nativeTasks`. The un-hosted default — which
 * is what every shipped machine runs — is `alt_tab_native_taskbar_sim.js`. */
let nativeTasks=[];
const appIcon=a=>'<img class="os-app-ic" alt="">';
const _focusNativeDecorated=id=>Promise.resolve(true);
global.window={};
const asked=[];
/* THE ONE CHANGED VALUE: this desk has one screen, so the compositor refuses the handoff. */
const pcWM={windows:async()=>[{id:90,app:'posterchan-desktop'}],focus:async()=>{},
  preview:async()=>'data:image/png;base64,x',
  cycleOutput:async direction=>{asked.push(direction);return false;}};
const focused=[];
const focusWin=w=>{focused.push(w.title);wins.forEach(x=>x.el.classList.s.delete('focused'));
  w.el.classList.add('focused');w.min=false;};
const make=(title,focus)=>({title,view:title.toLowerCase(),icon:'i-grid',min:false,
  el:new El('osw'+(focus?' focused':'')),body:new El('feed')});
const wins=[make('Terminal',true),make('Files',false),make('Social',false)];
const start=source.indexOf('  let _altSwitch=null;');
const end=source.indexOf('  // ---- snapping',start);
if(start<0||end<0)throw new Error('switcher implementation missing');
eval(source.slice(start,end)+'\nglobalThis.__cycleWindows=cycleWindows;');

const overlay=()=>document.body.children.find(x=>x.classList.contains('os-alt-switch'));
const selected=()=>{const o=overlay();if(!o)return null;
  const i=o.children.findIndex(c=>c.classList.contains('selected'));return i;};
function ok(name,value){if(!value)throw new Error(name);console.log('  ok   '+name);}

/* Focus starts on Terminal (index 0). Three presses must walk 1, 2, then WRAP to 0 — with the
 * chooser on screen the whole time. */
__cycleWindows('next');
ok('the chooser opens', !!overlay());
ok('first press highlights the next window', selected()===1);

__cycleWindows('next');
ok('the chooser is still on screen after a second press', !!overlay());
ok('second press moves on by one', selected()===2);

const before=overlay();
__cycleWindows('next');
ok('WRAPPING DOES NOT REMOVE THE CHOOSER', !!overlay());
ok('the wrap reuses the same chooser rather than rebuilding it', overlay()===before);
ok('the wrap lands on the first window, not on a rebuilt gesture', selected()===0);

__cycleWindows('next');
ok('the chooser keeps working after a wrap', selected()===1 && !!overlay());

/* It may ask the compositor for a handoff, but only once, and a refusal must change nothing. */
ok('the compositor was asked at most once for this gesture', asked.length<=1);

listeners.keyup[0]({key:'Alt'});
ok('Alt release commits the highlighted window', focused.at(-1)==='Files');
ok('committing removes the chooser', !overlay());

/* And the first press of a fresh gesture, made while the LAST window has focus, must draw. That is
 * the shape that showed nothing at all: `atEnd` was true, so the old code returned before drawing. */
focusWin(wins[2]);
__cycleWindows('next');
ok('a gesture started from the last window still draws a chooser', !!overlay());
ok('and it wraps to the first window', selected()===0);

setTimeout(()=>{
  ok('a refused handoff never removes the chooser', !!overlay());
  console.log('OK Alt+Tab holds on a single monitor');
},0);
