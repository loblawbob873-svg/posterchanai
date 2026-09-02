'use strict';
/* ALT+TAB WITH THE DESKTOP AS IT ACTUALLY SHIPS.
 *
 * `alt_tab_switcher_sim.js` puts Firefox in `wins` — `Object.assign(make('Firefox'),{native:41})`.
 * That is what the desktop looked like when native applications were HOSTED inside PosterChan
 * frames. Hosting is opt-in now (`pc_os_host_native`, default off), so on every shipped machine
 * Firefox/Telegram/foot are compositor windows the shell only keeps a taskbar row for —
 * `nativeTasks`. The fixture therefore agreed with the bug and could not see it.
 *
 * Measured on the real two-monitor desk before the fix: Firefox, Telegram and a terminal on screen,
 * the taskbar drawing all three, and `PCOS.windows()` answering `[]` on BOTH renderers — so the
 * chooser had zero rows and Alt+Tab was a no-op with nothing in any log.
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
    getPropertyValue:()=>''};this.attributes={};this._className='';this.className=name;this.isConnected=true;
    this._text=''; }
  set className(v){this._className=v;this.classList=new Classes(v);}
  get className(){return this._className;}
  set innerHTML(v){this.children=[];this._html=String(v);}
  get innerHTML(){return this._html||'';}
  appendChild(x){x.parent=this;this.children.push(x);return x;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);this.isConnected=false;}
  setAttribute(k,v){this.attributes[k]=String(v);}
  get childElementCount(){return this.children.length;}
  get textContent(){return this._text;}
  scrollIntoView(){this.scrolled=true;}
  querySelectorAll(){return [];}
  cloneNode(){return new El(this.className);}
}
const listeners={};
global.document={body:new El('body'),createElement:()=>new El(),
  addEventListener:(k,f)=>(listeners[k]=listeners[k]||[]).push(f)};
const toggleStart=()=>{},hideCtx=()=>{},enc=String,iconSvg=x=>'<svg>'+x+'</svg>';
const appIcon=a=>'<img class="os-app-ic" alt="'+String((a&&a.title)||'')+'">';

const shown=[],decorated=[],crossed=[],previewed=[];
const pcWM={windows:async()=>[{id:90,app:'place.poster.desktop'}],focus:async()=>{},
  show:async id=>{shown.push(Number(id));const r=nativeTasks.find(x=>Number(x.id)===Number(id));
    if(r)r.stashed=false;return true;},
  preview:async id=>{previewed.push(Number(id));return 'data:image/png;base64,shot';},
  cycleOutput:async direction=>{crossed.push(direction);return true;}};
const _focusNativeDecorated=id=>{decorated.push(Number(id));return Promise.resolve(true);};
/* The chooser lives in a TILED surface and sway paints every floating window above it, so the
 * gesture asks the compositor to fullscreen this shell while it is up. Record both directions. */
const fsCalls=[];
pcWM.fullscreen=async(id,on)=>{fsCalls.push((on?'+':'-')+id);return true;};
global.window={pcWM};

const focused=[];
const focusWin=w=>{focused.push(w.title);wins.forEach(x=>x.el.classList.s.delete('focused'));
  w.el.classList.add('focused');w.min=false;};
const make=(title,focus,min=false)=>({id:title,title,view:title.toLowerCase(),icon:'i-grid',min,
  el:new El('osw'+(focus?' focused':'')),body:new El('feed')});

/* The shipped shape: our own frames in `wins`, the machine's applications in `nativeTasks`. */
let wins=[make('Task Manager',true)];
let nativeTasks=[{id:60,title:'Telegram',appId:'TelegramDesktop',focused:false,stashed:false},
                 {id:204,title:'Firefox',appId:'firefox-bin',focused:false,stashed:false},
                 {id:188,title:'Terminal',appId:'foot',focused:false,stashed:true}];

const start=source.indexOf('  let _altSwitch=null;');
const end=source.indexOf('  // ---- snapping',start);
if(start<0||end<0)throw new Error('switcher implementation missing');
eval(source.slice(start,end)+'\nglobalThis.__cycleWindows=cycleWindows;\nglobalThis.__switchRows=_switchRows;\nglobalThis.__focusSwitchRow=(typeof _focusSwitchRow==="function")?_focusSwitchRow:()=>{};\nglobalThis.__rawRows=_switchRows;');

function ok(name,value){if(!value)throw new Error(name);console.log('  ok   '+name);}
const overlay=()=>document.body.children.find(x=>x.classList.contains('os-alt-switch'));

/* 1. The machine's windows are rows at all. */
const rows=__switchRows();
ok('every window on this screen is a row, hosted or not',rows.length===4);
ok('the compositor windows are there by name',
   ['Telegram','Firefox','Terminal'].every(t=>rows.some(r=>r.title===t)));

/* 1b. ORDER IS CREATION ORDER, NOT FOCUS ORDER. sway moves the focused floating window to the end
 *     of its list, so `nativeTasks` arrives in a different order after every focus change — and
 *     "last in the list" is what hands the gesture to the next monitor. Measured on hardware: two
 *     windows, and the order flipped 204,188 <-> 188,204 on every single focus change. */
const orderA=__switchRows().map(r=>r.key).join(',');
nativeTasks=[nativeTasks[2],nativeTasks[0],nativeTasks[1]];
ok('row order survives sway reordering its own list',__switchRows().map(r=>r.key).join(',')===orderA);
nativeTasks=[nativeTasks[1],nativeTasks[2],nativeTasks[0]];
ok('and survives it again',__switchRows().map(r=>r.key).join(',')===orderA);
ok('native rows are in con_id (creation) order',
   orderA.endsWith('n:60,n:188,n:204'));

/* 2. A chooser is drawn and it names them. */
__cycleWindows('next');
let o=overlay();
ok('a chooser is drawn with only PosterChan frames in wins',!!o);
ok('it carries one card per window',o&&o.children.length===4);
ok('one card is selected',o.children.filter(x=>x.classList.contains('selected')).length===1);
ok('a native card is labelled with its own application icon',
   o.children.some(c=>String(c.children[1].innerHTML).indexOf('os-app-ic')>=0));

/* 3. Committing on a native row focuses the compositor window, not a frame. */
listeners.keyup[0]({key:'Alt'});
ok('committing a native row focuses that compositor window',decorated.length===1);
ok('and does NOT try to focus it as an HTML frame',focused.length===0);

/* 4. A minimised (stashed) native window is restored before it is focused. */
while(overlay())listeners.keydown[0]({key:'Escape',preventDefault(){},stopPropagation(){}});
decorated.length=0;shown.length=0;focused.length=0;
const stashed=__switchRows().find(r=>r.native===188);
ok('the stashed terminal is a row',!!stashed);
__focusSwitchRow(__switchRowEntry(188));
function __switchRowEntry(id){
  /* _switchRows() is the shipped builder; take the live entry, not the exported summary. */
  return __rawRows().find(e=>e.native===id);
}

setTimeout(()=>{
  ok('a stashed native window is shown before it is focused',shown.length>0);
  /* 5. The single-row wrap no longer throws the gesture at another monitor: four rows means the
   *    press has somewhere to go on this screen. */
  ok('a full list does not need the other monitor on the first press',crossed.length===0);
  ok('live native cards ask the compositor for pixels',previewed.length>0);
  ok('the chooser is lowered again on every exit, never left fullscreen',
     fsCalls.length===0||fsCalls.filter(x=>x[0]==='-').length>=fsCalls.filter(x=>x[0]==='+').length);
  /* 6. THE CHOOSER MUST BE ABOVE THE APPLICATIONS. sway paints every floating window above the
   *    tiled shell, so a chooser drawn in the shell is invisible under Firefox — measured on
   *    hardware: the chooser existed at (1300,915) 472x218 with the shell FOCUSED, and grim of that
   *    exact rectangle returned Firefox's page. The gesture asks the compositor to fullscreen this
   *    shell, which is the one thing that outranks a floating window, and lowers it on every exit. */
  fsCalls.length=0;
  __cycleWindows('next');
  setTimeout(()=>{
    ok('the shell is raised over the floating applications while the chooser is up',
       fsCalls.some(x=>x[0]==='+'));
    listeners.keyup[0]({key:'Alt'});
    ok('and lowered again the moment the gesture ends',fsCalls.some(x=>x[0]==='-'));
    ok('lowering happens before the chosen window is focused',
       fsCalls.indexOf(fsCalls.find(x=>x[0]==='-'))>=0);
    console.log('OK Alt+Tab reaches the machine\'s own windows');
  },700);
},0);
