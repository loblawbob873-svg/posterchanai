"""A real Concord call through OUR broker and OUR LiveKit: two browsers press Join and hear each other.

Asked 2026-10-22: "Why is the call server armada.buzz? ... do all the buttons in there work?" -- and the honest
answer was that no Concord call had ever been placed end to end (docs/testing/concord-compatibility.md: "No live
cross-client broker/SFU call has been validated"). This places one. Nothing that carries the call is a stub:

  * the REAL livekit-server binary (livekit/livekit-server, ./install.sh --livekit), started with the config
    livekit_service.render_config generates -- one UDP port, TCP fallback, signalling on loopback;
  * the REAL broker (app/routers/concord_av.py) behind HTTPS (the client refuses any other), carrying the
    signalling WebSocket at /livekit to the SFU on loopback exactly as it does on a node;
  * the SHIPPED call panel (static/js/client/cord-call.js + cord-voice.js + the vendored LiveKit SDK and its E2EE
    worker), each participant its own headless Chrome with Chrome's fake microphone.

What is a fixture is only what Concord's community layer would hand the panel: the channel's room key and media
root (both participants hold the same ones -- that is what membership means here) and a presence bus standing in
for the relay. The panel attaches a remote track ONLY after that person's signed "joined" presence names the same
identity the SFU reports, and decrypts it ONLY with the sender key both sides derive from the media root -- so an
<audio> element that carries SOUND is the end-to-end proof: broker grant, SFU routing, presence attribution and
E2EE all worked. A status line saying "Connected" would prove none of that.
"""


import asyncio
import contextlib
import datetime
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import websockets

ROOT = Path(__file__).resolve().parents[2]
CHROME = "/opt/google/chrome/chrome"
LK_BIN = ROOT / "livekit" / "livekit-server"
pytestmark = [pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required"),
              pytest.mark.skipif(not LK_BIN.exists(), reason="livekit-server not installed (./install.sh --livekit)")]


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def self_signed(tmp: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import ipaddress
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    (tmp / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    return str(tmp / "cert.pem"), str(tmp / "key.pem")


PAGE = """<!doctype html><meta charset=utf-8><title>call</title><body>
<script src="/static/vendor/nostr/nostr.bundle.js"></script>
<script src="/static/js/client/cord-voice.js"></script>
<script src="/static/js/client/cord-call.js"></script>
<script>
window.__start=async(cfg)=>{
  const NT=window.NostrTools,hex=s=>Uint8Array.from(s.match(/../g),h=>parseInt(h,16));
  const me=NT.getPublicKey(NT.generateSecretKey()),room=NT.getPublicKey(hex(cfg.roomSk));
  window.__me=me;let seen=0;
  const ctx={name:'voice',current:()=>true,joinOnOpen:true,listened:()=>30000,seed:()=>[],
    preferred:cfg.broker||location.origin,remember:()=>{},label:pk=>pk===me?'me':'peer:'+pk.slice(0,8),
    material:()=>({room,signingKey:hex(cfg.roomSk),mediaRoot:hex(cfg.mediaRoot),epoch:'1',channelId:'c1',stream:'s'}),
    presence:async(verb,identity,broker)=>{await fetch('/bus',{method:'POST',body:JSON.stringify({pubkey:me,
      id:Math.random().toString(16).slice(2),at:Date.now(),content:verb,tags:verb==='joined'?[['identity',identity],['broker',broker]]:[]})});},
    subscribe:onEvent=>{const t=setInterval(async()=>{try{const r=await (await fetch('/bus?since='+seen)).json();
      for(const e of r.events){seen++;onEvent(e);}}catch(_){}} ,400);return ()=>clearInterval(t);},
    decode:async wrap=>[wrap]};
  window.__statuses=[];new MutationObserver(()=>{const st=document.querySelector('.pc-cord-call [role=status]');
    if(st&&window.__statuses[window.__statuses.length-1]!==st.textContent)window.__statuses.push(st.textContent);})
    .observe(document.body,{subtree:true,childList:true,characterData:true});
  window.__errors=[];addEventListener('error',e=>__errors.push(String(e.message)));addEventListener('unhandledrejection',e=>__errors.push(String(e.reason&&e.reason.message||e.reason)));
  await PCCordCall.open(ctx);return true;
};
// What a person would HEAR: the loudest moment across `ms` of every attached remote <audio>.
window.__loudness=async(ms)=>{const els=[...document.querySelectorAll('.pc-cord-call [data-call=media] audio')];
  if(!els.length)return -1;const ac=new AudioContext();await ac.resume();let peak=0;
  const an=els.map(el=>{const s=ac.createMediaStreamSource(el.srcObject);const a=ac.createAnalyser();a.fftSize=2048;s.connect(a);return a;});
  const end=Date.now()+ms,buf=new Float32Array(2048);
  while(Date.now()<end){for(const a of an){a.getFloatTimeDomainData(buf);let s=0;for(const v of buf)s+=v*v;peak=Math.max(peak,Math.sqrt(s/buf.length));}
    await new Promise(r=>setTimeout(r,50));}
  return peak;};
</script>"""


def make_app(lk_port: int, api_key: str, api_secret: str, grants: list):
    from fastapi import FastAPI, Request, WebSocket
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from app.routers import concord_av
    from app.services import livekit_service

    livekit_service.available = lambda: True
    livekit_service.credentials = lambda cfg=None: (api_key, api_secret)
    livekit_service._cfg = lambda: {"livekit_http_port": str(lk_port)}   # the SHIPPED /livekit proxy reads this
    concord_av._nonces.clear(); concord_av._hits.clear()
    real_check = concord_av.check_grant
    def counted(*a, **k):
        why = real_check(*a, **k); grants.append(why); return why
    concord_av.check_grant = counted

    app = FastAPI()
    app.include_router(concord_av.router)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    bus: list = []

    @app.get("/call")
    async def page():
        return HTMLResponse(PAGE)

    @app.post("/bus")
    async def post(request: Request):
        bus.append(json.loads(await request.body())); return JSONResponse({"ok": True})

    @app.get("/bus")
    async def read(since: int = 0):
        return JSONResponse({"events": bus[since:]})

    return app


class Tab:
    def __init__(self, ws): self.ws, self.n = ws, 0
    async def call(self, method, params=None):
        self.n += 1; await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            m = json.loads(await asyncio.wait_for(self.ws.recv(), 60))
            if m.get("id") == self.n:
                assert "error" not in m, m; return m.get("result", {})
    async def js(self, expr):
        r = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        assert "exceptionDetails" not in r, r; return r["result"].get("value")
    async def until(self, expr, timeout=40, what=""):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if await self.js(expr): return
            await asyncio.sleep(.25)
        status = await self.js("JSON.stringify({seen:(window.__statuses||[]).filter(x=>!/^Listening/.test(x)).slice(-8),errors:(window.__errors||[]).slice(-5)})")
        raise AssertionError(f"timed out waiting for {what or expr}; call status: {status!r}")


async def open_tab(stack, url, name):
    profile = stack.enter_context(tempfile.TemporaryDirectory(prefix="pc-lk-", ignore_cleanup_errors=True))
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--ignore-certificate-errors",
                             "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                             "--autoplay-policy=no-user-gesture-required",
                             "--disable-features=WebRtcHideLocalIpsWithMdns",
                             "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stack.callback(lambda: (proc.poll() is None) and (proc.kill(), proc.wait(timeout=10)))
    port_file = Path(profile, "DevToolsActivePort")
    for _ in range(300):
        with contextlib.suppress(FileNotFoundError, IndexError):
            port = port_file.read_text().splitlines()[0]
            if port.isdigit(): break
        await asyncio.sleep(.1)
    else:
        raise AssertionError(f"{name}: Chrome did not start")
    async with httpx.AsyncClient(trust_env=False) as h:
        for _ in range(100):
            with contextlib.suppress(httpx.HTTPError, ValueError):
                target = next((p["webSocketDebuggerUrl"] for p in (await h.get(f"http://127.0.0.1:{port}/json")).json()
                               if p.get("type") == "page"), None)
                if target: break
            await asyncio.sleep(.1)
    ws = await websockets.connect(target, max_size=20_000_000)
    stack.push_async_callback(ws.close)
    t = Tab(ws)
    await t.call("Page.enable")
    await t.call("Page.navigate", {"url": url})
    await t.until("typeof window.__start==='function' && !!window.PCCordCall", what=f"{name}: the call page")
    return t


def test_two_people_hear_each_other_through_our_broker_and_livekit(tmp_path):
    from app.services import livekit_service
    lk_http, lk_tcp, lk_udp, https_port = free_port(), free_port(), free_port(socket.SOCK_DGRAM), free_port()
    api_key, api_secret = "PCtest", "t" * 48
    cfg = {"livekit_http_port": str(lk_http), "livekit_tcp_port": str(lk_tcp), "livekit_udp_port": str(lk_udp),
           "turn_public_ip": "127.0.0.1"}
    conf = tmp_path / "lk.yaml"
    conf.write_text(livekit_service.render_config(cfg, api_key, api_secret))
    lk = subprocess.Popen([str(LK_BIN), "--config", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    grants: list = []
    try:
        for _ in range(100):
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"http://127.0.0.1:{lk_http}/", timeout=1).status_code == 200: break
            time.sleep(.1)
        else:
            raise AssertionError("livekit-server did not start: " + (lk.stderr.read(2000) or b"").decode(errors="replace"))

        import uvicorn
        cert, key = self_signed(tmp_path)
        server = uvicorn.Server(uvicorn.Config(make_app(lk_http, api_key, api_secret, grants), host="127.0.0.1",
                                               port=https_port, ssl_certfile=cert, ssl_keyfile=key, log_level="warning"))
        th = threading.Thread(target=server.run, daemon=True); th.start()
        for _ in range(100):
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"https://127.0.0.1:{https_port}/.well-known/concord/av", verify=False, timeout=1).status_code == 204: break
            time.sleep(.1)
        else:
            raise AssertionError("the broker did not come up")

        room_sk, media_root = os.urandom(32).hex(), os.urandom(32).hex()

        async def run():
            async with contextlib.AsyncExitStack() as stack:
                url = f"https://127.0.0.1:{https_port}/call"
                alice = await open_tab(stack, url, "alice")
                bob = await open_tab(stack, url, "bob")
                for t in (alice, bob):
                    await t.js("__start(%s)" % json.dumps({"roomSk": room_sk, "mediaRoot": media_root}))
                for name, t in (("alice", alice), ("bob", bob)):
                    await t.until("((document.querySelector('.pc-cord-call [role=status]')||{}).textContent||'').includes('end-to-end encrypted')",
                                  what=f"{name} to connect")
                for name, t in (("alice", alice), ("bob", bob)):
                    await t.until("document.querySelectorAll('.pc-cord-call [data-call=media] audio').length>=1",
                                  timeout=45, what=f"{name} to attach the other's verified, decrypted audio")
                    people = await t.js("[...document.querySelectorAll('.pc-cord-call [data-call=people] p')].map(p=>p.textContent)")
                    assert people and all(p.startswith("peer:") for p in people), (name, people)
                for name, t in (("alice", alice), ("bob", bob)):
                    loud = await t.js("__loudness(4000)")
                    assert loud > 0.01, f"{name} hears nothing from the other side (peak RMS {loud})"
                # Leave: the panel's own ✕, and the other side sees the track go.
                await alice.js("document.querySelector('.pc-cord-call [data-call=close]').click()")
                await bob.until("document.querySelectorAll('.pc-cord-call [data-call=media] audio').length===0",
                                timeout=30, what="bob to drop alice's audio after she left")
        asyncio.run(run())
        assert grants.count("") >= 2, ("each participant was granted by OUR broker", grants)
        server.should_exit = True
        th.join(timeout=10)
    finally:
        lk.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired): lk.wait(timeout=5)
        if lk.poll() is None: lk.kill()


def test_a_call_that_fails_keeps_saying_why(tmp_path):
    """The panel repaints its status line every second, and it used to repaint a FAILURE as "Ready to join" one
    second later -- so a call that could not connect looked like a call nobody had tried, with no reason shown.
    Found while getting the test above to pass: its first failure was invisible in the panel itself."""
    import uvicorn
    https_port = free_port()
    cert, key = self_signed(tmp_path)
    server = uvicorn.Server(uvicorn.Config(make_app(free_port(), "PCtest", "t" * 48, []), host="127.0.0.1",
                                           port=https_port, ssl_certfile=cert, ssl_keyfile=key, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    try:
        for _ in range(100):
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"https://127.0.0.1:{https_port}/call", verify=False, timeout=1).status_code == 200: break
            time.sleep(.1)

        async def run():
            async with contextlib.AsyncExitStack() as stack:
                t = await open_tab(stack, f"https://127.0.0.1:{https_port}/call", "caller")
                # A broker address where nothing listens: the join must fail, and must SAY so.
                await t.js("__start(%s)" % json.dumps({"roomSk": os.urandom(32).hex(), "mediaRoot": os.urandom(32).hex(),
                                                      "broker": f"https://127.0.0.1:{free_port()}"}))
                await t.until("((document.querySelector('.pc-cord-call [role=status]')||{}).textContent||'').includes('try again')",
                              what="the failure to be shown")
                await asyncio.sleep(3)
                line = await t.js("document.querySelector('.pc-cord-call [role=status]').textContent")
                assert "try again" in line and line != "Ready to join", line
        asyncio.run(run())
    finally:
        server.should_exit = True
        th.join(timeout=10)
