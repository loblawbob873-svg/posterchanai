import json
import os
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_node(source):
    env = dict(os.environ, WAYLAND_DISPLAY="wayland-test")
    out = subprocess.check_output(["node", "-e", source], cwd=ROOT, env=env, text=True)
    return json.loads(out)


def test_wayland_write_preserves_mime_and_pipes():
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
    assert result["call"]["args"][-3:] == ["wl-copy", "--type", "text/plain"]
    assert result["call"]["opts"]["stdio"] == ["pipe", "ignore", "ignore"]


def test_wayland_read_is_bounded_and_uses_plain_text_offer():
    result = run_node(r"""
const C=require('./desktop/clipboard.js');
let call;
C.readWaylandText({execFile:(bin,args,opts,cb)=>{call={bin,args,opts}; cb(null,'outside-app');}})
 .then(value=>console.log(JSON.stringify({value,call})));
""")
    assert result["value"] == "outside-app"
    assert result["call"]["args"] == ["--no-newline", "--type", "text"]


def test_wayland_write_turns_a_broken_stdin_pipe_into_a_failed_copy():
    result = run_node(r"""
const C=require('./desktop/clipboard.js');
let stdinError;
const child={
  stdin:{once(name,fn){if(name==='error')stdinError=fn;},end(){queueMicrotask(()=>stdinError(new Error('EPIPE')));}},
  once(name,fn){this[name]=fn;}, kill(){},
};
C.writeWaylandText('native-copy', {spawn:()=>child})
 .then(ok=>console.log(JSON.stringify({ok})));
""")
    assert result == {"ok": False}


def test_main_bridge_uses_native_wayland_clipboard_both_directions():
    src = (ROOT / "desktop/main.js").read_text()
    write = src[src.index("ipcMain.handle('pc:clip:write'"):src.index("/* CLIPBOARD READ")]
    read = src[src.index("ipcMain.handle('pc:clip:read'"):src.index("// Screen picker")]
    assert "writeWaylandText(s)" in write
    assert "readWaylandText()" in read
    assert "async" in write and "async" in read


def test_wayland_image_write_offers_png_and_pipes_the_exact_bytes():
    """Copy image had NO working path on this desktop: navigator.clipboard.write is refused on the
    app:// origin and the native bridge was text-only. It rides the same wl-copy invocation, because
    Electron's clipboard.writeImage fills Chromium's cache and never takes the compositor selection
    -- the copy would work inside PosterChan and paste nothing into Firefox or Telegram."""
    result = run_node(r"""
const C=require('./desktop/clipboard.js');
let call, input=null;
const child={
  stdin:{end(v){input=Array.from(v); queueMicrotask(()=>child.exit(0));}},
  once(name,fn){this[name]=fn;}, kill(){},
};
const png=Buffer.from([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a,1,2,3]);
C.writeWaylandImage(png, {spawn:(bin,args,opts)=>{call={bin,args,opts}; return child;}})
 .then(ok=>console.log(JSON.stringify({ok,input,call})));
""")
    assert result["ok"] is True
    assert result["input"] == [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]
    assert result["call"]["args"][-3:] == ["wl-copy", "--type", "image/png"]
    # The same descriptor-closing launcher as the text path, not a second spawn of its own.
    assert result["call"]["bin"] == "/bin/bash"
    assert "exec $__n>&-" in result["call"]["args"][1]
    assert result["call"]["opts"]["stdio"] == ["pipe", "ignore", "ignore"]


def test_wayland_image_write_refuses_empty_bytes_instead_of_taking_the_selection():
    """wl-copy with nothing on stdin still OWNS the selection, and then offers image/png over no
    bytes: a copy that reports success and pastes an empty image, which is worse than one that
    admits it failed."""
    result = run_node(r"""
const C=require('./desktop/clipboard.js');
let spawned=false;
const child={stdin:{end(){queueMicrotask(()=>child.exit(0));}},once(name,fn){this[name]=fn;},kill(){}};
C.writeWaylandImage(Buffer.alloc(0), {spawn:()=>{spawned=true; return child;}})
 .then(ok=>console.log(JSON.stringify({ok,spawned})));
""")
    assert result == {"ok": False, "spawned": False}


def test_main_bridge_publishes_an_image_through_the_compositor_not_through_a_read_back():
    src = (ROOT / "desktop/main.js").read_text()
    handler = src[src.index("ipcMain.handle('pc:clip:write-image'"):src.index("/* CLIPBOARD READ")]
    assert "fromOurPage(e)" in handler, "the image write must be gated like every other clip route"
    assert "writeWaylandImage(png)" in handler
    # clipboard.readImage() hands back Chromium's own cached write whether or not the compositor ever
    # took the selection, so it can never be the proof that a copy landed.
    assert "readImage" not in handler
    assert "0x89504e47" in handler, "an unchecked buffer would advertise image/png over anything"


def test_preload_exposes_the_image_write_and_only_the_write():
    src = (ROOT / "desktop/preload.js").read_text()
    bridge = src[src.index("exposeInMainWorld('pcClip'"):]
    bridge = bridge[:bridge.index("\n});")]
    assert "pc:clip:write-image" in bridge
    assert "readImage" not in bridge and "pc:clip:read-image" not in bridge
