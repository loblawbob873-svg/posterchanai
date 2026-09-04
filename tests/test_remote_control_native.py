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
    assert "remoteAbsolutePoint(screen.getAllDisplays()" in MAIN
    assert "screen.getDisplayNearestPoint(cursor)" in MAIN
    # ABSOLUTE PLACEMENT IS ydotool NOW, and that is a fix rather than a rename. It was
    # `swaymsg seat0 cursor set` -- a Sway command, on a session where the binary is not even
    # installed -- so every absolute packet failed while relative motion, clicks and keys all
    # worked: the remote pointer read as STUCK, not as a missing program.
    # Asserted on the CODE, not on the file: the comment above `setCursor` names the command it
    # replaced, which is the sentence a future reader most needs and the one a bare
    # `"swaymsg" not in NATIVE` would forbid.
    body = NATIVE.split("function setCursor(", 1)[1].split("\nfunction ", 1)[0]
    assert "swaymsg" not in body, body
    assert "'mousemove','--absolute'" in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_host_mapping_hits_exact_edges_on_the_frozen_monitor(tmp_path):
    start = MAIN.index("function remoteAbsolutePoint(")
    end = MAIN.index("ipcMain.handle('pc:remote:input'", start)
    helper = MAIN[start:end]
    driver = tmp_path / "map.js"
    driver.write_text(textwrap.dedent(f"""
      const screen={{getDisplayNearestPoint:()=>displays[0]}};
      {helper}
      const displays=[
        {{id:'left',bounds:{{x:-1920,y:0,width:1920,height:1080}}}},
        {{id:'right',bounds:{{x:0,y:-120,width:3840,height:2160}}}}
      ];
      const out=[
        remoteAbsolutePoint(displays,'left',{{x:0,y:0}},0,0),
        remoteAbsolutePoint(displays,'left',{{x:0,y:0}},1,1),
        remoteAbsolutePoint(displays,'right',{{x:0,y:0}},0,0),
        remoteAbsolutePoint(displays,'right',{{x:0,y:0}},1,1),
        remoteAbsolutePoint(displays,'right',{{x:0,y:0}},.5,.5),
        remoteAbsolutePoint(displays,'right',{{x:0,y:0}},1.01,.5)
      ];
      console.log(JSON.stringify(out));
    """), encoding="utf-8")
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [
        {"x": -1920, "y": 0}, {"x": -1, "y": 1079},
        {"x": 0, "y": -120}, {"x": 3839, "y": 2039},
        {"x": 1920, "y": 960}, None,
    ]


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_position_and_button_are_atomic_ordered_and_duplicate_release_is_ignored(tmp_path):
    driver = tmp_path / "remote-button.js"
    driver.write_text(textwrap.dedent(f"""
      const cp=require('child_process'), calls=[];
      cp.execFile=(file,args,opts,cb)=>setTimeout(()=>{{calls.push([file,args]);cb&&cb(null,'','');}},2);
      const rc=require({json.dumps(str(ROOT / 'desktop/remotecontrol.js'))});
      (async()=>{{
        const down=rc.input({{type:'button',button:0,down:true,x:100,y:200}});
        const up=rc.input({{type:'button',button:0,down:false,x:300,y:400}});
        const duplicate=rc.input({{type:'button',button:0,down:false,x:500,y:600}});
        console.log(JSON.stringify({{result:await Promise.all([down,up,duplicate]),calls}}));
      }})().catch(e=>{{console.error(e);process.exit(1)}});
    """), encoding="utf-8")
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["result"] == [True, True, True]
    input_calls = [call for call in result["calls"] if call[0] != "/usr/bin/systemctl"]
    # ORDER IS THE POINT: the position lands before the button, every time, and the second release
    # of a button already up is dropped rather than replayed.
    assert input_calls == [
        ["/usr/bin/ydotool", ["mousemove", "--absolute", "-x", "100", "-y", "200"]],
        ["/usr/bin/ydotool", ["click", "0x40"]],
        ["/usr/bin/ydotool", ["mousemove", "--absolute", "-x", "300", "-y", "400"]],
        ["/usr/bin/ydotool", ["click", "0x80"]],
    ]
