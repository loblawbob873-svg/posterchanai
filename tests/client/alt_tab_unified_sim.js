'use strict';
/* ONE Alt+Tab LIST ACROSS EVERY MONITOR ("the app list should be unified across multiple monitors").
 * Two renderers run the SHIPPED switcher from os.js, joined by the SHIPPED gatherSwitchRows from
 * desktop/main.js — the same wiring the real desktop has, minus Electron. */
const fs=require('fs'),path=require('path'),vm=require('vm');
const source=fs.readFileSync(path.resolve(__dirname,'../../static/js/client/os.js'),'utf8');
const mainSrc=fs.readFileSync(path.resolve(__dirname,'../../desktop/main.js'),'utf8');
const begin=source.indexOf('  let _altSwitch=null;'),end=source.indexOf('  // ---- snapping',begin);
const focusBegin=source.indexOf('  let _focusGeneration = 0;'),focusEnd=source.indexOf('  const _domCoveredNative',focusBegin);
if(begin<0||end<0||focusBegin<0||focusEnd<0)throw new Error('switcher implementation moved');
const code=source.slice(focusBegin,focusEnd)+source.slice(begin,end)+
  '\nglobalThis.cycleWindows=cycleWindows;globalThis.__rows=()=>_switchRows().map(e=>({key:e.key,title:e.title,native:e.native,icon:e.win?e.win.icon:""}));'+
  'globalThis.__focusKey=k=>{const e=_switchRows().find(x=>x.key===k);return e?_focusSwitchRow(e):false;};';
const g=mainSrc.match(/async function gatherSwitchRows\([\s\S]*?\n}\n/);
if(!g)throw new Error('gatherSwitchRows is gone from desktop/main.js');
const gatherSwitchRows=vm.runInNewContext('('+g[0].trim()+')',{Promise,setTimeout,Array,String});
class Classes{constructor(s=''){this.s=new Set(s.split(/\s+/).filter(Boolean));}add(...x){x.forEach(v=>this.s.add(v));}remove(...x){x.forEach(v=>this.s.delete(v));}contains(x){return this.s.has(x);}}
class El{constructor(s=''){this.children=[];this.parent=null;this.style={backgroundImage:'',getPropertyValue:()=>''};this.className=s;this.isConnected=true;this._html='';}
  set className(v){this._className=v;this.classList=new Classes(v);}get className(){return this._className;}
  set innerHTML(v){this.children=[];this._html=String(v);}get innerHTML(){return this._html;}
  appendChild(x){x.parent=this;this.children.push(x);return x;}remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);this.isConnected=false;}
  setAttribute(){}get childElementCount(){return this.children.length;}get textContent(){return '';}scrollIntoView(){}querySelectorAll(){return [];}cloneNode(){return new El(this.className);}}
const outputs=[];let handoffs=0;
function makeOutput(name,x,specs,unified=true){
  const listeners={},document={body:new El('body'),createElement:()=>new El(),addEventListener:(k,f)=>(listeners[k]=listeners[k]||[]).push(f)};
  const wins=specs.map((s,i)=>{const el=new El('osw'+(s.focused?' focused':''));return {id:name+i,title:s.title,view:s.title.toLowerCase(),icon:'i-grid',native:null,min:false,closing:false,el,body:new El('feed')};});
  const focused=[];
  const pcWM={windows:async()=>[],focus:async()=>true,cycleOutput:async()=>{handoffs++;return false;}};
  if(unified){
    pcWM.switchRowsElsewhere=()=>gatherSwitchRows(outputs.slice().sort((a,b)=>a.x-b.x).map(o=>({assignment:{output:o.name},o})),name,rec=>rec.o.c.__rows());
    pcWM.focusElsewhere=async(o,k)=>{const t=outputs.find(x=>x.name===o);return t?t.c.__focusKey(k):false;};
  }
  const c={document,wins,nativeTasks:[],console,setTimeout,clearTimeout,Promise,toggleStart(){},hideCtx(){},enc:String,iconSvg:x=>'<svg>'+x+'</svg>',
    appIcon:()=>'<img>',_focusNativeDecorated:()=>Promise.resolve(true),window:{},pcWM,
    focusWin(w){wins.forEach(v=>v.el.classList.remove('focused'));w.el.classList.add('focused');focused.push(w.title);}};
  c.window.pcWM=pcWM;vm.createContext(c);vm.runInContext(code,c);
  const out={name,x,c,wins,focused,listeners,document};outputs.push(out);return out;
}
const chooser=o=>o.document.body.children.find(x=>x.classList.contains('os-alt-switch'));
const press=(o,d='next')=>o.c.cycleWindows(d),release=o=>(o.listeners.keyup||[]).forEach(f=>f({key:'Alt'}));
const tick=()=>new Promise(r=>setTimeout(r,5));
function ok(n,v){if(!v)throw new Error(n);console.log('  ok   '+n);}
(async()=>{
  const left=makeOutput('DP-1',0,[{title:'Terminal',focused:true},{title:'Messages'}]);
  const right=makeOutput('DP-2',1920,[{title:'Firefox',focused:true},{title:'Telegram'}]);
  press(left);ok('the chooser opens at once with this monitor\'s windows',chooser(left)&&chooser(left).children.length===2);
  await tick();
  const cards=chooser(left).children;
  ok('every monitor\'s windows are in ONE list',cards.length===4);
  ok('windows from the other monitor say which monitor',cards.slice(2).every(c=>c.children[1]._html.includes('DP-2'))&&!cards[0].children[1]._html.includes('DP-'));
  ok('the first press still selects the next window',cards[1].className.includes('selected'));
  press(left);press(left);
  ok('the selection walks onto the other monitor\'s windows',chooser(left).children[3].className.includes('selected'));
  ok('no chooser appears on the other monitor',!chooser(right));
  release(left);await tick();
  ok('committing focuses the window on the monitor that owns it',right.focused.at(-1)==='Telegram');
  ok('the gesture was never handed across',handoffs===0);
  // A press that wraps before the other monitor answered still lands in the full list.
  press(left,'previous');await tick();
  const sel=chooser(left).children.findIndex(c=>c.className.includes('selected'));
  ok('previous from the first window goes to the LAST window on the desk',sel===3);
  release(left);await tick();
  // An older desktop build (no bridge) keeps the hand-off.
  outputs.length=0;handoffs=0;
  const a=makeOutput('DP-1',0,[{title:'Terminal',focused:true},{title:'Messages'}],false);makeOutput('DP-2',1920,[{title:'Firefox'}],false);
  press(a);press(a);await tick();
  ok('without the bridge the old hand-off still runs',handoffs===1);
  console.log('OK unified Alt+Tab across monitors');
})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
