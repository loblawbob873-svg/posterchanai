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


def test_wayfire_backend_is_theme_neutral_and_sway_is_rollback_default():
    src = (ROOT / "desktop/wm-wayfire.js").read_text()
    factory = (ROOT / "desktop/wm.js").read_text()
    assert "applyChrome(){return Promise.resolve(true);}" in src
    assert "macOS and Windows chrome" in src
    assert "process.env.WAYFIRE_SOCKET" in factory
    assert "POSTERCHAN_WM_FORCE_SWAY" in factory
    assert "return new SwayWM(sockPath)" in factory


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
