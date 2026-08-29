"""Persistent PosterChanOS taskbar pins."""
from pathlib import Path
import subprocess


SRC = (Path(__file__).parents[1] / "static/js/client/os.js").read_text()
SIM = Path(__file__).parent / "client" / "taskbar_native_move_runtime.js"


def test_layout_document_keeps_bounded_namespaced_pins():
    assert "pins: []" in SRC
    assert "/^(view|app):" in SRC
    assert "out.pins.length >= 24" in SRC


def test_closed_pins_are_drawn_and_open_windows_are_not_duplicated():
    assert "const openViews = new Set" in SRC
    assert "const openApps = new Set" in SRC
    assert 'data-kind="pin-view"' in SRC
    assert 'data-kind="pin-app"' in SRC


def test_start_menu_and_taskbar_offer_pin_and_unpin():
    assert SRC.count("Pin to taskbar") >= 2
    assert SRC.count("Unpin from taskbar") >= 2
    assert "setPinned('app', app, !pinned)" in SRC
    assert "setPinned('view', view, !pinned)" in SRC


def test_running_task_context_menu_can_move_recover_and_close_windows():
    assert "function taskbarMove(w)" in SRC
    assert "osw-taskbar-moving" in SRC
    assert "{label:'Move',run:()=>taskbarMove(running)}" in SRC
    assert "{label:'Close',run:()=>closeWin(running)}" in SRC
    assert "keepFrameReachable(w);_natGesture(w,false)" in SRC
    assert "function nativeTaskbarMove(row)" in SRC
    assert "{label:'Move',run:()=>nativeTaskbarMove(w)}" in SRC
    assert "if(!w)w=adoptNative(row)" in SRC
    menu = SRC[SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"):
               SRC.index("$$('.os-native-max'", SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"))]
    assert "move position cursor" not in menu


def test_taskbar_move_recovers_snapped_geometry_and_escape_restores_the_zone():
    start = SRC.index("function taskbarMove(w)")
    body = SRC[start:SRC.index("function nativeTaskbarMove(row)", start)]
    assert "snap:w.snap||null" in body
    assert "if(old.snap)unsnap(w)" in body
    assert "if(old.snap)snapTo(w,old.snap)" in body
    assert "left:old.left,top:old.top" in body


def test_native_taskbar_move_arms_before_compositor_focus_at_runtime():
    run = subprocess.run(["node", str(SIM)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "native taskbar Move holds" in run.stdout


def test_adopted_native_task_gets_move_and_close_without_an_ephemeral_pin():
    menu = SRC[SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"):
               SRC.index("$$('.os-native-max'", SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"))]
    assert "running.native==null" in menu
    assert "if(running)actions.push({label:'Move'" in menu
    assert "{label:'Close',run:()=>closeWin(running)}" in menu
    assert "if(key){" in menu


def test_taskbar_context_menu_is_anchored_in_the_desktops_scaled_coordinate_space():
    start = SRC.index("function showCtx(")
    body = SRC[start:SRC.index("function iconMenu(", start)]
    helper = SRC[SRC.index("function ctxPosition("):start]
    assert "desk.getBoundingClientRect()" in body
    assert "desk.offsetWidth" in body and "desk.offsetHeight" in body
    assert "ar.left-dr.left" in helper and "ar.top-dr.top" in helper
    # Both native and PosterChan task buttons pass the button, not just viewport pointer coordinates.
    task = SRC[SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"):
               SRC.index("$$('.os-native-max'", SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"))]
    assert task.count("],b);") >= 1
    assert "showCtx(e.clientX, e.clientY, actions, b)" in task


def test_taskbar_menu_runtime_does_not_reserve_the_taskbar_twice():
    start = SRC.index("function ctxPosition(")
    end = SRC.index("function showCtx(", start)
    fn = SRC[start:end]
    script = fn + """
global.vwL=()=>800;global.vhL=()=>600;global.zf=()=>1;
const p=ctxPosition({left:0,top:0,width:800,height:600},800,600,200,80,
  {left:300,top:600},300,600);
process.stdout.write(JSON.stringify(p));
"""
    run = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert run.stdout == '{"left":300,"top":514}'
    assert "(dh||vhL())-mh-4" in fn


def test_start_menu_can_add_and_remove_apps_from_desktop():
    assert "Add ' + label + ' to the desktop" in SRC
    assert "Hide ' + label + ' from the desktop" in SRC
    assert "showItem(view)" in SRC
    assert "hideItem(view)" in SRC
