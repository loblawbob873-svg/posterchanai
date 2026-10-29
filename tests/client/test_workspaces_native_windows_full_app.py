"""WORKSPACES ON POSTERCHANOS: the app windows there are compositor windows, and they belong to workspaces too.

"we need to make sure that our System Settings for startup apps supports the workspace feature too" -- and before
that could mean anything, workspaces had to cover the windows PosterChanOS actually shows. Every PosterChan app there
opens as a real compositor window and every installed program (Firefox, Telegram) is one; they live in
`nativeTasks`, never in the in-page `wins` the workspaces loop walked. So switching workspace on PosterChanOS hid
nothing and only emptied the taskbar.

The shipped bundle as the PosterChanOS desktop, with the compositor stood in by a stateful fake (minimise really
minimises, events really fire). Each case here fails on the code before the fix.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


WM = r'''
window.__wmlog=[];window.__offws=[];
window.__rows=[
  {id:7,app:'place.poster.desktop',title:'PosterChan Desktop',workspace:'1',rect:{x:0,y:0,width:innerWidth,height:innerHeight},focused:false,stashed:false},
  {id:30,app:'firefox',title:'Mozilla Firefox',workspace:'1',rect:{x:100,y:60,width:600,height:500},focused:true,stashed:false},
  {id:31,app:'place.poster.desktop',title:'PosterChan Window — notes',workspace:'1',rect:{x:140,y:60,width:600,height:500},focused:false,stashed:false}];
const __emit=()=>{ (window.__subs||[]).forEach(cb=>{ try{ cb({name:'window',payload:{change:'focus'}}); }catch(_){} }); };
const __set=(id,k,v)=>{ const r=__rows.find(x=>x.id===Number(id)); if(r){ r[k]=v; } setTimeout(__emit,30); return true; };
window.__focusRow=(id)=>{ __rows.forEach(r=>r.focused=(r.id===id)); const r=__rows.find(x=>x.id===id); if(r) r.stashed=false; setTimeout(__emit,30); };
window.__map=(id,title)=>{ __rows.push({id,app:'place.poster.desktop',title,workspace:'1',rect:{x:200,y:80,width:600,height:500},focused:false,stashed:false}); setTimeout(__emit,30); };
window.pcWM={windows:async()=>__rows.map(r=>({...r})),launch:async()=>({pid:1}),
  snapshot:async()=>({windows:__rows.map(r=>({...r})),allIds:__rows.map(r=>r.id),shellId:7}),
  onEvent:(cb)=>{(window.__subs=window.__subs||[]).push(cb);return()=>{}},focus:async(id)=>{__wmlog.push('focus:'+id);return true},
  shellFront:async()=>true,hasAppWindow:()=>false,decorate:async()=>true,
  hide:async(id)=>{__wmlog.push('hide:'+id);return __set(id,'stashed',true)},
  show:async(id)=>{__wmlog.push('show:'+id);return __set(id,'stashed',false)},
  offWorkspace:async(ids)=>{__offws.push(ids.slice().sort());return true}};
// System Settings -> Startup apps is drawn only where the autostart bridge exists (PosterChanOS).
window.pcAutostart={list:async()=>[],add:async()=>'',addApp:async()=>'',set:async()=>({}),remove:async()=>({}),run:async()=>({ok:true})};
'''

TASKS = "[...document.querySelectorAll('#os-bar .os-task[data-kind=\"native\"]')].map(t=>t.dataset.id).sort()"
STASHED = "__rows.filter(r=>r.stashed).map(r=>r.id).sort()"


async def _ready(b):
    await desktop.login(b)
    await b.until("!!document.querySelector('#os-bar') && window.PCOSShell && PCOSShell.available()===true")
    await b.until(TASKS + ".length===2")


@CHROME
def test_switching_workspace_puts_away_the_compositor_windows_and_brings_back_only_its_own():
    got = {}

    async def check(b):
        await _ready(b)
        await b.js("try{localStorage.removeItem('pc_osWorkspaces')}catch(_){};PCOS.addWorkspace();true")
        await b.until("PCOS.workspaces().current===1")
        await b.until(STASHED + ".length===2")
        await asyncio.sleep(.4)
        got['ws2_tasks'] = await b.js(TASKS)
        got['ws2_stashed'] = await b.js(STASHED)
        got['offws'] = await b.js("__offws.slice(-1)[0]")
        await b.js("PCOS.switchWorkspace(0);true")
        await b.until(STASHED + ".length===0")
        await asyncio.sleep(.4)
        got['ws1_tasks'] = await b.js(TASKS)
        got['offws_back'] = await b.js("__offws.slice(-1)[0]")
        # Somebody minimises Firefox themselves; a trip to workspace 2 and back must not un-minimise it.
        await b.js("pcWM.hide(30);true")
        await asyncio.sleep(.4)
        await b.js("__wmlog.length=0;PCOS.switchWorkspace(1);true")
        await b.until(STASHED + ".length===2")
        await asyncio.sleep(.3)
        await b.js("PCOS.switchWorkspace(0);true")
        await b.until(STASHED + "+''==='30'")
        await asyncio.sleep(.4)
        got['self_minimised'] = await b.js(STASHED)
        got['log'] = await b.js("__wmlog.slice()")
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, WM))
    assert got['ws2_tasks'] == [] and got['ws2_stashed'] == [30, 31], ("a new workspace still showed the app windows", got)
    assert got['offws'] == [30, 31], ("arrange was not told which windows are on another workspace", got)
    assert got['ws1_tasks'] == ['30', '31'] and got['offws_back'] == [], got
    assert got['self_minimised'] == [30], ("coming back un-minimised a window the person minimised", got)
    assert 'show:30' not in got['log'], got['log']


@CHROME
def test_a_window_moves_by_its_taskbar_menu_and_raising_it_elsewhere_follows_it():
    got = {}

    async def check(b):
        await _ready(b)
        await b.js("try{localStorage.removeItem('pc_osWorkspaces')}catch(_){};PCOS.addWorkspace();PCOS.switchWorkspace(0);true")
        await b.until(STASHED + ".length===0 && " + TASKS + ".length===2")
        await b.js("(()=>{const t=document.querySelector('#os-bar .os-task[data-kind=\"native\"][data-id=\"31\"]'),r=t.getBoundingClientRect();"
                   "t.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:r.left+8,clientY:r.top+8}))})()")
        await b.until("[...document.querySelectorAll('.os-ctx .os-ctx-b')].some(x=>x.textContent.trim()==='Move to workspace 2')")
        await b.js("[...document.querySelectorAll('.os-ctx .os-ctx-b')].find(x=>x.textContent.trim()==='Move to workspace 2').click()")
        await b.until(STASHED + "+''==='31'")
        await asyncio.sleep(.4)
        got['ws1_tasks'] = await b.js(TASKS)
        # Alt+Tab (or the app activating itself) raises Notes from workspace 2: the desktop goes there.
        await asyncio.sleep(1.6)
        await b.js("__focusRow(31);true")
        await b.until("PCOS.workspaces().current===1")
        await asyncio.sleep(.4)
        got['ws2_tasks'] = await b.js(TASKS)
        got['ws2_stashed'] = await b.js(STASHED)

    asyncio.run(desktop.with_browser('online', '', check, WM))
    assert got['ws1_tasks'] == ['30'], ("the moved window stayed on the first workspace's taskbar", got)
    assert got['ws2_tasks'] == ['31'] and got['ws2_stashed'] == [30], ("raising a window elsewhere did not follow it", got)


@CHROME
def test_a_startup_app_opens_on_the_workspace_chosen_in_system_settings():
    got = {}

    async def check(b):
        await _ready(b)
        await b.js("try{localStorage.removeItem('pc_osWorkspaces')}catch(_){};true")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("PCOS.openSystemSettings()")
        await b.until("!!document.querySelector('.os-set-nav [data-page=\"startup\"]')")
        await b.js("document.querySelector('.os-set-nav [data-page=\"startup\"]').click()")
        page = "document.querySelector('[data-settings-page=\"startup\"]:not([hidden])')"
        await b.until(f"!!{page} && !!{page}.querySelector('[data-startup-view=\"messages\"]')")
        got['before_on'] = await b.js(f"!!{page}.querySelector('[data-startup-ws=\"messages\"]')")
        await b.js(f"(()=>{{const c={page}.querySelector('[data-startup-view=\"messages\"]');c.checked=true;c.dispatchEvent(new Event('change'))}})()")
        await b.until(f"!!{page}.querySelector('[data-startup-ws=\"messages\"]')")
        await b.js(f"(()=>{{const s={page}.querySelector('[data-startup-ws=\"messages\"]');s.value='3';s.dispatchEvent(new Event('change'))}})()")
        got['saved'] = await b.js("ClientSettings.get('startupWorkspace',{})")
        got['over'] = await b.js(f"(()=>{{const c={page}.querySelector('[data-startup-pc-list]');return c.scrollWidth-c.clientWidth}})()")
        # Settings closed and opened again: the choice is still shown.
        await b.js("document.querySelector('.os-set-nav [data-page=\"startup\"]').click()")
        got['shown'] = await b.js(f"{page}.querySelector('[data-startup-ws=\"messages\"]').value")
        # Login: the window opens, maps in the compositor, and lands on workspace 3 -- the desktop stays on 1.
        await b.js("PCOSWin.enabled=()=>true;PCOSWin.open=(v)=>{__map(44,'PosterChan Window — '+v);return {}};true")
        got['count'] = await b.js("PCOS.runStartupApps()")
        await b.until("__rows.some(r=>r.id===44)")
        await b.until("__rows.find(r=>r.id===44).stashed===true")
        got['ws'] = await b.js("PCOS.workspaces()")
        got['tasks'] = await b.js(TASKS)
        await b.js("PCOS.switchWorkspace(2);true")
        await b.until(TASKS + "+''==='44'")

    asyncio.run(desktop.with_browser('online', '', check, WM))
    assert got['before_on'] is False, "an app that does not start at login offered a workspace"
    assert got['saved'] == {'messages': 3}, got
    assert got['over'] <= 1, f"the startup list overflows by {got['over']}px"
    assert got['shown'] == '3', got
    assert got['count'] == 1, got
    assert got['ws'] == {'count': 3, 'current': 0}, ("login did not make the workspace or moved the desktop", got)
    assert '44' not in got['tasks'], ("the startup app opened on the workspace in front", got)


def test_arrange_leaves_windows_on_other_workspaces_alone():
    """Arrange un-minimises what it tiles ("if a window is minimized, it won't grid"); the windows a workspace switch
    minimised are the exception, or Super+G drags every workspace into one grid. Runs the shipped Wayfire bridge."""
    import json
    import subprocess
    root = Path(__file__).resolve().parents[2]
    js = r'''
const W=require(%s);
const wm=Object.create((W.WayfireWM||W.Wayfire||Object.values(W).find(v=>typeof v==='function'&&v.prototype&&v.prototype.arrange)).prototype);
const shown=[],placed=[];
wm.arrangeSettleMs=0;
wm.outputs=async()=>[{name:'A',rect:{x:0,y:0,width:2000,height:1000},work:{x:0,y:0,w:2000,h:1000}}];
const row=(id,st)=>({id,app:'firefox',title:'w'+id,rect:{x:100,y:100,width:400,height:300},stashed:st,focusTime:id,fullscreen:false});
wm.windows=async()=>[row(1,false),row(2,true),row(3,true)];
wm.show=async(id)=>{shown.push(id)};
wm._send=async(m,a)=>{ if(a&&a.id!==undefined) placed.push(a.id); return {}; };
wm.place=async(id)=>{placed.push(id)};
wm.raise=async()=>{};wm.focus=async()=>{};
wm.arrange('grid',{x:10,y:10},{skip:new Set([3])}).then(r=>console.log(JSON.stringify({shown})),e=>console.log(JSON.stringify({err:String(e),shown})));
''' % json.dumps(str(root / 'desktop/wm-wayfire.js'))
    out = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=30)
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert 2 in res['shown'], ("arrange no longer shows a minimised window on this workspace", res, out.stderr[-500:])
    assert 3 not in res['shown'], ("arrange pulled a window from another workspace into the grid", res)
