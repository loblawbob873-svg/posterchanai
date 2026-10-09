"""PosterChan app windows open on the SHELL document, and an app route reloads as the shell.

Run: venv-unified/bin/python -m pytest tests/test_desktop_windows_open_on_the_shell.py

Reported: "my laptop hit this bug when opening up new posterchan apps: 'not found' in a black
screen. desktop had this problem yesterday" → "all the posterchan apps are not functioning on my
laptop now". The desktop page moves its own URL to what it shows (`/npub1…`, `/naddr1…`, `/r/…`), a
new window was opened at `location.pathname + ?pcwin=…`, and the bundle's app:// handler looks for
a FILE by that name — so after viewing a profile, a post or a repo, EVERY app window came up black
with "not found". The same handler answered a reload on such a path the same way.

Both halves run the shipped code: oswin.js `open()` under node with a desktop page parked on a
deep path, and the app:// handler lifted out of desktop/main.js against a temporary bundle.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node required")


def _node(js, *args):
    r = subprocess.run([NODE, "-e", js, *map(str, args)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def test_a_window_opened_from_a_deep_path_loads_the_shell():
    out = _node(r"""
const fs=require('fs'); const opened=[];
const root={ location:{protocol:'app:', pathname:'/npub1qqqqexample', search:'', href:'app://posterchan/npub1qqqqexample'},
  pcWM:{}, PCOSShell:{available:()=>true}, open:(u,t,f)=>{opened.push(u); return {};}, screen:{availWidth:1920, availHeight:1080},
  localStorage:{getItem(){return null}}, document:{querySelector:()=>({})}, addEventListener(){} };
globalThis.self=root;
const code=fs.readFileSync(process.argv[1],'utf8');
new Function('globalThis', code.replace(/typeof globalThis !== 'undefined' \? globalThis : this/, 'root'))
  .call(root, root);
const API = root.PCOSWin;
API.open('global','Social',{width:900,height:700});
API.open('notes','Notes',{width:900,height:700});
process.stdout.write(JSON.stringify({opened, shell:API.shellPath()}));
""", ROOT / "static/js/client/oswin.js")
    assert out["shell"] == "/index.html"
    assert out["opened"] and all(u.startswith("/index.html?pcwin=") for u in out["opened"]), out["opened"]
    assert not any("npub1" in u for u in out["opened"]), "a window was opened at the desktop's deep path"


HANDLER = r"""
const fs=require('fs'), path=require('path');
const src=fs.readFileSync(process.argv[1],'utf8');
const i=src.indexOf('function serveBundle() {'), j=src.indexOf('\n}\n', i)+3;
let handler=null;
const protocol={handle:(scheme, fn)=>{handler=fn;}};
const WWW=process.argv[2];
const _MIME={'.html':'text/html','.js':'text/javascript','.png':'image/png'};
function serveHostFile(){ return new Response('host'); }
// Meme Builder media kept on the machine (desktop/meme-local.js): the real module, an empty store.
const memeLocal=require(path.join(path.dirname(process.argv[1]),'meme-local.js'));
const memeStoreDir=()=>path.join(WWW,'..','no-meme-store');
eval(src.slice(i,j)); serveBundle();
(async()=>{
  const ask=async u=>{ const r=await handler({url:u, headers:new Map()}); return {status:r.status, body:(await r.text()).slice(0,40), type:r.headers.get('content-type')}; };
  process.stdout.write(JSON.stringify({
    root: await ask('app://posterchan/'),
    route: await ask('app://posterchan/npub1qqqqexample?pcwin=global'),
    repo: await ask('app://posterchan/r/npub1abc/myrepo'),
    js: await ask('app://posterchan/static/js/client/app.js'),
    missingJs: await ask('app://posterchan/static/js/client/nope.js'),
    missingPng: await ask('app://posterchan/icon-missing.png'),
    escape: await ask('app://posterchan/../../etc/passwd'),
  }));
})();
"""


def test_an_app_route_is_served_the_shell_and_a_missing_file_is_still_404(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>shell</title>")
    (tmp_path / "static/js/client").mkdir(parents=True)
    (tmp_path / "static/js/client/app.js").write_text("// app")
    out = _node(HANDLER, ROOT / "desktop/main.js", tmp_path)
    assert out["root"]["status"] == 200 and "shell" in out["root"]["body"]
    assert out["route"]["status"] == 200 and "shell" in out["route"]["body"], out["route"]
    assert out["repo"]["status"] == 200 and "shell" in out["repo"]["body"], out["repo"]
    assert out["js"]["status"] == 200 and out["js"]["body"] == "// app"
    assert out["missingJs"]["status"] == 404, "a missing script must not be masked by the shell"
    assert out["missingPng"]["status"] == 404
    # `/../../etc/passwd` is normalised by the URL parser to `/etc/passwd`, an app route: the SHELL
    # answers, never a file outside the bundle.
    assert "root:" not in out["escape"]["body"] and (out["escape"]["status"] in (403, 404) or "shell" in out["escape"]["body"])
