from pathlib import Path
import json
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
NATIVE = (ROOT / "desktop/remotecontrol.js").read_text()
MAIN = (ROOT / "desktop/main.js").read_text()
PRELOAD = (ROOT / "desktop/preload.js").read_text()
EBUILD = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()


def test_native_remote_input_is_shell_only_and_origin_guarded():
    block = MAIN[MAIN.index("ipcMain.handle('pc:remote:input'"):]
    block = block[:block.index("});") + 3]
    assert "fsGuard(e)" in block
    assert "if(!SHELL_MODE) return false" in block
    assert "remotecontrol.input(input)" in block
    assert "pcRemoteControl" in PRELOAD
    assert "pc:remote:release" in MAIN and "remotecontrol.release()" in MAIN


def test_native_remote_input_is_bounded_and_rate_limited():
    assert "(e.type==='move'||e.type==='absolute') && now-lastAt<16" in NATIVE
    assert "Math.abs(dx)>240||Math.abs(dy)>240" in NATIVE
    assert "e.type==='wheel'" in NATIVE
    assert "KEY_CODES.has(code)" in NATIVE
    assert "typeof e.down!=='boolean'" in NATIVE
    assert "execFile('/usr/bin/ydotool'" in NATIVE
    assert "exec(" not in NATIVE
    assert "queue=queue.then" in NATIVE
    assert "heldKeys" in NATIVE and "heldButtons" in NATIVE


def test_absolute_remote_pointer_maps_through_the_host_display():
    assert "input.type === 'absolute'" in MAIN
    assert "screen.getCursorScreenPoint()" in MAIN
    assert "screen.getAllDisplays().find" in MAIN
    assert "screen.getDisplayNearestPoint(point)" in MAIN
    assert "execFile('/usr/bin/swaymsg',['seat','seat0','cursor','set'" in NATIVE


def test_posterchanos_installs_and_enables_private_user_input_daemon():
    assert "x11-misc/ydotool" in EBUILD
    assert "systemctl --global enable ydotool.service" in EBUILD


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_native_bridge_executes_only_validated_argument_arrays(tmp_path):
    driver = tmp_path / "remote-native.js"
    driver.write_text(textwrap.dedent(f"""
      const cp=require('child_process'), calls=[];
      cp.execFile=(file,args,opts,cb)=>{{calls.push([file,args]);cb&&cb(null,'','');}};
      const rc=require({json.dumps(str(ROOT / 'desktop/remotecontrol.js'))});
      (async()=>{{
        const validMove=await rc.input({{type:'move',dx:12.4,dy:-8.7}});
        await new Promise(r=>setTimeout(r,20));
        const hugeMove=await rc.input({{type:'move',dx:999,dy:0}});
        const validKey=await rc.input({{type:'key',code:30,down:true}});
        const wheel=await rc.input({{type:'wheel',dy:1}});
        const badKey=await rc.input({{type:'key',code:116,down:true}});
        await rc.release();
        console.log(JSON.stringify({{validMove,hugeMove,validKey,wheel,badKey,calls}}));
      }})().catch(e=>{{console.error(e);process.exit(1)}});
    """), encoding="utf-8")
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["validMove"] is True and result["hugeMove"] is False
    assert result["wheel"] is True
    assert result["validKey"] is True and result["badKey"] is False
    ydotool = [args for file, args in result["calls"] if file == "/usr/bin/ydotool"]
    assert ydotool == [["mousemove", "12", "-9"], ["key", "30:1"], ["mousemove", "--wheel", "0", "1"], ["key", "30:0"]]
