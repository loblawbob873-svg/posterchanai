import json
import os
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_node(source):
    env = dict(os.environ, WAYLAND_DISPLAY="wayland-test")
    out = subprocess.check_output(["node", "-e", source], cwd=ROOT, env=env, text=True)
    return json.loads(out)


def test_wayland_write_is_bounded_and_does_not_inherit_desktop_descriptors():
    result = run_node(r"""
const C=require('./desktop/clipboard.js');
let call, input='';
const child={
  stdin:{end(v){input=v; queueMicrotask(()=>child.exit(0));}},
  once(name,fn){this[name]=fn;}, kill(){},
};
C.writeWaylandText('native-copy', {spawn:(bin,args,opts)=>{call={bin,args,opts}; return child;}})
 .then(ok=>console.log(JSON.stringify({ok,input,call})));
""")
    assert result["ok"] is True
    assert result["input"] == "native-copy"
    assert result["call"]["args"] == ["--type", "text/plain;charset=utf-8"]
    assert result["call"]["opts"]["stdio"] == ["pipe", "ignore", "ignore"]


def test_wayland_read_is_bounded_and_uses_plain_text_offer():
    result = run_node(r"""
const C=require('./desktop/clipboard.js');
C.readWaylandText({execFile:(bin,args,opts,cb)=>cb(null,'outside-app')})
 .then(value=>console.log(JSON.stringify({value})));
""")
    assert result["value"] == "outside-app"


def test_main_bridge_uses_native_wayland_clipboard_both_directions():
    src = (ROOT / "desktop/main.js").read_text()
    write = src[src.index("ipcMain.handle('pc:clip:write'"):src.index("/* CLIPBOARD READ")]
    read = src[src.index("ipcMain.handle('pc:clip:read'"):src.index("// Screen picker")]
    assert "writeWaylandText(s)" in write
    assert "readWaylandText()" in read
    assert "async" in write and "async" in read
