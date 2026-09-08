from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WAYFIRE = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text()
MAIN = (ROOT / "desktop/main.js").read_text()
WM = (ROOT / "desktop/wm.js").read_text()
PRELOAD = (ROOT / "desktop/preload.js").read_text()


def test_popup_maps_invisible_until_wayland_position_is_known():
    """THE COMPOSITOR HALF OF THIS IS GONE, AND THE RENDERER HALF IS WHY THAT IS SURVIVABLE.

    Sway mapped this title at `opacity set 0` and `placeAndReveal` set it back to 1 in the SAME
    command as the geometry, so the surface was never painted anywhere but its final position.
    Wayfire 0.10 has no equivalent per-window opacity rule, so the outer safety net is gone -- what
    remains is the renderer-side shield, which is compositor-neutral and was always the inner one:
    the BrowserWindow is opened `transparent`, preload inserts `html,body{opacity:0}` before first
    paint, and main.js only sends `pc:host:popup-placed` after the window has been positioned.

    That is asserted in full by the two tests below; this one only pins that the flyout is NOT
    decorated by the compositor, because a server-side title bar would be drawn at the centre
    placement regardless of what the page inside it is doing.
    """
    assert 'title contains "PosterChan Window"' in WAYFIRE
    ignore = next(line for line in WAYFIRE.splitlines() if line.startswith("ignore_views"))
    assert "PosterChan" in ignore


def test_popup_geometry_and_reveal_are_one_compositor_transaction():
    """Sway's backend still commits geometry and reveal together; it is kept and still tested."""
    body = WM.split("async placeAndReveal(", 1)[1].split("async placeOnOutput(", 1)[0]
    command = body.split("return this.command(", 1)[1]
    assert "resize set" in command
    assert "move absolute position" in command
    assert "opacity set 1" in command
    assert command.count("this.command(") == 0


def test_popup_main_path_uses_atomic_reveal_not_plain_place():
    body = MAIN.split("async function placePopupWindow", 1)[1].split(
        "ipcMain.handle('pc:popup:close'", 1
    )[0]
    assert "placeAndReveal" in body
    assert ".place(Number(row.id)" not in body


def test_popup_renderer_stays_transparent_until_placement_acknowledgement():
    create = MAIN.split("const p = new BrowserWindow({", 1)[1].split("});", 1)[0]
    assert "transparent: true" in create
    assert "#00000000" in create
    assert "--pc-popup-surface" in create
    placement = MAIN.split("async function placePopupWindow", 1)[1].split(
        "ipcMain.handle('pc:popup:close'", 1
    )[0]
    assert placement.index("placeAndReveal") < placement.index("pc:host:popup-placed")
    assert "webFrame.insertCSS" in PRELOAD
    assert "opacity:0!important" in PRELOAD
    assert "ipcRenderer.once('pc:host:popup-placed'" in PRELOAD
    assert "webFrame.removeInsertedCSS" in PRELOAD
    assert placement.count("pc:host:popup-placed") == 2  # positioned success plus the no-compositor fallback


def test_the_shield_has_a_floor_because_the_latch_is_set_before_the_attempt():
    """A LIVE, FULLY PAINTED MENU AT opacity:0 IS INDISTINGUISHABLE FROM A DEAD BUTTON.

    The sheet is lifted only by `pc:host:popup-placed`, and `placePopupWindow` has three ways of
    never sending it: the window was destroyed, `_popupWin !== win` (a second popup replaced this
    one mid-flight), or the send itself throws. Observed on the real desktop while chasing the tray
    flyout: a mapped `PosterChan Popup` view of the right size, in the right place, with `.os-pop`
    drawn inside it — and nothing on screen.

    Placement's own worst case is bounded (12 attempts 60ms apart, then it sends regardless), so
    anything past a second means the message is not coming. Revealing an unplaced menu shows it
    briefly where the compositor put it — the flash this shield exists to prevent — but a flash is a
    menu you can use, and this is the case where the alternative is none.
    """
    body = PRELOAD.split("--pc-popup-surface", 1)[1].split("\n}\n", 1)[0]
    assert "setTimeout(" in body, body
    floor = int(body.split("setTimeout(()=>reveal(", 1)[1].split("),", 1)[1].split(")", 1)[0])
    # Longer than placement's worst case (12 * 60ms) so it can never pre-empt a real placement…
    assert floor > 720, floor
    # …and short enough that nobody experiences it as a dead control.
    assert floor <= 3000, floor
    # One reveal path, so a placement that arrives after the floor cannot re-insert or double-remove.
    assert body.count("removeInsertedCSS") == 2, body
    assert "if(placed) return;" in body


def test_focus_is_asked_for_not_inferred_from_a_flag_wayfire_never_sets():
    """`outputs().find(o => o.focused)` is always undefined on Wayfire.

    `window-rules/list-outputs` answers name/id/geometry and nothing else, so normalizeOutput can
    only ever set focused:false. Two decisions read that flag — which surface owns a Start/popup
    gesture, and which output's ORIGIN is added to a popup's coordinates — so on two monitors every
    menu was delivered to the first surface and placed on the leftmost screen, whichever screen the
    person was on. Reported as "start menu and taskbar widgets are not popping up in the appropriate
    place" on a 2x3840x2560 desktop.

    Both backends must answer one question, and main.js must ASK it rather than scan for the flag.
    """
    root = Path(__file__).resolve().parents[1] / "desktop"
    wayfire = (root / "wm-wayfire.js").read_text(encoding="utf-8")
    sway = (root / "wm.js").read_text(encoding="utf-8")
    main = (root / "main.js").read_text(encoding="utf-8")

    assert "async focusedOutputName()" in wayfire, "the Wayfire backend cannot report focus"
    assert "window-rules/get-focused-output" in wayfire, "focus is not read from the compositor"
    assert "async focusedOutputName()" in sway, "the sway backend lost the shared question"

    # Both decision points must call it. Either alone leaves half the bug in place: the gesture
    # reaches the right surface but the window is placed on the wrong screen, or the reverse.
    assert main.count("focusedOutputName()") >= 2, \
        "a decision still infers focus from a flag one compositor never sets"

    # And the hot path must stay pure IPC — outputs() runs once per pointer frame while dragging.
    outs = wayfire[wayfire.index("async outputs()"):]
    outs = outs[:outs.index("\n  ")] if "\n  " in outs else outs
    assert "_randr" not in outs, "outputs() now spawns wlr-randr on the drag path"


def test_start_is_activated_above_social_after_placement_and_stale_popups_do_not_take_focus():
    import subprocess
    src=MAIN[MAIN.index('async function placePopupWindow('):MAIN.index("ipcMain.handle('pc:popup:close'")]
    script="""
const assert=require('node:assert/strict');
let active='Social',_popupWin,_popupKind='start';const POPUP_TITLE='PosterChan Popup';
const order=[];let replaced=false;
const popup={isDestroyed:()=>false,focus:()=>{active='Start';order.push('electron-focus')},
 webContents:{send:()=>order.push('reveal')}};
const wm=()=>({windows:async()=>[{id:88,title:POPUP_TITLE}],outputs:async()=>[],
 placeAndReveal:async()=>{order.push('place');if(replaced)_popupWin={};},
 focus:async()=>{active='Start';order.push('compositor-focus')}});
const snapPopupToWorkArea=want=>want;
"""+src+"""
(async()=>{
 _popupWin=popup;await placePopupWindow(popup,{x:0,y:0,w:400,h:500});
 assert.equal(active,'Start');assert.deepEqual(order,['place','compositor-focus','electron-focus','reveal']);
 active='Social';order.length=0;replaced=true;_popupWin=popup;
 await placePopupWindow(popup,{x:0,y:0,w:400,h:500});
 assert.equal(active,'Social');assert.deepEqual(order,['place']);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    run=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=15)
    assert run.returncode==0,run.stderr


def test_scaled_popups_fit_above_taskbar_on_small_large_and_portrait_outputs():
    import subprocess
    source=MAIN[MAIN.index('const _BAR_FLYOUTS = new Set('):MAIN.index("ipcMain.handle('pc:popup:close'")]
    script='''
const assert=require('node:assert/strict');
const _workAreas=new Map(),POPUP_TITLE='PosterChan Popup';
let _popupWin,_popupKind='noti',row,outputs,placed,clientSize;
const wm=()=>({windows:async()=>[row],outputs:async()=>outputs,
 placeAndReveal:async(id,x,y,w,h)=>{placed={x,y,w,h}},focus:async()=>{}});
'''+source+'''
(async()=>{
 for(const [width,height,x,y] of [[1024,768,0,0],[1366,768,-1366,0],[1920,1080,0,0],[3840,2560,3840,0],[1080,1920,0,-1920]]){
  for(const scale of [1,1.25,1.5,2])for(const kind of ['start','noti','tray','net']){
   const reserve=Math.round(48*scale),bounds={width:430,height:1200};
   _workAreas.clear();_workAreas.set('output',{x,y,w:width,h:height-reserve,reserve});
   outputs=[{rect:{x,y,width,height}}];
   row={id:88,title:POPUP_TITLE,rect:{x,y,width:bounds.width*scale,height:bounds.height*scale}};
   _popupWin={isDestroyed:()=>false,getBounds:()=>bounds,setSize:(w,h)=>{clientSize={w,h}},focus:()=>{},webContents:{send:()=>{}}};
   _popupKind=kind;
   await placePopupWindow(_popupWin,{x:x+width-100,y:y+10,w:bounds.width,h:bounds.height});
   assert(Math.abs(clientSize.w*scale-placed.w)<=1 && Math.abs(clientSize.h*scale-placed.h)<=1);
   assert(placed.x>=x && placed.x+placed.w<=x+width+1,JSON.stringify(placed));
   assert(placed.y>=y && placed.y+placed.h<=y+height-reserve+1,JSON.stringify(placed));
  }
 }
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
