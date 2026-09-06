'use strict';
/* Two renderer simulation over the installed os.js/CSS. It never touches the live compositor. */
const fs=require('fs'),path=require('path'),vm=require('vm');
const source=fs.readFileSync(process.env.PC_INSTALLED_OS_JS||path.resolve(__dirname,'../../static/js/client/os.js'),'utf8');
const css=fs.readFileSync(process.env.PC_INSTALLED_CLIENT_CSS||path.resolve(__dirname,'../../static/css/client.css'),'utf8');
const begin=source.indexOf('  let _altSwitch=null;'),end=source.indexOf('  // ---- snapping',begin);
if(begin<0||end<0)throw new Error('installed switcher implementation missing');
const focusBegin=source.indexOf('  let _focusGeneration = 0;'),focusEnd=source.indexOf('  const _domCoveredNative',focusBegin);
if(focusBegin<0||focusEnd<0)throw new Error('installed focus generation implementation missing');
const code=source.slice(focusBegin,focusEnd)+source.slice(begin,end)+'\nglobalThis.cycleWindows=cycleWindows;';
class Classes{constructor(s=''){this.s=new Set(s.split(/\s+/).filter(Boolean));}add(...x){x.forEach(v=>this.s.add(v));}remove(...x){x.forEach(v=>this.s.delete(v));}contains(x){return this.s.has(x);}}
class El{constructor(s=''){this.children=[];this.parent=null;this.style={backgroundImage:'',getPropertyValue:k=>k==='--native-stash-preview'?(this.preview||''):''};this.className=s;this.isConnected=true;}set className(v){this._className=v;this.classList=new Classes(v);}get className(){return this._className;}set innerHTML(v){this.children=[];}appendChild(x){x.parent=this;this.children.push(x);return x;}remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);this.isConnected=false;}setAttribute(){}get childElementCount(){return this.children.length;}get textContent(){return '';}scrollIntoView(){}querySelectorAll(){return [];}cloneNode(){return new El(this.className);}}
const outputs=[];
function makeOutput(name,specs){
  const listeners={},document={body:new El('body'),createElement:()=>new El(),addEventListener:(k,f)=>(listeners[k]=listeners[k]||[]).push(f)};
  const wins=specs.map((s,i)=>{const el=new El('osw'+(s.focused?' focused':'')+(s.native?' osw-native':''));if(s.native)el.preview='url(preview-'+i+')';return {title:s.title,view:s.title.toLowerCase(),icon:'i-grid',native:s.native?100+i:null,min:false,closing:false,el,body:new El('feed')};});
  const focused=[],restored=[];
  /* Hosted fixture: these native windows are adopted frames in `wins`, so `nativeTasks` is empty.
   * The un-hosted default lives in alt_tab_native_taskbar_sim.js. */
  const c={document,wins,nativeTasks:[],console,setTimeout,clearTimeout,Promise,toggleStart(){},hideCtx(){},enc:String,iconSvg:x=>x,
    appIcon:()=>'<img class="os-app-ic" alt="">',_focusNativeDecorated:()=>Promise.resolve(true),
    window:{},
    focusWin(w){wins.forEach(x=>x.el.classList.remove('focused'));w.el.classList.add('focused');w.min=false;focused.push(w.title);if(w.native)restored.push(w.title);},
    pcWM:{windows:async()=>[{id:name==='left'?90:91,app:'posterchan-desktop'}],focus:async()=>true,cycleOutput:async d=>handoff(name,d)}};
  c.window.pcWM=c.pcWM;vm.createContext(c);vm.runInContext(code,c);const out={name,c,wins,focused,restored,listeners,document};outputs.push(out);return out;
}
const chooser=o=>o.document.body.children.find(x=>x.classList.contains('os-alt-switch'));
function handoff(from,d){const at=outputs.findIndex(x=>x.name===from),step=d==='previous'?-1:1;outputs[(at+step+outputs.length)%outputs.length].c.cycleWindows(d,true);return true;}
const press=(o,d)=>o.c.cycleWindows(d),release=o=>(o.listeners.keyup||[]).forEach(f=>f({key:'Alt'}));
const tick=()=>new Promise(r=>setTimeout(r,0));
function ok(n,v){if(!v)throw new Error(n);console.log('  ok   '+n);}
(async()=>{
  const left=makeOutput('left',[{title:'Terminal',focused:true},{title:'Messages'}]);
  const right=makeOutput('right',[{title:'Firefox',native:true},{title:'Telegram',native:true}]);
  press(left,'next');ok('local internal windows cycle',chooser(left).children.length===2);release(left);ok('local focus commits',left.focused.at(-1)==='Messages');
  press(left,'next');await tick();ok('boundary hands to adjacent renderer',!chooser(left)&&chooser(right)&&outputs.filter(chooser).length===1);
  release(right);ok('Firefox restores and focuses',right.focused.at(-1)==='Firefox'&&right.restored.at(-1)==='Firefox');
  press(right,'next');release(right);ok('Telegram-like frame is reachable',right.focused.at(-1)==='Telegram'&&right.restored.at(-1)==='Telegram');
  press(right,'previous');release(right);ok('reverse local cycle works',right.focused.at(-1)==='Firefox');
  press(right,'previous');await tick();ok('reverse boundary returns to prior renderer',chooser(left)&&!chooser(right));release(left);
  const rule=css.slice(css.indexOf('.os-tasks{'),css.indexOf('.os-tasks::-webkit-scrollbar'));
  ok('installed taskbar stays left-aligned',rule.includes('justify-content:flex-start')&&!rule.includes('translateX(-50%)'));
  console.log('OK installed cross-output Alt+Tab contract holds');
})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
