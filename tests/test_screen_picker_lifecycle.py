import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "desktop" / "main.js"
NODE = shutil.which("node") or shutil.which("nodejs")


def _function(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for pos in range(brace, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start:pos + 1]
    raise AssertionError("unterminated function")


@pytest.mark.skipif(not NODE, reason="needs node")
def test_failed_picker_renderer_settles_cancel_and_allows_a_retry():
    """Run the shipped picker function with Electron-shaped event emitters.

    The first local-page load rejects. It must resolve as Cancel and clear the singleton guard;
    a renderer crash must do the same, and an immediately following call must still be closable.
    """
    function = _function(MAIN.read_text(encoding="utf-8"), "function pickScreenSource()")
    script = r"""
const {EventEmitter}=require('events');
let pendingSources=[],pickerOpen=false,created=0,loads=0;
const logs=[];const screenLog=x=>logs.push(String(x));
const path={join:(...x)=>x.join('/')};const __dirname='/desktop';const win=null;
const desktopCapturer={getSources:async()=>[{id:'a',name:'A'},{id:'b',name:'B'}]};
const ipcMain=new EventEmitter();ipcMain.removeAllListeners=EventEmitter.prototype.removeAllListeners;
class BrowserWindow extends EventEmitter{
  constructor(){super();created++;this.dead=false;this.webContents=new EventEmitter();}
  isDestroyed(){return this.dead;}
  show(){}
  close(){if(this.dead)return;this.dead=true;this.emit('closed');}
  loadFile(){loads++;return loads===1?Promise.reject(new Error('missing picker page')):Promise.resolve();}
}
const first=await pickScreenSource();
const secondPromise=pickScreenSource();
await new Promise(r=>setImmediate(r));
const second=[...BrowserWindowInstances];
"""
    # Track instances without altering the production function or relying on timing-sensitive GC.
    script = script.replace(
        "constructor(){super();created++;this.dead=false;this.webContents=new EventEmitter();}",
        "constructor(){super();created++;BrowserWindowInstances.push(this);this.dead=false;this.webContents=new EventEmitter();}"
    ).replace(
        "class BrowserWindow extends EventEmitter{",
        "const BrowserWindowInstances=[];class BrowserWindow extends EventEmitter{"
    ).replace(
        "const second=[...BrowserWindowInstances];",
        "BrowserWindowInstances[1].webContents.emit('render-process-gone',{}, {reason:'oom'});"
        "const second=await secondPromise;const thirdPromise=pickScreenSource();"
        "await new Promise(r=>setImmediate(r));BrowserWindowInstances[2].close();"
        "const third=await thirdPromise;"
        "process.stdout.write(JSON.stringify({first,second,third,created,pickerOpen,logs}));"
    )
    wrapped = "(async()=>{\n" + script.replace(
        "const first=await pickScreenSource();", function + "\nconst first=await pickScreenSource();"
    ) + "\n})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([NODE, "-e", wrapped], cwd=ROOT, text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["first"] is None and out["second"] is None and out["third"] is None
    assert out["created"] == 3, "a failed picker left the singleton guard latched"
    assert out["pickerOpen"] is False
    assert any("failed to load" in line for line in out["logs"])
    assert any("renderer stopped: oom" in line for line in out["logs"])
