"""A window capture is never asked for where the PNG cannot be written.

Found 2026-10-27 on the laptop: /tmp (a RAM tmpfs) was full, Alt+Tab's view-shot capture made Wayfire write a PNG
there, libpng failed the write and Wayfire ABORTED -- the compositor and every window with it ("laptop display was
in terminal"). view-shot writes inside the compositor, so the callers decide: no room, no request. Runs the shipped
desktop/wm-wayfire.js against a fake Wayfire socket and the shipped pc-wayfire-health with a fake IPC.
"""
import json
import os
import shutil
import subprocess
import textwrap
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _node_capture(tmp_path, min_free):
    sock = tmp_path / "wayfire.socket"
    script = tmp_path / "cap.js"
    script.write_text(textwrap.dedent(f"""
      const net=require('net'),fs=require('fs');
      const calls=[];
      const frame=o=>{{const b=Buffer.from(JSON.stringify(o)),h=Buffer.alloc(4);h.writeUInt32LE(b.length);return Buffer.concat([h,b]);}};
      const server=net.createServer(c=>{{let b=Buffer.alloc(0);c.on('data',x=>{{b=Buffer.concat([b,x]);
        while(b.length>=4){{const n=b.readUInt32LE();if(b.length<4+n)return;
          const q=JSON.parse(b.subarray(4,4+n));b=b.subarray(4+n);calls.push(q.method);
          if(q.method==='view-shot/capture')fs.writeFileSync(q.data.file,Buffer.from([137,80,78,71,13,10,26,10,0]));
          c.write(frame({{result:'ok'}}));}}}});}});
      server.listen({json.dumps(str(sock))},async()=>{{
        process.env.WAYFIRE_SOCKET={json.dumps(str(sock))};
        const {{WayfireWM}}=require({json.dumps(str(ROOT / 'desktop/wm-wayfire.js'))});
        const w=new WayfireWM();
        const shot=await w.captureView(7);
        console.log(JSON.stringify({{calls,shot:String(shot).slice(0,22)}}));
        try{{w.sock.destroy();}}catch(_){{}}server.close();process.exit(0);
      }});
    """))
    env = {**os.environ, "PC_CAPTURE_MIN_FREE": str(min_free), "TMPDIR": str(tmp_path), "NO_COLOR": "1"}
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=20, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_full_temp_dir_costs_the_preview_never_the_compositor(tmp_path):
    full = _node_capture(tmp_path, 10 ** 18)                 # more than any filesystem has free
    assert "view-shot/capture" not in full["calls"], "the compositor was asked to write into a full filesystem"
    assert full["shot"] == ""
    ok = _node_capture(tmp_path, 1)
    assert "view-shot/capture" in ok["calls"] and ok["shot"].startswith("data:image/png;base64,")


def _health():
    m = types.ModuleType("health_fixture")
    exec(compile((ROOT / "os/bin/pc-wayfire-health").read_text(), "pc-wayfire-health", "exec"), m.__dict__)
    return m


def test_the_health_check_does_not_ask_either(tmp_path, monkeypatch):
    m = _health()
    asked = []

    def request(method, data=None, timeout=None):
        asked.append(method)
        if method == "list-methods":
            return {"methods": ["view-shot/capture"]}
        Path(data["file"]).write_bytes(b"png")
        return {}

    monkeypatch.setattr(m, "request", request, raising=False)
    m.__dict__["request"] = request
    target = str(tmp_path / "shot.png")
    shell = {"id": 3, "pid": 99}
    monkeypatch.setenv("PC_CAPTURE_MIN_FREE", str(10 ** 18))
    m.owned_capture(shell, target)
    assert "view-shot/capture" not in asked, "the health check asked the compositor to write into a full filesystem"
    monkeypatch.setenv("PC_CAPTURE_MIN_FREE", "1")
    assert m.owned_capture(shell, target) is True and "view-shot/capture" in asked


def test_the_installed_copy_is_the_one_tested():
    """The package installs files/pc-wayfire-health; os/bin is the copy run above. One may not drift."""
    assert (ROOT / "os/bin/pc-wayfire-health").read_bytes() == \
        (ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-wayfire-health").read_bytes()
