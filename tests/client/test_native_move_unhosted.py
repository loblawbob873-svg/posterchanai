"""Super+Shift+Arrow and the taskbar's "Move to other display" move a native application that is NOT
hosted in a PosterChan frame.

With hosting off (the default) Firefox/Telegram are plain compositor windows: the tick looked only for a
hosted frame, and the frame handoff cannot run without one, so the window never moved (measured on two
outputs of an isolated copy of the installed desktop; after the fix Telegram moved HEADLESS-2 ->
HEADLESS-1 -> HEADLESS-2). Runs the SHIPPED `moveNativeToMonitor` from os.js under node, and pins the
main-process route it depends on.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")


def _function(src, name):
    start = src.index(f"function {name}(")
    brace = src.index("{", start)
    depth = 0
    for pos in range(brace, len(src)):
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
            if depth == 0:
                return src[start:pos + 1]
    raise AssertionError(name)


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_unhosted_native_window_moves_through_the_compositor():
    script = f"""
const calls=[], frameMoves=[];
let hosted=[], nativeTasks=[], outputsRight=true;
const nativeWins=()=>hosted;
const moveWindowToMonitor=(w,d)=>{{frameMoves.push([w.native,d]);return true;}};
const moveToOtherMonitor=w=>{{frameMoves.push([w.native,'other']);return true;}};
globalThis.window={{pcWM:{{moveToOutput:async(id,dir)=>{{calls.push([id,dir]);return dir==='left';}}}}}};
const pcWM=window.pcWM;
{_function(OS, 'moveNativeToMonitor')}
(async()=>{{
  const out={{}};
  nativeTasks=[{{id:13,app:'org.telegram.desktop'}}];
  out.keyed=await moveNativeToMonitor(13,'left'); out.callsKeyed=calls.splice(0);
  out.other=await moveNativeToMonitor(13,''); out.callsOther=calls.splice(0);
  out.stranger=await moveNativeToMonitor(99,'left'); out.callsStranger=calls.splice(0);
  hosted=[{{native:21}}];
  out.hosted=await moveNativeToMonitor(21,'right'); out.frameMoves=frameMoves.slice(); out.callsHosted=calls.splice(0);
  process.stdout.write(JSON.stringify(out));
}})();
"""
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got["keyed"] is True and got["callsKeyed"] == [[13, "left"]]
    assert got["other"] is True and got["callsOther"] == [[13, "right"], [13, "left"]], \
        "'other display' tries each direction until an output exists"
    assert got["stranger"] is False and got["callsStranger"] == [], "a window this taskbar does not list is left alone"
    assert got["hosted"] is True and got["frameMoves"] == [[21, "right"]] and got["callsHosted"] == []


def test_keyboard_tick_and_taskbar_menu_both_use_it():
    tick = OS[OS.index("/^pc:move-native:\\d+:(left|right|up|down)$/.test(p)"):]
    tick = tick[:tick.index("});")]
    assert "moveNativeToMonitor(id,direction)" in tick
    menu = OS[OS.index("{label:'Move to other display',run:()=>{"):]
    menu = menu[:menu.index("}},")]
    assert "else moveNativeToMonitor(w.id,'')" in menu


def test_main_route_moves_the_window_to_the_adjacent_output():
    route = MAIN[MAIN.index("ipcMain.handle('pc:wm:move-to-output'"):MAIN.index("ipcMain.handle('pc:wm:handoff'")]
    assert "fsGuard(e)" in route
    assert "/^(left|right|up|down)$/.test(direction)" in route
    assert "adjacentShellSurface(e, direction)" in route
    assert "wm().placeOnOutput(nativeId, record.assignment.rect, direction)" in route
    assert "moveToOutput: (id, direction) => ipcRenderer.invoke('pc:wm:move-to-output'" in PRELOAD
