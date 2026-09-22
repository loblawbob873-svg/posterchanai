"""A hidden flyout must be SHOWN even when Chromium never says `ready-to-show`.

Measured in the PosterChanOS VM (QEMU bochs-drm, no GPU, Wayland): a `show: false` BrowserWindow
gets `ready-to-show` only if its page paints within ~100ms of the window being created. A data:
page whose <head> busy-waits 30/60ms got it at 49/83ms; 120ms and 250ms never did, and neither did
the real client page (~130ms to first paint). main.js showed and placed the Start menu ONLY from
that event, so pressing Super loaded a fully drawn menu into a window that was never mapped —
"in VM, start menu not working", while the same build worked on a laptop that paints faster.

The tests below drive the SHIPPED helper and the SHIPPED openPopupWindow with a window that never
emits `ready-to-show`, which is exactly the VM's behaviour.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "desktop/show-when-ready.js"
MAIN = (ROOT / "desktop/main.js").read_text()


def _node(script):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr + result.stdout


FAKES = r'''
const assert=require('node:assert/strict');
const {showWhenReady}=require(%r);
class Emitter{constructor(){this.h={};}
 on(n,f){(this.h[n]=this.h[n]||[]).push(f);} once(n,f){const w=(...a)=>{this.off(n,w);f(...a)};this.on(n,w);}
 off(n,f){this.h[n]=(this.h[n]||[]).filter(x=>x!==f);} emit(n,...a){for(const f of [...(this.h[n]||[])])f(...a);}}
class Win extends Emitter{constructor(){super();this.dead=false;this.webContents=new Emitter();}
 isDestroyed(){return this.dead}}
let now=0;const timers=[];
const setT=(f,ms)=>{const t={f,at:now+ms,live:true};timers.push(t);return t};
const clearT=t=>{if(t)t.live=false};
const advance=ms=>{now+=ms;for(const t of timers.slice())if(t.live&&t.at<=now){t.live=false;t.f();}};
const opts=extra=>Object.assign({setTimeout:setT,clearTimeout:clearT,graceMs:150,ceilingMs:1000},extra||{});
''' % str(HELPER)


def test_helper_shows_once_whichever_signal_comes_first():
    _node(FAKES + r'''
// 1. the fast path: ready-to-show, and nothing afterwards shows it again
{const w=new Win();const seen=[];showWhenReady(w,why=>seen.push(why),opts());
 w.emit('ready-to-show');w.webContents.emit('did-finish-load');advance(5000);w.emit('ready-to-show');
 assert.deepEqual(seen,['ready-to-show']);}
// 2. THE VM: the page loads and never paints -> shown after the grace, not never
{const w=new Win();const seen=[];showWhenReady(w,why=>seen.push(why),opts());
 w.webContents.emit('did-finish-load');advance(149);assert.deepEqual(seen,[]);
 advance(1);assert.deepEqual(seen,['loaded without a first paint']);
 w.emit('ready-to-show');advance(5000);assert.equal(seen.length,1,'shown twice');}
// 3. a load that is itself slow -> the ceiling
{const w=new Win();const seen=[];showWhenReady(w,why=>seen.push(why),opts());
 advance(999);assert.deepEqual(seen,[]);advance(1);assert.equal(seen.length,1);}
// 4. a destroyed window is never touched (a listener on a dead window throws in Electron)
{const w=new Win();let n=0;showWhenReady(w,()=>n++,opts());w.dead=true;w.emit('closed');
 w.emit('ready-to-show');w.webContents.emit('did-finish-load');advance(5000);assert.equal(n,0);}
// 5. a window the caller already replaced is declined
{const w=new Win();let n=0;showWhenReady(w,()=>n++,opts({stillWanted:()=>false}));
 w.webContents.emit('did-finish-load');advance(5000);assert.equal(n,0);}
// 6. a throwing show() does not escape into the open path
{const w=new Win();showWhenReady(w,()=>{throw Error('boom')},opts());w.emit('ready-to-show');}
''')


def test_the_start_menu_is_shown_and_placed_when_ready_to_show_never_fires():
    opener = MAIN[MAIN.index("async function openPopupWindow("):MAIN.index("/* WAYLAND GIVES A CLIENT NO SAY")]
    _node(FAKES.replace("let now=0", "let now_unused=0") + r'''
const APP_URL='app://posterchan/index.html',POPUP_TITLE='PosterChan Popup',STICKY_POPUPS=new Set(['compose']);
const _shellScopes=new Map(),path=require('node:path'),__dirname='.';
let _popupWin=null,_popupKind='';const created=[],placed=[];
const wm=()=>({outputs:async()=>[],focusedOutputName:async()=>''});
const placePopupWindow=(p,want)=>placed.push([p,want]);const forwardShellTick=()=>{};
const closePopupWindow=()=>{const p=_popupWin;_popupWin=null;_popupKind='';if(p&&!p.dead){p.dead=true;p.emit('closed');}};
class BrowserWindow extends Win{
 constructor(o){super();this.o=o;this.shown=0;created.push(this);}
 getBounds(){return {width:this.o.width,height:this.o.height}}
 show(){assert.equal(this.dead,false,'showed a destroyed window');this.shown++;}
 // Exactly what the VM does: the document loads, and ready-to-show never comes.
 async loadURL(u){this.url=u;this.webContents.emit('did-finish-load');}
}
''' + opener + r'''
(async()=>{
 assert.equal(await openPopupWindow({sender:{id:1}},'start',{x:10,y:200,width:420,height:560}),true);
 await new Promise(r=>setTimeout(r,400));
 assert.equal(created[0].shown,1,'the Start menu window was never shown');
 assert.equal(placed.length,1,'the Start menu was never placed');
 // A replaced popup is not shown behind the new one.
 openPopupWindow({sender:{id:1}},'noti',{});
 await openPopupWindow({sender:{id:1}},'tray',{});
 await new Promise(r=>setTimeout(r,400));
 assert.equal(created[1].shown,0,'a superseded popup was shown');
 assert.equal(created[2].shown,1);
 process.exit(0);
})().catch(e=>{console.error(e);process.exit(1)});
''')


def test_no_hidden_window_waits_on_ready_to_show_alone():
    """The screen-share picker had the same shape: `show: false` + show only on ready-to-show, which
    on the VM would leave Share waiting on a picker nobody can see."""
    assert "pick.once('ready-to-show'" not in MAIN
    assert "showWhenReady(pick," in MAIN
    popup = MAIN[MAIN.index("async function openPopupWindow("):MAIN.index("/* WAYLAND GIVES A CLIENT NO SAY")]
    assert "p.once('ready-to-show'" not in popup
    assert "showWhenReady(p," in popup
    assert "require('./show-when-ready')" in MAIN
