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
  remove(...xs) { xs.forEach(x => this.s.delete(x)); }
  toggle(x, on) { if(on===undefined) return this.s.has(x)?this.s.delete(x):this.s.add(x); return on?this.s.add(x):this.s.delete(x); }
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
let _focusGeneration=0;const _claimFocus=()=>++_focusGeneration;
const _focusCompositorCurrent=(id,t)=>t===_focusGeneration?pcWM.focus(id):Promise.resolve(false);
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
eval(source.slice(start,end)+'\nglobalThis.__cycleWindows=cycleWindows;\nglobalThis.__switchRows=_switchRows;\nglobalThis.__focusSwitchRow=(typeof _focusSwitchRow==="function")?_focusSwitchRow:()=>{};\nglobalThis.__rawRows=_switchRows;\nglobalThis.__previewCache=(typeof _nativePreviewCache!==\"undefined\")?_nativePreviewCache:null;');

function ok(name,value){if(!value)throw new Error(name);console.log('  ok   '+name);}

/* ALT+TAB, MEASURED AS A RATE — because "sometimes" is what was reported.
 *
 * "alt+tab is severely broke, not sure how it passed the test suite: some times barely shows the
 * window list, previews sometimes missing."
 *
 * It passed because every Alt+Tab test opens the chooser ONCE, under ideal conditions, and asserts
 * it opened. Neither of the two reported faults is visible that way:
 *
 *   THE LIST. A native row's title arrives SEPARATELY from the window (Firefox and XWayland
 *   announce themselves before their class/title). A chooser opened during that gap used to drop
 *   the row entirely, so the list was short purely because a window was young — intermittently, and
 *   never on the second try.
 *
 *   THE PREVIEWS. A preview is a compositor SCREENSHOT, and `apply` refuses to write into a chooser
 *   that has since closed. Caching it on the switch session meant every press re-captured
 *   everything, so whichever capture lost the race left a blank card. The more windows open, the
 *   likelier.
 *
 * So this drives the shipped switcher SIXTY times with the conditions jittered exactly where the
 * real system is nondeterministic — when a title lands, and when a capture resolves — and asserts
 * the two invariants on EVERY press, not on a lucky one.
 */
const TRIALS = 60;
let previewCalls = 0;
pcWM.preview = async id => { previewCalls++;
  // A capture that sometimes resolves after the gesture is over, which is the real race.
  await new Promise(r => setTimeout(r, id % 3 === 0 ? 0 : 5));
  return 'data:image/png;base64,shot' + id; };

const WINDOWS = [
  {id:60,  appId:'TelegramDesktop'},
  {id:188, appId:'foot'},
  {id:204, appId:'firefox-bin'},
  {id:301, appId:'org.keepassxc.KeePassXC'},
];

let listedShort = 0, blankAfterWarm = 0, namelessRows = 0;

(async () => {
  for (let trial = 0; trial < TRIALS; trial++) {
    // A window is "young" — title not yet delivered — with a probability that varies per trial, so
    // every combination of arrived/not-arrived is exercised rather than one fixed shape.
    nativeTasks = WINDOWS.map((w, i) => ({
      id: w.id, appId: w.appId,
      title: ((trial + i) % 3 === 0) ? '' : w.appId.replace(/[-.].*$/, ''),
      focused: false, stashed: false,
    }));

    /* Count the NATIVE rows only. The reused fixture also carries in-page frames in `wins`, and
       conflating the two made this assert against a number that was never the window count. */
    const rows = __switchRows();
    const nat = rows.filter(r => r.native != null && WINDOWS.some(w => w.id === r.native));
    if (nat.length !== WINDOWS.length) listedShort++;
    if (nat.some(r => !String(r.title || '').trim())) namelessRows++;

    __cycleWindows('next');
    await new Promise(r => setTimeout(r, 12));   // the gesture is SHORT — shorter than a capture
    if (typeof _altCommit === 'function') { try { _altCommit(); } catch (_) {} }
    await new Promise(r => setTimeout(r, 12));

    // From the second press on, the cache is warm and every native card must carry a picture.
    if (trial > 0) {
      const missing = WINDOWS.filter(w => !(globalThis.__previewCache && globalThis.__previewCache.has(w.id)));
      if (missing.length) blankAfterWarm++;
    }
  }

  ok('every window is listed on every one of ' + TRIALS + ' presses, young or not',
     listedShort === 0);
  ok('no row is ever drawn nameless', namelessRows === 0);
  ok('previews survive the gesture, so a warm press is never blank', blankAfterWarm === 0);
  ok('and captures are not repeated once cached (' + previewCalls + ' for ' +
     WINDOWS.length + ' windows over ' + TRIALS + ' presses)',
     previewCalls <= WINDOWS.length * 3);
  console.log('alt+tab rate: ' + TRIALS + ' presses, list short ' + listedShort +
              ', blank-after-warm ' + blankAfterWarm);
})();
