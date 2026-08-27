import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "desktop/native-preview.js"
OS = (ROOT / "static/js/client/os.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
MAIN = (ROOT / "desktop/main.js").read_text()


def node(script):
    return subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=10)


def test_capture_failure_is_an_empty_safe_fallback():
    run = node(f"""const p=require({str(PREVIEW)!r});p.capture({{x:1,y:2,width:30,height:40}},
      (_b,_a,_o,cb)=>cb(new Error('no grim'),Buffer.alloc(0))).then(x=>console.log(JSON.stringify(x)));""")
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == '""'


def test_capture_is_memory_only_png_and_uses_exact_geometry():
    run = node(f"""const p=require({str(PREVIEW)!r});let seen;p.capture({{x:-2,y:7,width:30,height:40}},
      (_b,a,o,cb)=>{{seen=[a,o.encoding];cb(null,Buffer.from([137,80,78,71,13,10,26,10]));}})
      .then(x=>console.log(JSON.stringify([seen,x])));""")
    assert run.returncode == 0, run.stderr
    assert '[-2,7 30x40' not in run.stdout
    assert '"-2,7 30x40"' in run.stdout
    assert 'data:image/png;base64,' in run.stdout


def test_preview_is_private_bounded_and_never_persistent():
    src = PREVIEW.read_text()
    assert "timeout:1500" in src and "maxBuffer:16*1024*1024" in src
    assert "writeFile" not in src and "mkdir" not in src
    handler = MAIN.split("ipcMain.handle('pc:wm:preview'", 1)[1].split("ipcMain.handle('pc:wm:close'", 1)[0]
    assert "target.stashed" in handler and "target.visible===false" in handler
    assert "if(overlap)return ''" in handler


def test_restore_discards_preview_and_failure_keeps_readable_body():
    sync = OS[OS.index("async function nsync()") : OS.index("/* THE MACHINE'S FILES")]
    assert "_nativePreview(it.w,preview)" in sync
    assert sync.count("_nativePreview(it.w,'')") >= 3
    close = OS[OS.index("function closeWin(") : OS.index("function minimise(")]
    assert "_nativePreview(w,'')" in close
    assert ".osw.native-stashed .osw-body::after" in CSS
    assert ".native-stash-preview .osw-body::after" in CSS
    assert "click to bring this window forward" in CSS
