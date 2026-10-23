#!/usr/bin/env python3
"""Does a Concord voice call work between an ARMADA user and a POSTERCHAN user? Live, against production.

tests/client/test_concord_call_livekit_live.py proves PosterChan <-> PosterChan through our own broker. This asks the
cross-client question, with nothing stubbed but the people: two headless Chromes with Chrome's fake microphone --

  armada      https://armada.buzz, a FRESH throwaway key made in memory for this run (logged in the way Armada's own
              e2e suite does it: its nsec login record in localStorage), driving Armada's real UI
  posterchan  the instance (default https://poster.place), the live-journey test identity
              (~/.config/posterchanai/live-journey.key) signed in through the NIP-07 button like verify_live_signin.py

and walks what two people would do:

  1. invite/join   Armada creates "pc-call-interop-test <timestamp>" and makes an invite link; PosterChan opens it
                   and presses "Accept and join".
  2. call, Armada first      Armada presses "Join voice", then PosterChan presses the channel's call button.
  3. call, PosterChan first  both leave; PosterChan joins first, then Armada.
  4. cleanup       PosterChan leaves the community, Armada dissolves it (both always attempted).

For each call it records, from the DevTools network log of each browser: which broker each side asked
(`/.well-known/concord/av/<room>`), the LiveKit room and identity inside the JWT it got back (claims only -- the
token itself is never printed), and the SFU websocket it opened. "Same room" means same broker AND same room.
Then it measures what each side HEARS: the loudest moment (WebAudio RMS) over 4 s of every REMOTE audio track the
page's RTCPeerConnections received -- remote only (the 'track' event), so a side's own fake mic can never count.
The tracks are E2EE-decrypted before they reach WebAudio, so sound there means broker, SFU and keys all agreed.

    venv-unified/bin/python scripts/check_armada_call_interop.py [https://poster.place]

What it found the first time (2026-10-22): Armada first -> both on av.armada.buzz, same room, each hears the other's
398 Hz tone. PosterChan first -> SPLIT: PosterChan correctly takes its own broker for an empty room, and Armada
ignores the occupied broker in presence -- it routes only on the community's `av_brokers` (Concord PR #22, not in
the merged CORD-07 §5, which says join the occupied broker and heal toward the smallest tie-break hash).

Exit 0 = in BOTH join orders each side heard the other. 1 = the call does not work; the line says where it broke
(invite/join, broker/token, same room?, media). 2 = could not run (no key, no Chrome, Armada's UI could not be
driven) -- never a pass. Not part of the deploy gate: it creates a community on public relays and calls through
two production SFUs.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_live_signin import KEY_FILE, SHIM  # noqa: E402  (the same identity and NIP-07 shim)

CHROME = os.environ.get("PC_CHROME", "/opt/google/chrome/chrome")
ARMADA = (os.environ.get("PC_ARMADA_URL") or "https://armada.buzz").rstrip("/")
HEARD = 0.01          # peak RMS: the threshold test_concord_call_livekit_live.py uses


class CouldNotRun(Exception):
    """The harness could not drive a UI step -- exit 2, never a verdict about calls."""


class Broke(Exception):
    """The call (or the join before it) demonstrably does not work -- exit 1."""


# ---------------------------------------------------------------------------------------------------------------
# What runs in both pages before their own code.
# ---------------------------------------------------------------------------------------------------------------
MEASURE = r"""
(()=>{if(window.__pcHook)return;window.__pcHook=1;window.__rtracks=[];
 const RP=window.RTCPeerConnection;
 if(RP){class H extends RP{constructor(...a){super(...a);(window.__pcs=window.__pcs||[]).push(this);this.addEventListener('track',e=>{try{if(e.track&&e.track.kind==='audio')window.__rtracks.push(e.track);}catch(_){}});}}
   window.RTCPeerConnection=H;}
 /* REMOTE tracks only (they came from a 'track' event). A remote WebRTC track only feeds WebAudio while a media
  * element plays it, hence the muted <audio>. Returns the peak RMS and the dominant frequency of each. */
 window.__loud=async(ms)=>{
   const live=window.__rtracks.filter(t=>t.readyState==='live');
   if(!live.length)return {tracks:0,peak:-1,per:[]};
   const ac=new AudioContext();await ac.resume();const keep=[];
   const an=live.map(t=>{const s=new MediaStream([t]);const el=new Audio();el.muted=true;el.srcObject=s;el.play().catch(()=>{});keep.push(el);
     const x=ac.createAnalyser();x.fftSize=4096;ac.createMediaStreamSource(s).connect(x);
     const shown=[...document.querySelectorAll('audio,video')].some(m=>m.srcObject&&m.srcObject.getAudioTracks&&m.srcObject.getAudioTracks().some(y=>y.id===t.id));
     return {x,peak:0,bins:new Float32Array(x.frequencyBinCount).fill(-Infinity),shown};});
   const end=Date.now()+ms,buf=new Float32Array(4096),fb=new Float32Array(2048);
   while(Date.now()<end){for(const a of an){a.x.getFloatTimeDomainData(buf);let s=0;for(const v of buf)s+=v*v;const r=Math.sqrt(s/buf.length);
       if(r>a.peak){a.peak=r;a.x.getFloatFrequencyData(fb);a.bins=fb.slice();}}
     await new Promise(r=>setTimeout(r,50));}
   for(const el of keep)el.srcObject=null;const rate=ac.sampleRate;await ac.close();
   const per=an.map(a=>{let bi=0;for(let i=1;i<a.bins.length;i++)if(a.bins[i]>a.bins[bi])bi=i;
     return {peak:+a.peak.toFixed(4),hz:Math.round(bi*rate/4096),played:a.shown};});
   return {tracks:an.length,peak:Math.max(...per.map(p=>p.peak)),per};};
 /* The same fake microphone the call publishes, heard locally: the reference tone. */
 /* What WebRTC itself counted: audio sent, and audio received vs concealed (a stream whose frames cannot be
  * decrypted arrives as packets the jitter buffer can only conceal). */
 window.__rtp=async()=>{const o={sentBytes:0,recvPackets:0,samples:0,concealed:0};
   for(const pc of (window.__pcs||[])){if(pc.connectionState==='closed')continue;let st;try{st=await pc.getStats();}catch(_){continue;}
     st.forEach(r=>{if(r.kind!=='audio')return;if(r.type==='outbound-rtp')o.sentBytes+=r.bytesSent||0;
       if(r.type==='inbound-rtp'){o.recvPackets+=r.packetsReceived||0;o.samples+=r.totalSamplesReceived||0;o.concealed+=r.concealedSamples||0;}});}
   return o;};
 /* What each app SAID along the way (toasts / status lines), for the report. */
 window.__toasts=[];const seenT=new WeakSet();const grab=()=>{for(const e of document.querySelectorAll('.toast,[data-sonner-toast],li[role=status],ol[role=region] li')){
   if(seenT.has(e))continue;const t=(e.innerText||'').replace(/\s+/g,' ').trim();if(t){seenT.add(e);window.__toasts.push(t.slice(0,200));}}};
 new MutationObserver(grab).observe(document,{subtree:true,childList:true});
 window.__micHz=async()=>{const s=await navigator.mediaDevices.getUserMedia({audio:true});const ac=new AudioContext();await ac.resume();
   const x=ac.createAnalyser();x.fftSize=4096;ac.createMediaStreamSource(s).connect(x);let best=-Infinity,hz=0;const buf=new Float32Array(4096),fb=new Float32Array(2048);
   const end=Date.now()+2500;while(Date.now()<end){x.getFloatTimeDomainData(buf);let q=0;for(const v of buf)q+=v*v;const r=Math.sqrt(q/buf.length);
     if(r>best){best=r;x.getFloatFrequencyData(fb);let bi=0;for(let i=1;i<fb.length;i++)if(fb[i]>fb[bi])bi=i;hz=Math.round(bi*ac.sampleRate/4096);}
     await new Promise(r=>setTimeout(r,50));}
   s.getTracks().forEach(t=>t.stop());await ac.close();return hz;};
})();
"""

# Armada: no native share sheet (headless Chrome's hangs the renderer) and a clipboard we can read back -- the
# invite dialog shares or copies its link and offers no other way to read it.
ARMADA_INIT = r"""
try{delete Navigator.prototype.share;delete Navigator.prototype.canShare;}catch(_){}
window.__copied=[];try{Object.defineProperty(Navigator.prototype,'clipboard',{configurable:true,
  get(){return {writeText:async t=>{window.__copied.push(String(t));},readText:async()=>''};}});}catch(_){}
"""

# Find a visible control by its accessible label or its text, exact first.
FIND = r"""((label,scope)=>{const root=scope?document.querySelector(scope):document;if(!root)return null;
 const els=[...root.querySelectorAll('button,a,[role=menuitem],[role=button]')].filter(b=>b.getClientRects().length&&!b.disabled);
 const name=b=>((b.getAttribute('aria-label')||'')+'\u0001'+(b.innerText||'').trim());
 return els.find(b=>(b.getAttribute('aria-label')||'').trim()===label)||els.find(b=>(b.innerText||'').trim()===label)||null;})"""


def b32(hrp: str, data: bytes) -> str:
    """bech32 (NIP-19) -- for the throwaway Armada nsec."""
    cs = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

    def polymod(v):
        g = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
        c = 1
        for x in v:
            b = c >> 25
            c = (c & 0x1FFFFFF) << 5 ^ x
            for i in range(5):
                c ^= g[i] if (b >> i) & 1 else 0
        return c
    acc = bits = 0
    out = []
    for byte in data:
        acc, bits = (acc << 8) | byte, bits + 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    pm = polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + out + [0] * 6) ^ 1
    return hrp + "1" + "".join(cs[d] for d in out + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


def throwaway_identity() -> dict:
    import coincurve
    sk = secrets.token_bytes(32)
    pk = coincurve.PrivateKey(sk).public_key.format(compressed=True)[1:].hex()
    return {"pk": pk, "nsec": b32("nsec", sk)}


def jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:       # noqa: BLE001
        return {}


def rank(room: str, origin: str) -> str:
    """CORD-07 §5 tie-break, as both clients compute it: sha256(room[32] || utf8(origin)); smallest wins."""
    return hashlib.sha256(bytes.fromhex(room) + origin.encode()).hexdigest()


def say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------------------------------------------
class Browser:
    """One headless Chrome, one tab, over the DevTools protocol: evaluate, auto-accept native dialogs (Armada asks
    `confirm()` before a link invite and before dissolving), and keep the broker/SFU traffic."""

    def __init__(self, name: str, port: str = "0"):
        self.name, self.port, self.n, self.pending = name, port, 0, {}
        self.av: dict[str, dict] = {}       # requestId -> broker request record
        self.sockets: list[dict] = []
        self.dialogs: list[str] = []
        self.errors: list[str] = []
        self.proc = self.ws = self.pumper = None
        self.signed_in = False
        self.prof = tempfile.mkdtemp(prefix=f"pc-armada-call-{name}-")

    async def start(self):
        import httpx
        import websockets
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1366,900", "--no-first-run",
             "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
             "--autoplay-policy=no-user-gesture-required", "--disable-features=WebRtcHideLocalIpsWithMdns",
             f"--remote-debugging-port={self.port}", f"--user-data-dir={self.prof}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        port_file = Path(self.prof, "DevToolsActivePort")
        port = None
        for _ in range(150):
            with contextlib.suppress(FileNotFoundError, IndexError):
                line = port_file.read_text().splitlines()[0]
                if line.isdigit():
                    port = line
                    break
            await asyncio.sleep(0.1)
        if not port:
            raise CouldNotRun(f"{self.name}: Chrome did not start")
        target = None
        async with httpx.AsyncClient(trust_env=False) as h:
            for _ in range(100):
                with contextlib.suppress(Exception):
                    target = next((t["webSocketDebuggerUrl"] for t in (await h.get(f"http://127.0.0.1:{port}/json")).json()
                                   if t.get("type") == "page"), None)
                    if target:
                        break
                await asyncio.sleep(0.1)
        if not target:
            raise CouldNotRun(f"{self.name}: no DevTools page target")
        self.ws = await websockets.connect(target, max_size=None)
        self.pumper = asyncio.create_task(self.pump())
        for m in ("Page.enable", "Runtime.enable", "Network.enable"):
            await self.call(m)

    async def stop(self):
        with contextlib.suppress(Exception):
            if self.pumper:
                self.pumper.cancel()
            if self.ws:
                await self.ws.close()
        with contextlib.suppress(Exception):
            os.killpg(self.proc.pid, 15)
        with contextlib.suppress(Exception):
            self.proc.wait(timeout=10)
        shutil.rmtree(self.prof, ignore_errors=True)

    async def pump(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)
                continue
            meth, p = m.get("method"), m.get("params") or {}
            if meth == "Page.javascriptDialogOpening":
                self.dialogs.append(p.get("message", "")[:200])
                asyncio.create_task(self.call("Page.handleJavaScriptDialog", {"accept": True}))
            elif meth == "Network.requestWillBeSent":
                url = (p.get("request") or {}).get("url", "")
                if "/.well-known/concord/av" in url and (p.get("request") or {}).get("method") == "GET":
                    self.av[p["requestId"]] = {"t": time.monotonic(), "url": url, "status": None, "claims": None, "sfu": None}
            elif meth == "Network.responseReceived" and p.get("requestId") in self.av:
                self.av[p["requestId"]]["status"] = (p.get("response") or {}).get("status")
            elif meth == "Network.loadingFinished" and p.get("requestId") in self.av:
                rec = self.av[p["requestId"]]
                if re.search(r"/av/[0-9a-f]{64}$", rec["url"]) and rec["status"] == 200:
                    asyncio.create_task(self._token_body(p["requestId"], rec))
            elif meth == "Network.webSocketCreated":
                url = p.get("url", "").split("?")[0]           # the query carries the access token
                if "/rtc" in url or "livekit" in url:
                    self.sockets.append({"t": time.monotonic(), "url": url})
            elif meth == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails") or {}
                self.errors.append(((d.get("exception") or {}).get("description") or d.get("text") or "")[:200])

    async def _token_body(self, rid, rec):
        with contextlib.suppress(Exception):
            r = await self.call("Network.getResponseBody", {"requestId": rid})
            body = json.loads((r.get("result") or {}).get("body") or "{}")
            c = jwt_claims(body.get("token", ""))
            rec["claims"] = {"room": (c.get("video") or {}).get("room"), "identity": c.get("sub"),
                             "iss": c.get("iss")}
            rec["sfu"] = body.get("url")

    async def call(self, method, params=None, timeout=30):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)

    async def js(self, expr, timeout=30):
        r = await self.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True}, timeout)
        res = r.get("result") or {}
        if res.get("exceptionDetails"):
            raise RuntimeError(json.dumps(res["exceptionDetails"])[:300])
        return (res.get("result") or {}).get("value")

    async def until(self, expr, seconds, tick=None):
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            if tick:
                with contextlib.suppress(Exception):
                    await tick()
            with contextlib.suppress(Exception):
                v = await self.js(expr, timeout=15)
                if v:
                    return v
            await asyncio.sleep(0.5)
        return None

    async def click(self, label, seconds=30, scope=None, tick=None):
        expr = f"(()=>{{const b={FIND}({json.dumps(label)},{json.dumps(scope)});if(!b)return false;b.click();return true}})()"
        if not await self.until(expr, seconds, tick):
            raise CouldNotRun(f"{self.name}: no '{label}' control after {seconds}s; the page showed: {await self.text(300)}")

    async def text(self, n=400):
        with contextlib.suppress(Exception):
            return (await self.js("(document.body&&document.body.innerText||'').replace(/\\s+/g,' ')", 10) or "")[-n:]
        return "?"

    async def init(self, source):
        await self.call("Page.addScriptToEvaluateOnNewDocument", {"source": source})

    def brokers_since(self, t0):
        """Token requests made after t0, in order: (origin, room-from-path, status, claims, sfu)."""
        out = []
        for rec in sorted(self.av.values(), key=lambda r: r["t"]):
            m = re.match(r"(https://[^/]+)/\.well-known/concord/av/([0-9a-f]{64})$", rec["url"])
            if rec["t"] >= t0 and m:
                out.append({"broker": m.group(1), "room": m.group(2), "status": rec["status"],
                            "claims": rec["claims"], "sfu": rec["sfu"]})
        return out


# ---------------------------------------------------------------------------------------------------------------
async def armada_dismiss(a: Browser):
    """Armada's first-run interstitials (notifications, 'restore your setup') cover the app; wave them off."""
    await a.js("(()=>{for(const l of ['Not now','Skip for now']){const b=" + FIND + "(l,null);if(b)b.click();}return 1})()", 10)


async def armada_login_and_create(a: Browser, ident: dict, name: str) -> dict:
    login = [{"id": "nsec:" + ident["pk"], "type": "nsec", "pubkey": ident["pk"],
              "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
              "data": {"nsec": ident["nsec"]}}]
    await a.init(MEASURE + ARMADA_INIT + "try{if(location.hostname===%s&&!localStorage.getItem('armada:login')){"
                 "localStorage.setItem('armada:login',%s);localStorage.setItem('armada:active-pubkey',%s);}}catch(_){}"
                 % (json.dumps(ARMADA.split("//", 1)[1]), json.dumps(json.dumps(login)), json.dumps(ident["pk"])))
    await a.call("Page.navigate", {"url": ARMADA + "/"})
    tick = lambda: armada_dismiss(a)        # noqa: E731
    await a.click("Add a server or encrypted chat", 60, tick=tick)
    await a.click("Create encrypted community", 20)
    typed = await a.until("(()=>{const e=[...document.querySelectorAll('input')].find(i=>i.getClientRects().length&&(i.placeholder==='Community name'||i.getAttribute('aria-label')==='Community name'));"
                          "if(!e)return false;e.focus();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,%s);"
                          "e.dispatchEvent(new Event('input',{bubbles:true}));return true})()" % json.dumps(name), 20)
    if not typed:
        raise CouldNotRun(f"armada: the community name field never appeared; the page showed: {await a.text()}")
    voice_servers = []
    for _ in range(4):
        await asyncio.sleep(1)
        servers = await a.js("[...document.querySelectorAll('button')].map(b=>b.getAttribute('aria-label')||'').filter(l=>/^Remove https:/.test(l)).map(l=>l.slice(7))")
        if servers:
            voice_servers = servers
        if await a.js("!!" + FIND + "('Create encrypted community','[role=dialog]')") or \
                await a.js("!![...document.querySelectorAll('button')].find(b=>b.innerText.trim()==='Create encrypted community'&&b.getClientRects().length&&!b.disabled)"):
            break
        await a.click("Continue", 15)
    await a.click("Create encrypted community", 20)
    url = await a.until("/\\/c\\/[0-9a-f]{64}\\/[0-9a-f]{64}/.test(location.pathname)&&location.href", 150, tick=tick)
    if not url:
        raise CouldNotRun(f"armada: the community was not created in 150 s; the page showed: {await a.text()}")
    return {"url": url, "voice_servers": voice_servers}


async def armada_invite_link(a: Browser) -> str:
    await a.click("Community menu", 30)
    await a.click("Invite people", 15)
    await a.click("Invite", 20, scope="[role=dialog]")
    link = await a.until("(window.__copied||[]).find(u=>/\\/invite\\//.test(u))||''", 60)
    if not link:
        raise CouldNotRun(f"armada: no invite link was produced; dialogs: {a.dialogs[-2:]}; page: {await a.text()}")
    await a.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));1")
    return link


async def posterchan_login(p: Browser, base: str, sk_hex: str):
    bundle = (ROOT / "static/vendor/nostr/nostr.bundle.js").read_text()
    await p.init("(()=>{" + bundle + "\n;window.__NT=NostrTools;})();window.__LJ_SK=" + json.dumps(sk_hex) + ";" + SHIM + MEASURE)
    await p.call("Page.navigate", {"url": base + "/"})
    if not await p.until("(()=>{const b=document.querySelector('#btn-nip07');return (!!b&&typeof b.onclick==='function')"
                         "||!!(window.__PC&&__PC.me&&__PC.me()&&__PC.me().pubkey)})()", 60):
        raise CouldNotRun("posterchan: no sign-in button after 60 s")
    await p.js("(()=>{const b=document.querySelector('#btn-nip07');if(b)b.click();return 1})()")
    if not await p.until("!!(window.__PC&&__PC.me&&__PC.me()&&__PC.me().pubkey)", 60):
        raise CouldNotRun("posterchan: the NIP-07 sign-in never completed")
    await p.js("__PC.switchMessagesTab('concord');1")
    if not await p.until("document.body.classList.contains('concord-view')&&!!document.querySelector('.cc-app')", 45):
        raise CouldNotRun("posterchan: Communities did not open")
    p.signed_in = True


async def posterchan_join(p: Browser, link: str, name: str, accepted):
    opened = await p.js("(()=>{try{return !!PCConcord.openInvite(%s)}catch(e){return 'ERR '+e.message}})()" % json.dumps(link))
    if opened is not True:
        raise Broke(f"invite/join: PosterChan would not open Armada's invite link ({opened})")
    sheet_js = ("(()=>{const b=document.querySelector('#cc-invite-accept');return b&&b.getClientRects().length?"
                "(document.querySelector('#cc-invite-sheet')||{}).innerText:''})()")
    sheet = None
    for attempt in range(3):
        if attempt:     # what a person does when nothing happened: press "Preview invite" again
            await p.js("(()=>{const g=document.querySelector('#cc-join-go');if(g&&!g.disabled)g.click();return 1})()")
        sheet = await p.until(sheet_js, 40)
        if sheet:
            break
        say(f"      preview attempt {attempt + 1}: nothing after 40 s; PosterChan said {await p.js('window.__toasts.slice(-4)')}")
    if not sheet:
        raise Broke("invite/join: Armada's invite never previewed in PosterChan (3 x 40 s): "
                    + str(await p.js("((document.querySelector('#cc-join')||{}).innerText||'').replace(/\\s+/g,' ').slice(0,300)")))
    say("      preview: " + " ".join(sheet.split())[:160])
    await p.js("document.querySelector('#cc-invite-accept').click();1")
    accepted()
    ok = await p.until("(()=>{const b=document.querySelector('[data-cc-voice-channel=\"general\"]');"
                       "return !!b&&((document.querySelector('.cc-server.active')||{}).title||'')===%s})()" % json.dumps(name), 120)
    if not ok:
        shown = await p.js("((document.querySelector('.cc-app')||{}).innerText||'').replace(/\\s+/g,' ').slice(0,300)")
        raise Broke(f"invite/join: PosterChan accepted but never opened the community with a callable #general (120 s): {shown}")


PANEL_STATUS = "((document.querySelector('.pc-cord-call [role=status]')||{}).textContent||'')"


async def posterchan_call(p: Browser):
    await p.js("document.querySelector('[data-cc-voice-channel=\"general\"]').click();1")
    st = await p.until(f"(()=>{{const s={PANEL_STATUS};return /end-to-end encrypted|try again/.test(s)&&s}})()", 90)
    return st or (await p.js(PANEL_STATUS))


async def posterchan_hangup(p: Browser):
    await p.js("(()=>{const b=document.querySelector('.pc-cord-call [data-call=close]');if(b)b.click();return 1})()")


async def armada_join_call(a: Browser):
    await a.click("Join voice", 30)
    # Armada joins a COMMUNITY call muted (VoiceRoomShell audio={false}; only DM calls pass joinUnmuted), so a
    # person presses the mic button to be heard -- and so does this.
    # The control is drawn before the room has connected, and a press then is lost (measured): press it again
    # until the mic is really on, the way a person would.
    for _ in range(6):
        await a.click("Unmute microphone", 60)
        if await a.until("!!" + FIND + "('Mute microphone',null)", 8):
            break
    else:
        raise CouldNotRun(f"armada: pressed 'Unmute microphone' and the mic never came on; Armada said {await a.js('window.__toasts.slice(-4)')}")


async def armada_hangup(a: Browser):
    await a.js("(()=>{const b=" + FIND + "('Leave call',null);if(b)b.click();return !!b})()")


async def wait_remote_audio(b: Browser, seconds=45):
    return await b.until("(window.__rtracks||[]).filter(t=>t.readyState==='live').length", seconds)


def describe(side: str, reqs: list, sockets: list) -> str:
    if not reqs:
        return f"{side}: asked NO broker for a token"
    last = reqs[-1]
    c = last["claims"] or {}
    tried = ", ".join(f"{r['broker']} -> {r['status']}" for r in reqs)
    return (f"{side}: token requests [{tried}]; using {last['broker']} room={(c.get('room') or last['room'])[:16]}… "
            f"identity={c.get('identity')} sfu={last['sfu']} ; SFU sockets: {[s['url'] for s in sockets] or 'none'}")


async def one_call(a: Browser, p: Browser, first: str) -> dict:
    """Place one call with `first` joining first. Returns the evidence and the verdict for this order."""
    t0 = time.monotonic()
    res = {"order": f"{first} first"}
    if first == "armada":
        await armada_join_call(a)
        await _wait(lambda: any(r["claims"] for r in a.brokers_since(t0)), 60)
        await asyncio.sleep(4)          # its "joined" presence goes out after the SFU connect
        res["posterchan_status"] = await posterchan_call(p)
    else:
        res["posterchan_status"] = await posterchan_call(p)
        await asyncio.sleep(2)
        await armada_join_call(a)
        await _wait(lambda: any(r["claims"] for r in a.brokers_since(t0)), 60)
        await asyncio.sleep(25)         # PosterChan re-ranks occupied brokers when it hears Armada's presence
    await _wait(lambda: any(r["claims"] for r in p.brokers_since(t0)), 30)
    ar, pr = a.brokers_since(t0), p.brokers_since(t0)
    res["armada"] = describe("armada", ar, [s for s in a.sockets if s["t"] >= t0])
    res["posterchan"] = describe("posterchan", pr, [s for s in p.sockets if s["t"] >= t0])
    say("      " + res["armada"])
    say("      " + res["posterchan"])
    say(f"      posterchan panel: {res['posterchan_status']!r}")
    a_tok = next((r for r in reversed(ar) if r["claims"]), None)
    p_tok = next((r for r in reversed(pr) if r["claims"]), None)
    if not a_tok or not p_tok:
        res["broke"] = ("broker/token: " + ("Armada" if not a_tok else "PosterChan") + " got no LiveKit token")
        return res
    room_a, room_p = a_tok["claims"].get("room") or a_tok["room"], p_tok["claims"].get("room") or p_tok["room"]
    if room_a != room_p:
        res["broke"] = f"same room?: different LiveKit rooms ({room_a[:16]}… vs {room_p[:16]}…) -- not the same channel keys"
        return res
    if a_tok["broker"] != p_tok["broker"]:
        res["broke"] = (f"same room?: SPLIT -- same room name {room_a[:16]}… but on two different SFUs: Armada via "
                        f"{a_tok['broker']}, PosterChan via {p_tok['broker']}")
        # WHOSE rule failed. Merged CORD-07 §5: an empty room -> your own broker; an occupied one -> theirs; a split
        # heals toward the smallest sha256(room || origin). So the side that should have moved is the LOSER's.
        winner = min((a_tok["broker"], p_tok["broker"]), key=lambda o: rank(room_a, o))
        mover = "Armada" if winner == p_tok["broker"] else "PosterChan"
        res["broke"] += (f"; by CORD-07 §5 the call belongs on {winner} (occupied first / smallest rank), so {mover} "
                         f"should have joined or migrated there and did not"
                         + (" -- Armada routes ONLY on the community's av_brokers and never follows presence "
                            "(Concord PR #22, unmerged; Armada's PersistentVoiceRoom: 'No automatic split healing')"
                            if mover == "Armada" else " -- a PosterChan bug: cord-call.js should re-rank and move"))
        # Still measure: the honest record of what each side hears in a split is silence.
    res["room"] = room_a
    await asyncio.gather(wait_remote_audio(a, 45), wait_remote_audio(p, 45))
    la, lp = await asyncio.gather(a.js("__loud(4000)", 30), p.js("__loud(4000)", 30))
    ra, rp = await asyncio.gather(a.js("__rtp()", 20), p.js("__rtp()", 20))
    say(f"      WebRTC audio counters  armada: {ra}  posterchan: {rp}")
    res["posterchan_status_after"] = await p.js(PANEL_STATUS)
    say(f"      posterchan panel now: {res['posterchan_status_after']!r}; PosterChan said {await p.js('window.__toasts.slice(-3)')}; "
        f"Armada said {await a.js('window.__toasts.slice(-3)')}")
    res["armada_hears"], res["posterchan_hears"] = la, lp
    say(f"      Armada hears PosterChan: {la}")
    say(f"      PosterChan hears Armada: {lp}")
    people = await p.js("[...document.querySelectorAll('.pc-cord-call [data-call=people] p')].map(x=>x.textContent)")
    say(f"      PosterChan panel people: {people}")
    if "broke" not in res:
        deaf = [who for who, l in (("Armada", la), ("PosterChan", lp)) if not l or l.get("peak", -1) <= HEARD]
        if deaf:
            res["broke"] = "media: " + " and ".join(f"{w} hears nothing (peak RMS {(la if w == 'Armada' else lp).get('peak')})" for w in deaf)
    return res


async def _wait(pred, seconds):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        if pred():
            return True
        await asyncio.sleep(0.5)
    return False


async def posterchan_leave_tests(p: Browser) -> list:
    """Leave EVERY community named like this check's -- this run's, and any an interrupted run left behind."""
    notes = []
    for _ in range(6):
        title = await p.js("(()=>{const b=[...document.querySelectorAll('[data-cc-server]')].find(x=>(x.title||'').startsWith('pc-call-interop-test'));"
                           "if(!b)return '';b.click();return b.title})()")
        if not title:
            break
        if not await p.until("((document.querySelector('.cc-server.active')||{}).title||'')===%s&&!!document.querySelector('#cc-leave-shortcut')" % json.dumps(title), 30):
            notes.append(f"posterchan: could not open '{title}' to leave it -- leave it by hand")
            break
        await p.js("document.querySelector('#cc-leave-shortcut').click();1")
        if not await p.until("(()=>{const b=document.querySelector('.uiconfirm [data-uc=\"1\"]');if(b){b.click();return true}return false})()", 15):
            notes.append(f"posterchan: no Leave confirmation for '{title}' -- leave it by hand")
            break
        gone = await p.until("![...document.querySelectorAll('[data-cc-server]')].some(x=>x.title===%s)" % json.dumps(title), 60)
        notes.append(f"posterchan: left '{title}'" if gone else f"posterchan: '{title}' is still listed after Leave -- check by hand")
        if not gone:
            break
    return notes


async def cleanup(a: Browser, p: Browser, created: bool, joined: bool) -> list:
    notes = []
    with contextlib.suppress(Exception):
        await posterchan_hangup(p)
    with contextlib.suppress(Exception):
        await armada_hangup(a)
    if joined or p.signed_in:
        try:
            notes += await posterchan_leave_tests(p)
        except Exception as e:      # noqa: BLE001
            notes.append(f"posterchan: leaving failed ({e}) -- leave it by hand")
    if created:
        try:
            await asyncio.sleep(2)
            await a.click("Community menu", 20)
            await a.click("Dissolve community", 10)
            gone = await a.until("!/\\/c\\/[0-9a-f]{64}/.test(location.pathname)", 60)
            notes.append("armada: community dissolved" if gone else "armada: dissolve pressed but the page stayed -- check by hand")
        except Exception as e:      # noqa: BLE001
            notes.append(f"armada: dissolving failed ({e}) -- dissolve it by hand")
    return notes


async def run(base: str, sk_hex: str) -> int:
    # checkall's contract: the runner's port for one Chrome; the other binds 0 and reads DevToolsActivePort.
    a, p = Browser("armada"), Browser("posterchan", os.environ.get("PC_CHECK_PORT") or "0")
    name = "pc-call-interop-test " + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    created = joined = False
    results, verdict, why = [], 2, ""
    try:
        await asyncio.gather(a.start(), p.start())
        ident = throwaway_identity()
        say(f"armada identity (throwaway, this run only): {ident['pk']}")
        say(f"brokers answering the CORD-07 probe: {await probe_brokers(base)}")
        say(f"[1] invite/join: Armada creates '{name}'")
        made, _ = await asyncio.gather(armada_login_and_create(a, ident, name), posterchan_login(p, base, sk_hex))
        created = True
        say(f"      created: {made['url']}")
        say(f"      Armada's create wizard listed voice servers (the community's av_brokers): {made['voice_servers']}")
        link = await armada_invite_link(a)
        say(f"      invite link: {link.split('#')[0]}#<secret>")
        def accepted():
            nonlocal joined
            joined = True
        await posterchan_join(p, link, name, accepted)
        say("      PosterChan joined and sees #general with a call button")
        try:
            hz = await p.js("__micHz()", 20)
            say(f"      the fake microphone's tone (reference): {hz} Hz")
        except Exception:       # noqa: BLE001
            pass
        for k, first in enumerate(("armada", "posterchan"), start=2):
            say(f"[{k}] call, {first} joins first")
            r = await one_call(a, p, first)
            results.append(r)
            if r.get("room"):
                ranks = sorted((rank(r["room"], o), o) for o in {ARMADA_AV(made), base})
                say(f"      CORD-07 §5 rank for this room (smallest wins): {[o for _, o in ranks]}")
            say(f"      => {'BROKE at ' + r['broke'] if r.get('broke') else 'both sides heard each other'}")
            await posterchan_hangup(p)
            await armada_hangup(a)
            await asyncio.sleep(6)
        broken = [r for r in results if r.get("broke")]
        verdict = 1 if broken else 0
        why = "; ".join(f"{r['order']}: {r['broke']}" for r in broken)
    except Broke as e:
        verdict, why = 1, str(e)
    except CouldNotRun as e:
        verdict, why = 2, str(e)
    except Exception as e:      # noqa: BLE001 -- could not run is never a pass
        verdict, why = 2, f"{type(e).__name__}: {e}"
    finally:
        with contextlib.suppress(Exception):
            for note in await asyncio.wait_for(cleanup(a, p, created, joined), 150):
                say(f"[cleanup] {note}")
        if a.errors or p.errors:
            say(f"[pages] uncaught: armada {a.errors[-3:]} posterchan {p.errors[-3:]}")
        await asyncio.gather(a.stop(), p.stop(), return_exceptions=True)
    if verdict == 0:
        say("OK: an Armada user and a PosterChan user hear each other, whichever joins first")
    elif verdict == 1:
        say(f"FAIL: the Armada <-> PosterChan call does not work -- {why}")
    else:
        say(f"SKIP: could not run -- {why}")
    return verdict


def ARMADA_AV(made: dict) -> str:       # noqa: N802
    return (made.get("voice_servers") or ["https://av.armada.buzz"])[0]


async def probe_brokers(base: str) -> dict:
    import httpx
    out = {}
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as h:
        for origin in (base, "https://av.armada.buzz", ARMADA):
            try:
                out[origin] = (await h.get(origin + "/.well-known/concord/av")).status_code
            except Exception as e:      # noqa: BLE001
                out[origin] = type(e).__name__
    return out


def main(argv) -> int:
    base = (argv[1] if len(argv) > 1 else "https://poster.place").rstrip("/")
    if not KEY_FILE.exists():
        print(f"SKIP: no test identity ({KEY_FILE} holds a 64-hex secret key)")
        return 2
    sk = KEY_FILE.read_text().strip().lower()
    if len(sk) != 64 or any(c not in "0123456789abcdef" for c in sk):
        print(f"SKIP: {KEY_FILE} is not a 64-hex secret key")
        return 2
    if not Path(CHROME).exists():
        print(f"SKIP: no Chrome at {CHROME}")
        return 2
    try:
        return asyncio.run(asyncio.wait_for(run(base, sk), 1200))
    except Exception as e:      # noqa: BLE001
        print(f"SKIP: could not run: {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
