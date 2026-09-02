'use strict';
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
const listeners={};
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
const crossed=[];
const previewed=[];
const pcWM={windows:async()=>[{id:90,app:'posterchan-desktop'}],focus:async()=>{},
  preview:async id=>{previewed.push(id);return 'data:image/png;base64,firefox';},
  cycleOutput:async direction=>{crossed.push(direction);return true;}};
const focused=[];
const focusWin=w=>{focused.push(w.title);wins.forEach(x=>x.el.classList.s.delete('focused'));
  w.el.classList.add('focused');w.min=false;};
const make=(title,focus,min=false)=>({title,view:title.toLowerCase(),icon:'i-grid',min,
  el:new El('osw'+(focus?' focused':'')),body:new El('feed')});
const wins=[make('Terminal',true),make('Web Search',false,true),make('Social',false),
  Object.assign(make('Firefox',false),{native:41,view:'native:41'})];
const start=source.indexOf('  let _altSwitch=null;');
const end=source.indexOf('  // ---- snapping',start);
if(start<0||end<0)throw new Error('switcher implementation missing');
eval(source.slice(start,end)+'\nglobalThis.__cycleWindows=cycleWindows;');
function ok(name,value){if(!value)throw new Error(name);console.log('  ok   '+name);}

__cycleWindows('next');
let overlay=document.body.children.find(x=>x.classList.contains('os-alt-switch'));
ok('centered switcher is drawn',!!overlay);
ok('all titled windows including minimised/native are represented',overlay.children.length===4);
ok('one card is visibly selected',overlay.children.filter(x=>x.classList.contains('selected')).length===1);
ok('every card has preview and title',overlay.children.every(x=>x.children.length===2));
listeners.keydown[0]({key:'Escape',preventDefault(){},stopPropagation(){}});
ok('Escape restores the initial window',focused.at(-1)==='Terminal'&&!document.body.children.includes(overlay));

__cycleWindows('next');__cycleWindows('next');
overlay=document.body.children.find(x=>x.classList.contains('os-alt-switch'));
listeners.keyup[0]({key:'Alt'});
ok('Alt release commits the highlighted window',focused.at(-1)==='Social');
ok('commit removes the chooser',!document.body.children.includes(overlay));
__cycleWindows('next');
overlay=document.body.children.find(x=>x.classList.contains('os-alt-switch'));
listeners.keyup[0]({key:'Alt'});
ok('native Firefox participates in the visual switcher',focused.at(-1)==='Firefox');
__cycleWindows('next');
setTimeout(()=>{
  ok('live native card requests compositor pixels',previewed.includes(41));
  ok('end of one monitor hands Alt+Tab to the adjacent monitor',crossed.join(',')==='next');
  ok('local chooser does not wrap over the cross-monitor handoff',!document.body.children.some(x=>x.classList.contains('os-alt-switch')));
  console.log('OK Alt+Tab switcher holds');
},0);
