"""POSTERCHANOS: OFFICE OPENS A DOCUMENT ON THE MACHINE, WITH NO NETWORK (desktop/office-local.js).

"for posterchanOS, let's make office work offline too … include the office sweet that loads only when
office documents are loaded". The shipped module, driven under node, against the REAL Collabora CODE
build the instance runs (officeserver/Collabora_Online.AppImage, unpacked once into a cache): nothing
runs until a document is opened; the WOPI host refuses a wrong token; the editor loads INSIDE a page of
the embedding origin in real Chrome and answers its save handshake; Save As PDF converts; closing the
last document lets CODE stop.
"""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
APPIMAGE = ROOT / "officeserver" / "Collabora_Online.AppImage"
CHROME = "/opt/google/chrome/chrome"


def _code_root():
    want = os.environ.get("PC_TEST_CODE_ROOT")
    if want:
        return Path(want)
    if not APPIMAGE.exists():
        return None
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "posterchan-test-code"
    tag = cache / (str(APPIMAGE.stat().st_size) + ".ok")
    root = cache / "squashfs-root"
    if not tag.exists():
        shutil.rmtree(cache, ignore_errors=True)
        cache.mkdir(parents=True)
        subprocess.run([str(APPIMAGE), "--appimage-extract"], cwd=cache, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
        tag.write_text("ok")
    return root


DRIVER = r"""
const { OfficeLocal } = require(process.argv[1]);
const rl = require('readline').createInterface({ input: process.stdin });
const o = new OfficeLocal({ root: process.argv[2], origin: process.argv[3], idleMs: 1500, workDir: process.argv[4] });
rl.on('line', async line => {
  const j = JSON.parse(line); let out;
  try {
    if (j.op === 'open') out = await o.open(Buffer.from(j.b64, 'base64'), j.name, 'edit');
    else if (j.op === 'contents') out = { b64: o.contents(j.id, j.token).toString('base64') };
    else if (j.op === 'export') { const r = await o.export(j.id, j.token, j.fmt); out = { mime: r.mime, b64: r.bytes.toString('base64') }; }
    else if (j.op === 'close') out = { closed: o.close(j.id, j.token) };
    else if (j.op === 'running') out = { running: o.running(), wopi: o.wopi && o.wopi.port };
    else if (j.op === 'stop') { o.stop(); out = {}; }
    out = Object.assign({ ok: true }, out);
  } catch (e) { out = { ok: false, error: String(e && e.message || e) }; }
  process.stdout.write(JSON.stringify(out) + '\n');
});
"""


class Driver:
    def __init__(self, root, origin, work):
        self.p = subprocess.Popen([NODE, "-e", DRIVER, str(ROOT / "desktop/office-local.js"), str(root), origin, work],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def __call__(self, **job):
        self.p.stdin.write(json.dumps(job) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self):
        try:
            self(op="stop")
        except Exception:
            pass
        self.p.kill()


def _blank_odt():
    from app.routers.office import blank_document
    return blank_document("text")[0]


@pytest.mark.skipif(NODE is None or not (os.environ.get("PC_TEST_CODE_ROOT") or APPIMAGE.exists()) or not Path(CHROME).exists(),
                    reason="needs node, Chrome and the CODE AppImage")
def test_a_document_opens_saves_and_converts_on_the_machine_and_code_stops_after():
    import base64

    page = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = page["html"].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    host = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=host.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{host.server_port}"
    work = tempfile.mkdtemp(prefix="pc-office-local-")
    d = Driver(_code_root(), origin, work)
    try:
        assert d(op="running")["running"] is False, "CODE was running before any document was opened"
        s = d(op="open", b64=base64.b64encode(_blank_odt()).decode(), name="letter.odt")
        assert s["ok"], s
        assert s["editor_url"].startswith("http://127.0.0.1:") and "WOPISrc=" in s["editor_url"], s
        wopi = d(op="running")["wopi"]

        # The WOPI host answers the session's token and nobody else's.
        base = f"http://127.0.0.1:{wopi}/wopi/files/{s['id']}"
        info = httpx.get(base, params={"access_token": s["token"]}).json()
        assert info["BaseFileName"] == "letter.odt" and info["UserCanWrite"] and info["PostMessageOrigin"] == origin
        assert httpx.get(base, params={"access_token": "x" * len(s["token"])}).status_code == 401
        assert httpx.get(base + "/contents", params={"access_token": "nope"}).status_code == 401

        # Real Chrome, a page of the embedding origin, the editor in an iframe: it must load the
        # document and answer the save handshake (the path Save / Save As / PDF all take).
        page["html"] = """<!doctype html><body><iframe name=ed style="width:1200px;height:800px"></iframe>
          <form id=f method=post target=ed action="%s"><input type=hidden name=access_token value="%s"></form>
          <script>window.__msgs=[];addEventListener('message',e=>{let d=e.data;try{d=JSON.parse(d)}catch(_){}
            __msgs.push(d&&d.MessageId||'');
            if(d&&d.MessageId==='App_LoadingStatus'&&d.Values&&d.Values.Status==='Document_Loaded'){
              const w=document.querySelector('iframe').contentWindow;
              w.postMessage(JSON.stringify({MessageId:'Host_PostmessageReady',SendTime:Date.now(),Values:{}}),'*');
              w.postMessage(JSON.stringify({MessageId:'Action_Save',SendTime:Date.now(),Values:{Notify:true,DontTerminateEdit:true,DontSaveIfUnmodified:false}}),'*');}});
          document.getElementById('f').submit();</script>""" % (s["editor_url"], s["token"])
        msgs = asyncio.run(_load_in_chrome(origin))
        assert "App_LoadingStatus" in msgs, ("the editor never spoke to the page", msgs[-20:])
        assert "Action_Save_Resp" in msgs, ("the editor did not answer Save", msgs[-20:])

        pdf = d(op="export", id=s["id"], token=s["token"], fmt="pdf")
        assert pdf["ok"] and base64.b64decode(pdf["b64"])[:5] == b"%PDF-", pdf.get("error")
        assert d(op="contents", id=s["id"], token=s["token"])["ok"]

        assert d(op="close", id=s["id"], token=s["token"])["closed"] is True
        assert d(op="contents", id=s["id"], token=s["token"])["ok"] is False
        for _ in range(60):
            if not d(op="running")["running"]:
                break
            import time
            time.sleep(.5)
        assert d(op="running")["running"] is False, "CODE kept running after the last document closed"
    finally:
        d.close()
        host.shutdown()
        host.server_close()
        shutil.rmtree(work, ignore_errors=True)


async def _load_in_chrome(origin):
    from tests.client.test_effects_full_app import Browser
    with tempfile.TemporaryDirectory(prefix="pc-office-chrome-") as profile:
        proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0",
                                 "--user-data-dir=" + profile, "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(150):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient(trust_env=False) as h:
                pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
            url = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(url, max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call("Page.enable")
                await b.call("Page.navigate", {"url": origin + "/"})
                msgs = []
                for _ in range(240):
                    await asyncio.sleep(.5)
                    msgs = await b.js("window.__msgs||[]") or []
                    if "Action_Save_Resp" in msgs:
                        break
                return msgs
        finally:
            proc.kill()
