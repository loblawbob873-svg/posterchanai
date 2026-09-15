from pathlib import Path
import json
import shutil
import subprocess
import textwrap

import pytest
from tests.overlay_paths import shell_ebuild


ROOT = Path(__file__).resolve().parents[1]
NATIVE = (ROOT / "desktop/remotecontrol.js").read_text()
MAIN = (ROOT / "desktop/main.js").read_text()
PRELOAD = (ROOT / "desktop/preload.js").read_text()
EBUILD = shell_ebuild().read_text()


def test_native_remote_input_is_shell_only_and_origin_guarded():
    block = MAIN[MAIN.index("ipcMain.handle('pc:remote:input'"):]
    block = block[:block.index("});") + 3]
    assert "fsGuard(e)" in block
    assert "if(!SHELL_MODE) return false" in block
    assert "remotecontrol.input(input)" in block
    assert "pcRemoteControl" in PRELOAD
    assert "pc:remote:release" in MAIN and "remotecontrol.release()" in MAIN


def test_native_remote_input_is_bounded_and_rate_limited():
    assert "e.type==='move' && now-lastAt<16" in NATIVE
    assert "pendingCursor.x=x;pendingCursor.y=y" in NATIVE
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
    assert "screen.getDisplayNearestPoint(cursor)" not in MAIN
    # Shell input uses direct compositor coordinates; legacy standalone callers retain ydotool.
    assert 'remotecontrol.setPositioner(async(x,y)=>' in MAIN
    assert 'manager.setCursor(x,y)' in MAIN
    assert "'posterchan-shell/set-cursor',{x,y}" in (ROOT/'desktop/wm-wayfire.js').read_text()



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


def test_direct_positioning_and_release_wait_for_pending_press():
    js = f"""
      const cp=require('child_process'),calls=[];
      cp.execFile=(file,args,opts,cb)=>setTimeout(()=>{{calls.push(args);cb(null);}},5);
      const rc=require({json.dumps(str(ROOT / 'desktop/remotecontrol.js'))});
      rc.setPositioner(async(x,y)=>{{calls.push(['warp',x,y]);return true;}});
      (async()=>{{
        const right=rc.input({{type:'button',button:2,down:true,x:-100,y:200}});
        const middle=rc.input({{type:'button',button:1,down:true,x:300,y:400}});
        const key=rc.input({{type:'key',code:42,down:true}});
        const release=rc.release();
        await Promise.all([right,middle,key,release]);
        console.log(JSON.stringify(calls.filter(c=>c[0]!=='--user')));
      }})();
    """
    run = subprocess.run(['node','-e',js],capture_output=True,text=True,timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [
        ['warp',-100,200], ['click','0x41'], ['warp',300,400], ['click','0x42'],
        ['key','42:1'], ['key','42:0'], ['click','0x81','0x82']]


def test_motion_coalesces_to_final_point_without_crossing_button_barriers():
    js = f"""
      const calls=[],cp=require('child_process');
      cp.execFile=(f,a,o,cb)=>{{calls.push(a);cb(null);}};
      const rc=require({json.dumps(str(ROOT / 'desktop/remotecontrol.js'))});
      rc.setPositioner(async(x,y)=>{{calls.push(['warp',x,y]);return true;}});
      (async()=>{{
        const jobs=[];
        for(let i=0;i<100;i++)jobs.push(rc.input({{type:'absolute',x:i,y:i}}));
        jobs.push(rc.input({{type:'button',button:0,down:true,x:100,y:100}}));
        for(let i=101;i<200;i++)jobs.push(rc.input({{type:'absolute',x:i,y:i}}));
        jobs.push(rc.release());await Promise.all(jobs);
        console.log(JSON.stringify(calls.filter(c=>c[0]!=='--user')));
      }})();
    """
    run=subprocess.run(['node','-e',js],capture_output=True,text=True,timeout=10)
    assert run.returncode == 0,run.stderr
    assert json.loads(run.stdout) == [
        ['warp',99,99],['warp',100,100],['click','0x40'],['warp',199,199],['click','0x80']]


def test_portal_monitor_choice_handles_equal_sizes_scaling_and_disconnects():
    mapping = MAIN[MAIN.index('function remoteAbsolutePoint('):MAIN.index("ipcMain.handle('pc:remote:input'")]
    configure = MAIN[MAIN.index('function remoteCaptureCandidates('):MAIN.index("ipcMain.handle('pc:remote:configure'")]
    js = f"""
      const assert=require('node:assert/strict');
      let remoteControlDisplayId='',remoteControlDisplayExplicit=false,remoteCaptureGeneration=1;
      let displays=[
        {{id:10,label:'DP-1',bounds:{{x:-1920,y:-100,width:1920,height:1280}},scaleFactor:2}},
        {{id:20,label:'DP-2',bounds:{{x:0,y:0,width:1920,height:1280}},scaleFactor:2}}
      ];
      const screen={{getAllDisplays:()=>displays,getDisplayNearestPoint:()=>{{throw Error('must not guess')}}}};
      {mapping}
      {configure}
      const reset=()=>{{remoteControlDisplayId='';remoteControlDisplayExplicit=false;}};
      (async()=>{{
        let choices=0;
        const choose=async rows=>{{choices++;assert.equal(rows.length,2);return 0;}};
        assert.equal((await configureRemoteCapture({{width:3840,height:2560}},choose)).displayId,'10');
        assert.equal(choices,1,'equal-size monitors require explicit choice');
        assert.deepEqual(remoteAbsolutePoint(displays,'10',{{x:100,y:100}},0,0),{{x:-1920,y:-100}});
        assert.deepEqual(remoteAbsolutePoint(displays,'10',{{x:100,y:100}},1,1),{{x:-1,y:1179}});
        assert.deepEqual(remoteAbsolutePoint(displays,'10',{{x:100,y:100}},.5,.5),{{x:-960,y:540}});
        await configureRemoteCapture({{width:1920,height:1280}},()=>{{throw Error('must keep selection')}});
        assert.equal(remoteAbsolutePoint(displays,'missing',{{x:10,y:10}},.5,.5),null);
        displays=displays.slice(1);
        assert.equal((await configureRemoteCapture({{width:3840,height:2560}},choose)).ok,false);
        reset();assert.equal((await configureRemoteCapture({{width:3840,height:2560}},choose)).displayId,'20');
        displays.unshift({{id:10,bounds:{{x:-1920,y:0,width:1920,height:1080}},scaleFactor:1}});
        reset();assert.equal((await configureRemoteCapture({{width:3840,height:2560}},choose)).displayId,'20');
        reset();assert.equal((await configureRemoteCapture({{width:1600,height:900}},async()=>2)).reason,'cancelled');
        assert.equal(remoteControlDisplayId,'');
        assert.equal((await configureRemoteCapture({{width:1600,height:900}},async()=>{{displays=[];return 0;}})).reason,'display-unavailable');
        displays=[{{id:1,bounds:{{x:0,y:0,width:1000,height:1000}}}},{{id:2,bounds:{{x:1000,y:0,width:1000,height:1000}}}}];
        reset();assert.equal((await configureRemoteCapture({{width:1000,height:1000}},async()=>{{remoteCaptureGeneration++;return 0;}})).reason,'capture-changed');
        remoteControlDisplayId='2';remoteControlDisplayExplicit=true;
        assert.equal((await configureRemoteCapture({{width:1000,height:1000}},()=>{{throw Error('explicit source')}})).displayId,'2');
        assert.equal((await configureRemoteCapture({{width:NaN,height:1000}},choose)).ok,false);
        console.log('ok');
      }})().catch(e=>{{console.error(e);process.exit(1)}});
    """
    run = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == 'ok'


def test_disconnected_shared_monitor_rejects_pointer_keys_and_wheel():
    handler = MAIN[MAIN.index("ipcMain.handle('pc:remote:input'"):MAIN.index('function remoteCaptureCandidates(')]
    mapping = MAIN[MAIN.index('function remoteAbsolutePoint('):MAIN.index("ipcMain.handle('pc:remote:input'")]
    js = f"""
      const assert=require('node:assert/strict');let handler;
      const ipcMain={{handle:(_,fn)=>handler=fn}},fsGuard=()=>{{}},SHELL_MODE=true;
      const remoteControlDisplayId='shared',calls=[];
      const screen={{getAllDisplays:()=>[{{id:'other',bounds:{{x:0,y:0,width:100,height:100}}}}],
        getCursorScreenPoint:()=>({{x:20,y:20}})}};
      const remotecontrol={{input:i=>calls.push(i)}};
      {mapping}
      {handler}
      for(const input of [{{type:'absolute',x:.5,y:.5}},{{type:'button',button:0,down:true}},
          {{type:'key',code:30,down:true}},{{type:'wheel',dy:1}}])assert.equal(handler({{}},input),false);
      assert.equal(calls.length,0);
    """
    run = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
