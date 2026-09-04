import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wayfire_backend_contract_and_wire_protocol(tmp_path):
    script = tmp_path / "wayfire-contract.js"
    script.write_text(textwrap.dedent(f"""
      const net=require('net'),fs=require('fs');
      const sock={json.dumps(str(tmp_path / 'wayfire-wayland-1.socket'))};
      const calls=[];
      const views=[{{id:7,pid:700,'app-id':'firefox',title:'Web',mapped:true,activated:true,
        fullscreen:false,minimized:false,'tiled-edges':0,'output-id':2,
        'last-focus-timestamp':123456,
        geometry:{{x:1920,y:0,width:900,height:700}}}}];
      const outputs=[{{id:1,name:'LEFT',focused:false,scale:1,geometry:{{x:0,y:0,width:1920,height:1080}}}},
                     {{id:2,name:'RIGHT',focused:true,scale:2,geometry:{{x:1920,y:0,width:2560,height:1440}}}}];
      const frame=o=>{{const b=Buffer.from(JSON.stringify(o)),h=Buffer.alloc(4);h.writeUInt32LE(b.length);return Buffer.concat([h,b]);}};
      const server=net.createServer(c=>{{let b=Buffer.alloc(0);c.on('data',x=>{{b=Buffer.concat([b,x]);while(b.length>=4){{const n=b.readUInt32LE();if(b.length<4+n)return;const q=JSON.parse(b.subarray(4,4+n));b=b.subarray(4+n);calls.push(q);let r={{ok:true}};if(q.method==='window-rules/list-views')r=views;if(q.method==='window-rules/list-outputs')r=outputs;if(q.method==='list-methods')r={{methods:['window-rules/list-views']}};c.write(frame(r));}}}});}});
      server.listen(sock,async()=>{{
        process.env.WAYFIRE_SOCKET=sock;const {{WM}}=require({json.dumps(str(ROOT / 'desktop/wm.js'))});const w=new WM();
        const list=await w.windows(),outs=await w.outputs();await w.focus(7);await w.hide(7);await w.show(7);
        await w.fullscreen(7,true);await w.placeAndReveal(7,2200,100,800,600);await w.close(7);await w.applyChrome();
        console.log(JSON.stringify({{backend:w.backend,list,outs,calls}}));w.sock.destroy();server.close();
      }});
    """), encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["backend"] == "wayfire"
    assert result["list"][0]["app"] == "firefox"
    assert result["list"][0]["floating"] is True
    assert result["list"][0]["focusTime"] == 123456
    assert result["outs"][1]["rect"] == {"x": 1920, "y": 0, "width": 2560, "height": 1440}
    methods = [x["method"] for x in result["calls"]]
    assert methods == [
        "window-rules/list-views", "window-rules/list-outputs",
        "window-rules/focus-view", "wm-actions/set-minimized",
        "wm-actions/set-minimized", "wm-actions/set-fullscreen",
        "window-rules/list-outputs", "window-rules/configure-view",
        "window-rules/close-view",
    ]
    configured = result["calls"][-2]["data"]
    assert configured["output_id"] == 2
    assert configured["geometry"] == {"x": 280, "y": 100, "width": 800, "height": 600}
    # This is the Start/popup path: global renderer coordinates on RIGHT must become
    # output-local coordinates in the same configure transaction, never a detached centre frame.
    assert "tiled-edges" not in configured, "Wayfire 0.10 configure-view accepts geometry, not theme/tiling chrome"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shell_assignment_translates_asymmetric_secondary_output_to_local_coordinates():
    """Electron display assignments are global; Wayfire configure-view is output-local."""
    js = f"""
      const {{WayfireWM}}=require({json.dumps(str(ROOT / 'desktop/wm-wayfire.js'))});
      const w=new WayfireWM('/tmp/not-used'),calls=[];
      w.outputs=async()=>[{{id:4,name:'DP-1',rect:{{x:-1600,y:240,width:1600,height:900}}}},
                          {{id:9,name:'DP-2',rect:{{x:0,y:-180,width:3440,height:1440}}}}];
      w.fullscreen=async(id,on)=>calls.push({{method:'fullscreen',id,on}});
      w._viewConfig=async(id,rect,extra)=>calls.push({{method:'configure',id,rect,extra}});
      (async()=>{{await w.assignShell(77,{{output:'DP-2',rect:{{x:0,y:-180,width:3440,height:1440}}}});
        console.log(JSON.stringify(calls));}})();
    """
    run = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [
        {"method": "fullscreen", "id": 77, "on": False},
        {"method": "configure", "id": 77,
         "rect": {"x": 0, "y": 0, "w": 3440, "h": 1440}, "extra": {"output_id": 9}},
    ]


def test_wayfire_backend_is_theme_neutral_and_sway_is_rollback_default():
    src = (ROOT / "desktop/wm-wayfire.js").read_text()
    factory = (ROOT / "desktop/wm.js").read_text()
    assert "applyChrome(){return Promise.resolve(true);}" in src
    assert "macOS and Windows chrome" in src
    assert "process.env.WAYFIRE_SOCKET" in factory
    assert "POSTERCHAN_WM_FORCE_SWAY" in factory
    assert "return new SwayWM(sockPath)" in factory


def test_shell_drag_guard_is_exact_id_not_shared_app_id():
    main = (ROOT / "desktop/main.js").read_text()
    assert "Number(record.conId)===Number(row.id)" in main
    assert "scheduleDisplayReconcile()" in main
    guard = main[main.index("Wayfire's move plugin cannot exclude"):]
    guard = guard[:guard.index("wm().on('tick'")]
    assert "row.app" not in guard


def test_installed_window_shortcuts_use_wayfire_ipc_not_swaymsg():
    helper = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap").read_text()
    branch = helper[helper.index("def wayfire_main"):helper.index("def focused")]
    assert 'window-rules/list-views' in branch
    assert 'window-rules/configure-view' in branch
    assert 'window-rules/close-view' in branch
    assert 'wm-actions/set-minimized' in branch
    assert 'pc:move-native:' in branch
    assert 'if os.environ.get("WAYFIRE_SOCKET")' in helper


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wayfire_010_event_negotiation_keeps_multi_window_focus_live(tmp_path):
    script = tmp_path / "events.js"
    script.write_text(textwrap.dedent(f"""
      const net=require('net');const sock={json.dumps(str(tmp_path / 'wf.socket'))},calls=[];
      const views=[
        {{id:1,pid:10,'app-id':'place.poster.desktop',title:'PosterChan',mapped:true,'output-id':1}},
        {{id:2,pid:20,'app-id':'firefox',title:'Firefox',mapped:true,'output-id':1}},
        {{id:3,pid:30,'app-id':'foot',title:'Terminal',mapped:true,'output-id':1}},
        {{id:4,pid:40,'app-id':'telegramdesktop',title:'Telegram',mapped:true,'output-id':1}}];
      const frame=o=>{{const b=Buffer.from(JSON.stringify(o)),h=Buffer.alloc(4);h.writeUInt32LE(b.length);return Buffer.concat([h,b]);}};
      const server=net.createServer(c=>{{let b=Buffer.alloc(0);c.on('data',x=>{{b=Buffer.concat([b,x]);while(b.length>=4){{const n=b.readUInt32LE();if(b.length<4+n)return;const q=JSON.parse(b.subarray(4,4+n));b=b.subarray(4+n);calls.push(q);let r={{ok:true}};
        if(q.method==='window-rules/list-views')r=views;
        if(q.method==='window-rules/events/watch'&&q.data.events.includes('output-layout-changed'))r={{error:'Event not found: "output-layout-changed"'}};
        c.write(frame(r));}}}});}});
      server.listen(sock,async()=>{{const {{WayfireWM}}=require({json.dumps(str(ROOT / 'desktop/wm-wayfire.js'))});const w=new WayfireWM(sock);
        await w.subscribe();const before=await w.windows();await w.focus(2);await w.hide(2);await w.show(2);await w.focus(3);
        console.log(JSON.stringify({{before,calls,subscribed:w.subscribed}}));w.sock.destroy();w.actionServer.close();server.close();}});
    """), encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert len(result["before"]) == 4 and result["subscribed"] is True
    watches = [c for c in result["calls"] if c["method"] == "window-rules/events/watch"]
    assert len(watches) == 2
    assert "output-layout-changed" not in watches[-1]["data"]["events"]
    methods = [c["method"] for c in result["calls"]]
    assert methods[-4:] == ["window-rules/focus-view", "wm-actions/set-minimized",
                            "wm-actions/set-minimized", "window-rules/focus-view"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wayfire_frames_decode_fragmented_and_coalesced_messages():
    js = f"""
      const {{wfFrame,wfDecoder}}=require({json.dumps(str(ROOT / 'desktop/wm-wayfire.js'))});
      const out=[],d=wfDecoder(x=>out.push(x)),both=Buffer.concat([wfFrame({{ok:1}}),wfFrame({{event:'view-focused'}})]);
      d(both.subarray(0,2));d(both.subarray(2,9));d(both.subarray(9));console.log(JSON.stringify(out));
    """
    run = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [{"ok": 1}, {"event": "view-focused"}]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_snap_matrix_uses_the_window_monitor_and_reserves_the_taskbar():
    js = f"""
      const {{WayfireWM}}=require({json.dumps(str(ROOT / 'desktop/wm-wayfire.js'))});
      const w=new WayfireWM('/tmp/not-used'),placed=[];
      w.windows=async()=>[{{id:9,rect:{{x:2200,y:200,width:700,height:500}}}}];
      w.outputs=async()=>[{{rect:{{x:0,y:0,width:1920,height:1080}}}},{{rect:{{x:1920,y:0,width:2560,height:1440}}}}];
      w.place=async(id,x,y,width,height)=>{{placed.push({{id,x,y,width,height}});return true;}};
      (async()=>{{for(const z of ['left','right','top-left','top-right','bottom-left','bottom-right','max'])await w.snap(9,z);console.log(JSON.stringify(placed));}})();
    """
    run = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    rows = json.loads(run.stdout)
    assert rows == [
        {"id": 9, "x": 1920, "y": 0, "width": 1280, "height": 1368},
        {"id": 9, "x": 3200, "y": 0, "width": 1280, "height": 1368},
        {"id": 9, "x": 1920, "y": 0, "width": 1280, "height": 684},
        {"id": 9, "x": 3200, "y": 0, "width": 1280, "height": 684},
        {"id": 9, "x": 1920, "y": 684, "width": 1280, "height": 684},
        {"id": 9, "x": 3200, "y": 684, "width": 1280, "height": 684},
        {"id": 9, "x": 1920, "y": 0, "width": 2560, "height": 1368},
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wayfire_key_action_reaches_shell_once_without_sway_tick(tmp_path):
    js = f"""
      const cp=require('child_process'),fs=require('fs');process.env.XDG_RUNTIME_DIR={json.dumps(str(tmp_path))};
      const {{WayfireWM}}=require({json.dumps(str(ROOT / 'desktop/wm-wayfire.js'))}),w=new WayfireWM('/tmp/not-used');
      let count=0,payload='';w.on('tick',e=>{{count++;payload=e.payload;}});w._openActionSocket();
      const socket=process.env.XDG_RUNTIME_DIR+'/posterchan-action.sock';
      const wait=()=>fs.existsSync(socket)?Promise.resolve():new Promise(r=>setTimeout(()=>r(wait()),5));
      wait().then(()=>cp.execFile({json.dumps(str(ROOT / 'os/bin/pc-wayfire-action'))},['pc:start'],{{env:process.env}},e=>{{
        if(e)throw e;setTimeout(()=>{{console.log(JSON.stringify({{count,payload,mode:fs.statSync(socket).mode&0o777}}));w.actionServer.close();}},20);
      }}));
    """
    run = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"count": 1, "payload": "pc:start", "mode": 0o600}
