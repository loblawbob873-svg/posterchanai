"""A background post says "uploading…" WHILE it uploads, not after.

Reported 2026-10-09: "why is background taking forever to render". The card is drawn in
milliseconds; the wait was the upload (the server answered in ~1s once it arrived), but every caller
set "uploading…" only after buildBgPost returned -- after the upload -- so the whole wait sat under
"rendering…". Runs the SHIPPED buildBgPost with a slow fake upload and records what the status said
at the moment the upload started, and that all three composers pass the stage callback.
"""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_status_reads_uploading_while_the_upload_runs():
    js = r"""
const fs=require('fs'),vm=require('vm');const code=fs.readFileSync('static/js/client/compose.js','utf8');
const i=code.indexOf('  async function buildBgPost(');const j=code.indexOf('\n  }\n',i)+4;
let status='rendering…',seenAtUpload=null;
vm.runInThisContext('var _BG_WORDS=t=>t,_cardHook=w=>w,renderBgPost=async()=>new Blob(["x"]),'+
  'uploadBlob=async()=>{seenAtUpload=globalThis.status;await new Promise(r=>setTimeout(r,20));return "https://x/a.jpg"};'+code.slice(i,j));
globalThis.status=status;
(async()=>{const out=await buildBgPost('hello there',{},false,()=>{globalThis.status='uploading…';});
  process.stdout.write(JSON.stringify({seenAtUpload,url:out.url}));})();
""".replace("seenAtUpload=globalThis.status", "globalThis.seen=globalThis.status").replace(
        "JSON.stringify({seenAtUpload,url:out.url})", "JSON.stringify({seen:globalThis.seen,url:out.url})")
    out = json.loads(subprocess.run(["node", "-e", js], cwd=ROOT, capture_output=True, text=True,
                                    timeout=30, check=True).stdout)
    assert out["url"] == "https://x/a.jpg"
    assert out["seen"] == "uploading…", out


def test_every_composer_passes_the_stage_callback():
    src = (ROOT / "static/js/client/compose.js").read_text() + (ROOT / "static/js/client/timeline.js").read_text()
    calls = [l for l in src.splitlines() if "await buildBgPost(" in l]
    assert len(calls) == 3, calls
    assert all("uploading…" in l for l in calls), calls
