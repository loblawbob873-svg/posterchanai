#!/usr/bin/env python3
"""Music sharing, end to end, in THREE real browser sessions — and the track-row buttons' styling.

    venv-unified/bin/python scripts/check_music_sharing.py

Three isolated Chrome browser contexts (separate storage, separate throwaway keys) load the SHIPPED
musicshare.js, client.css and nostr-tools against one relay and one Blossom served by this script
(in memory — nothing on disk, nothing in the working tree). The Blossom keeps per-blob OWNERS like
the real server: a DELETE drops one reference, the bytes go with the last one.

  A-shares           A picks two songs, types B's npub into the share dialog and presses Share.
  B-sees             B is OFFERED it under "Shared with me" — Accept / Reject, nothing else.
  B-accepts          Accepting makes it an ordinary playlist chip; rejecting makes it stay gone.
  B-plays            B presses play; the bytes are fetched, decrypted and DECODED as audio.
  B-adds             "Keep 2" — B's server now counts B as an owner of those bytes.
  C-blind            C (a stranger) sees nothing, and cannot decrypt A's document to B even when
                     handed the ciphertext.
  A-revokes          "Stop sharing" in "Shared by me".
  B-share-gone       B's "Shared with me" no longer lists it.
  B-keeps            B's added songs still fetch, still decrypt with the key wrapped to B, and still
                     decode — A's release did not take them.
  grey-buttons       "Music UI looks bad because + and Rename are grey boxes": every button class in
                     app.js's track-row template (read from the shipped file, so a new one is covered
                     the day it is added) and every button the share views draw must NOT render as the
                     browser's default button, at phone and desktop width.
  overflow           the share views push the page sideways at 390px.
  bleed              a label is drawn outside its own button (reported on the APK at phone width).

Exit 0 = clean, 1 = problems, 2 = could not run (no Chrome / no websockets).
"""
import asyncio
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PC_CHECK_PORT") or 9571)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-music-sharing-check"
WIDTHS = [(390, 844, True), (1280, 860, False)]


def _row_button_classes():
    """Every `track-*` BUTTON class the shipped library row template draws."""
    # The library screen moved out of app.js into music.js: read the client source (both).
    sys.path.insert(0, ROOT)
    from tests.client_source import client_source
    src = client_source()
    at = src.index("function _renderMusicList(")
    body = src[at:src.index("\n  function ", at + 10)]
    return sorted(set(re.findall(r'<button class="(track-[a-z]+)', body)))


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
<script src="/static/vendor/nostr/nostr.bundle.js"></script>
</head><body>
<div id="feed" class="feed"></div>
<div id="modal-root"></div>
<script src="/static/js/client/sprite.js"></script>
<script>
const NT = window.NostrTools;
const H = new URLSearchParams(location.hash.slice(1));
const unhex = h => Uint8Array.from(h.match(/../g).map(x => parseInt(x, 16)));
const sk = unhex(H.get('sk')), PK = NT.getPublicKey(sk);
const enc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.__toasts = []; window.__names = {};
let clock = Math.floor(Date.now() / 1000);
const post = (u, b) => fetch(u, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b) }).then(r => r.json());
window.Store = { evs: [], query(){ return this.evs.slice(); }, saveEvent(e){ if(!this.evs.some(x => x.id === e.id)) this.evs.push(e); } };
window.Relay = { ready: async () => {},
  query: async f => { const r = await post('/relay/query', f); const a = r.events; Object.defineProperty(a, 'complete', { value:true }); return a; },
  publish: async ev => post('/relay/publish', ev) };
const conv = peer => NT.nip44.getConversationKey(sk, peer);
const lib = new Map();                 // sha → { m, plain }
const mk = crypto.getRandomValues(new Uint8Array(32));
window.__lib = lib;
window.__PC = {
  me: () => ({ pubkey: PK }),
  nip44enc: async (peer, pt) => NT.nip44.encrypt(pt, conv(peer)),
  nip44dec: async (peer, ct) => NT.nip44.decrypt(ct, conv(peer)),
  publish: async (kind, content, tags) => {
    clock = Math.max(clock + 1, Math.floor(Date.now() / 1000));
    const ev = NT.finalizeEvent({ kind, content, tags, created_at: clock }, sk);
    const r = await window.Relay.publish(ev);
    if(r.ok) window.Store.saveEvent(ev);
    return { ok: !!r.ok, ev };
  },
  mediaServer: () => location.origin + '/blossom',
  uploadBlob: async (file) => {
    const r = await fetch('/blossom/upload', { method:'PUT', headers:{ 'X-Pk': PK }, body: file });
    const j = await r.json(); if(!r.ok) throw new Error(j.error || ('HTTP ' + r.status)); return j.url;
  },
  releaseBlob: async s => (await fetch('/blossom/' + s, { method:'DELETE', headers:{ 'X-Pk': PK } })).ok,
  musicShareKey: async sha => PCMusicShare.deriveKey(mk, sha),
  musicPlainOf: async sha => { const t = lib.get(sha); if(!t || !t.plain) throw new Error('not a library track'); return t.plain; },
  musicLibrary: () => [...lib.keys()],
  musicLibraryAdd: async entries => { for(const [s, m] of entries) lib.set(s, { m }); return true; },
  modal: (html, onMount) => { const bg = document.createElement('div'); bg.className = 'modal-bg';
    bg.innerHTML = '<div class="modal glass neon-border">' + html + '</div>';
    document.getElementById('modal-root').appendChild(bg); if(onMount) onMount(bg.querySelector('.modal')); },
  closeModal: () => { const m = document.querySelector('#modal-root .modal-bg'); if(m) m.remove(); },
  uiConfirm: async () => true, uiPrompt: async () => null,
  toast: m => window.__toasts.push(String(m)), enc,
  profOf: pk => window.__names[pk] ? { name: window.__names[pk] } : {}, needProfile(){},
  attachUserAutocomplete: () => ({}), nip05Resolve: async () => null,
  saveBlobAs: async (blob, name) => { window.__saved = { name, size: blob.size }; },
};
/* A real WAV, so "B plays it" is a DECODE and not a byte count. */
window.__wav = (hz, secs) => {
  const rate = 8000, n = Math.floor(rate * secs), b = new ArrayBuffer(44 + n * 2), v = new DataView(b);
  const w = (o, s) => { for(let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); w(8, 'WAVE'); w(12, 'fmt '); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, n * 2, true);
  for(let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.round(Math.sin(2 * Math.PI * hz * i / rate) * 12000), true);
  return new Uint8Array(b);
};
window.__decode = async (u8) => { const ctx = new OfflineAudioContext(1, 8000, 8000);
  const buf = await ctx.decodeAudioData(u8.slice().buffer); return buf.duration; };
window.__ctx = { play: async (sha) => {
  try{ const pt = await PCMusicShare.plain(sha); window.__played = { sha, duration: await window.__decode(pt) }; }
  catch(e){ window.__played = { sha, error: String(e && e.message || e) }; } } };
</script>
<script src="/static/js/client/musicshare.js"></script>
<script>window.__ready = !!window.PCMusicShare;</script>
</body></html>"""


# ------------------------------------------------------------------------------------ the server

class Net:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.blobs = {}          # sha → {"bytes", "owners": set}

    @staticmethod
    def _d(e):
        return next((t[1] for t in e.get("tags", []) if t and t[0] == "d" and len(t) > 1), "")

    def publish(self, ev):
        with self.lock:
            if ev["kind"] == 5:
                for t in ev.get("tags", []):
                    if t[0] != "a":
                        continue
                    k, pk, d = (t[1].split(":", 2) + ["", "", ""])[:3]
                    self.events = [e for e in self.events if not (str(e["kind"]) == k and e["pubkey"] == pk
                                   and self._d(e) == d and e["created_at"] <= ev["created_at"])]
                self.events.append(ev)
                return True
            if 30000 <= ev["kind"] < 40000:
                cur = [e for e in self.events if e["kind"] == ev["kind"] and e["pubkey"] == ev["pubkey"]
                       and self._d(e) == self._d(ev)]
                if any(e["created_at"] > ev["created_at"] for e in cur):
                    return False
                self.events = [e for e in self.events if e not in cur]
            self.events.append(ev)
            return True

    def query(self, filters):
        def ok(f, e):
            if "kinds" in f and e["kind"] not in f["kinds"]:
                return False
            if "authors" in f and e["pubkey"] not in f["authors"]:
                return False
            for k, vals in f.items():
                if k.startswith("#") and not any(t and t[0] == k[1:] and len(t) > 1 and t[1] in vals
                                                  for t in e.get("tags", [])):
                    return False
            return True
        with self.lock:
            return [e for e in self.events if any(ok(f, e) for f in filters)]


def make_server(tmp, net):
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            return os.path.join(tmp, path.lstrip("/") or "index.html")

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _blob(self):
            m = re.match(r"^/blossom/([0-9a-f]{64})$", self.path.split("?")[0])
            return m.group(1) if m else None

        def do_POST(self):
            if self.path == "/relay/publish":
                return self._json(200, {"ok": net.publish(json.loads(self._body()))})
            if self.path == "/relay/query":
                return self._json(200, {"events": net.query(json.loads(self._body()))})
            return self._json(404, {})

        def do_PUT(self):
            if self.path != "/blossom/upload":
                return self._json(404, {})
            pk = self.headers.get("X-Pk", "")
            data = self._body()
            sha = hashlib.sha256(data).hexdigest()
            with net.lock:
                b = net.blobs.setdefault(sha, {"bytes": data, "owners": set()})
                b["owners"].add(pk)
            return self._json(200, {"url": f"http://127.0.0.1:{self.server.server_port}/blossom/{sha}", "sha256": sha})

        def do_DELETE(self):
            sha = self._blob()
            pk = self.headers.get("X-Pk", "")
            with net.lock:
                b = net.blobs.get(sha)
                if b:
                    b["owners"].discard(pk)
                    if not b["owners"]:
                        del net.blobs[sha]
            return self._json(200, {"ok": True})

        def _serve_blob(self, head):
            sha = self._blob()
            with net.lock:
                b = net.blobs.get(sha)
            if not b:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(b["bytes"])))
            self.end_headers()
            if not head:
                self.wfile.write(b["bytes"])

        def do_HEAD(self):
            if self.path.startswith("/blossom/"):
                return self._serve_blob(True)
            return super().do_HEAD()

        def do_GET(self):
            if self.path.startswith("/owners/"):
                sha = self.path.split("/")[-1]
                with net.lock:
                    b = net.blobs.get(sha)
                return self._json(200, {"owners": sorted(b["owners"]) if b else None})
            if self.path.startswith("/blossom/"):
                return self._serve_blob(False)
            return super().do_GET()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------------------------------ the browser

class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def call(self, method, params=None):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            raise RuntimeError("page threw: " + json.dumps(r["exceptionDetails"])[:800])
        return r["result"].get("value")

    async def until(self, expr, secs=20):
        for _ in range(int(secs * 10)):
            try:
                if await self.js(expr):
                    return True
            except RuntimeError:
                pass
            await asyncio.sleep(0.1)
        return False


AUDIT = r"""(() => {
  const ref = document.createElement('button'); ref.textContent = 'x'; document.body.appendChild(ref);
  const rcs = getComputedStyle(ref), ua = { bg: rcs.backgroundColor, border: rcs.borderTopStyle }; ref.remove();
  const bad = [];
  for(const b of document.querySelectorAll('#feed button, #probe button')){
    const cs = getComputedStyle(b);
    if(b.offsetParent === null) continue;
    if(cs.backgroundColor === ua.bg && cs.borderTopStyle === ua.border)
      bad.push((b.className || b.id || b.tagName) + ' bg=' + cs.backgroundColor + ' border=' + cs.borderTopStyle);
  }
  /* TEXT LEAVING ITS BUTTON — the report was "the text on the two buttons are bleeding out the
     button", which is not page overflow (the button keeps its size, the words simply draw past it)
     and not a grey button. Measured against the CONTENTS' own rectangle rather than scrollWidth,
     because an `overflow:visible` box reports nothing unusual for the one thing being asked about. */
  const bleed = [];
  for(const b of document.querySelectorAll('#feed button, #probe button')){
    if(b.offsetParent === null || !b.textContent.trim()) continue;
    const range = document.createRange(); range.selectNodeContents(b);
    const t = range.getBoundingClientRect(), r = b.getBoundingClientRect();
    range.detach && range.detach();
    if(!t.width) continue;
    const over = Math.max(r.left - t.left, t.right - r.right);
    if(over > 1) bleed.push((b.id || b.className || b.tagName) + ' by ' + Math.round(over) + 'px: ' + b.textContent.trim().slice(0, 40));
  }
  return { bad, bleed, ua, overflow: document.documentElement.scrollWidth - window.innerWidth,
           rows: document.querySelectorAll('#probe .track button').length };
})()"""


async def drive(base, problems):
    import websockets
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--autoplay-policy=no-user-gesture-required",
                             f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ver = None
        for _ in range(60):
            try:
                ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version"))
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not ver:
            print("SKIP  could not start Chrome")
            return 2
        import secrets
        keys = {n: secrets.token_hex(32) for n in "ABC"}
        async with websockets.connect(ver["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as bws:
            browser = Page(bws)
            conns, pages = [], {}
            for n in "ABC":
                cx = await browser.call("Target.createBrowserContext")
                t = await browser.call("Target.createTarget", {"url": "about:blank", "browserContextId": cx["browserContextId"]})
                ws = await websockets.connect(f"ws://127.0.0.1:{PORT}/devtools/page/{t['targetId']}", max_size=64 * 1024 * 1024)
                conns.append(ws)
                p = Page(ws)
                await p.call("Runtime.enable")
                await p.call("Page.enable")
                await p.call("Page.navigate", {"url": f"{base}/index.html#sk={keys[n]}&n={n}"})
                if not await p.until("window.__ready === true"):
                    print(f"SKIP  page {n} never loaded musicshare.js")
                    return 2
                pages[n] = p
            A, B, C = pages["A"], pages["B"], pages["C"]
            pk = {n: await pages[n].js("window.__PC.me().pubkey") for n in "ABC"}
            npub_b = await A.js(f"NostrTools.nip19.npubEncode({json.dumps(pk['B'])})")
            for p in pages.values():
                await p.js(f"window.__names = {json.dumps({pk['A']: 'Alice', pk['B']: 'Bob', pk['C']: 'Carol'})}; true")

            # ---- A shares two songs with B, through the dialog ----
            await A.js("""(async () => {
              const add = async (name, hz) => { const plain = __wav(hz, 0.5);
                const sha = Array.from(crypto.getRandomValues(new Uint8Array(32))).map(b => b.toString(16).padStart(2,'0')).join('');
                __lib.set(sha, { m: { name, mime:'audio/wav', size: plain.length, enc:true, mk:true, folder:'Music' }, plain });
                return { sha, name, mime:'audio/wav', size: plain.length, ext:'wav' }; };
              window.__tracks = [await add('Sunrise', 440), await add('Sunset', 330)];
              PCMusicShare.openShareDialog({ name:'Road trip', tracks: window.__tracks });
              return true; })()""")
            await A.js(f"document.querySelector('#msh-to').value = {json.dumps(npub_b)}; document.querySelector('#msh-go').click(); true")
            if not await A.until("window.__toasts.some(t => /^shared 2 songs with 1 person/.test(t))", 30):
                problems.append("A-shares: no 'shared 2 songs' toast — " + json.dumps(await A.js("window.__toasts")
                                + [await A.js("(document.querySelector('#msh-prog')||{}).textContent||''")]))
                return 1
            wire = await A.js(f"""(async () => (await Relay.query([{{kinds:[30078], authors:[{json.dumps(pk['A'])}],
                                   '#p':[{json.dumps(pk['B'])}]}}]))[0].content)()""")

            if not wire or "Road trip" in wire or "Sunrise" in wire:
                problems.append("C-blind: the share document is readable on the wire")

            # ---- B is OFFERED it, accepts, and it becomes a playlist ----
            # "Shared with me" is the decision; what survives it is an ordinary playlist chip.
            await B.js("PCMusicShare.renderIn(document.getElementById('feed'), window.__ctx); true")
            if not await B.until("document.querySelector('.msh-offer') && /Road trip/.test(document.querySelector('.msh-offer').textContent)"
                                 " && /Alice/.test(document.querySelector('.msh-offer').textContent)"):
                problems.append("B-sees: 'Road trip' from Alice is not offered under Shared with me")
                return 1
            if not await B.js("!!document.querySelector('.msh-yes') && !!document.querySelector('.msh-no')"):
                problems.append("B-sees: the offer has no Accept / Reject")
                return 1
            bar = await B.js("PCMusicShare.barHTML('', false)")
            if "Shared with me" not in bar or 'ma-pln">1<' not in bar or "ma-plshare" not in bar:
                problems.append("B-sees: the chip bar does not count the waiting share: " + bar[:300])
            if "Road trip" in bar:
                problems.append("B-sees: an UNANSWERED share is already a playlist chip")
            await B.js("document.querySelector('.msh-yes').click(); true")
            if not await B.until("document.querySelectorAll('.msh-track').length === 2"):
                problems.append("B-accepts: accepting did not open the playlist with its two songs")
                return 1
            bar = await B.js("PCMusicShare.barHTML('', false)")
            if "Road trip" not in bar or 'ma-pln">2<' not in bar:
                problems.append("B-accepts: the accepted share is not a playlist chip: " + bar[:300])
            if 'ma-pln">1<' in bar.split("Shared with me")[-1][:60]:
                problems.append("B-accepts: 'Shared with me' still counts a share that was answered")
            # …and the rejected one stays gone, which is what makes "no" mean no.
            key = await B.js("(PCMusicShare.acceptedShares()[0]||{}).key||''")
            await B.js(f"PCMusicShare.decide({json.dumps(key)}, false); true")
            if await B.js("PCMusicShare.acceptedShares().length") != 0 or await B.js("PCMusicShare.pendingShares().length") != 0:
                problems.append("B-rejects: a rejected share did not stay rejected")
            await B.js(f"PCMusicShare.decide({json.dumps(key)}, true); true")
            await B.js("PCMusicShare.renderShared(" + json.dumps(key) + ", document.getElementById('feed'), window.__ctx); true")
            if not await B.until("document.querySelectorAll('.msh-track').length === 2"):
                problems.append("B-accepts: the playlist did not redraw after re-accepting")
                return 1
            await B.js("document.querySelector('.msh-track .track-play').click(); true")
            if not await B.until("window.__played"):
                problems.append("B-plays: pressing play did nothing")
            else:
                played = await B.js("window.__played")
                if not (played.get("duration") and abs(played["duration"] - 0.5) < 0.05):
                    problems.append(f"B-plays: the song did not decode: {played}")

            # ---- layout + the grey-box report, on B's open share and on the library row template ----
            classes = _row_button_classes()
            icon = '<svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>'
            probe = ('<div class="music-list" id="probe"><div class="ma-pls">' + bar + '</div><div class="track">' + "".join(
                f'<button class="{c}" data-sha="{"0" * 64}">{icon}</button>' if c != "track-name" else "" for c in classes)
                + '<span class="track-name">A fairly long song title that has to fit</span><span class="track-meta">🔒 3.1MB</span></div></div>')
            await B.js(f"document.body.insertAdjacentHTML('beforeend', {json.dumps(probe)}); true")
            for (w, h, phone) in WIDTHS:
                await B.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": phone})
                await asyncio.sleep(0.3)
                r = await B.js(AUDIT)
                if r["rows"] < 5:
                    problems.append(f"grey-buttons: the row probe drew {r['rows']} buttons from {classes}")
                for b in r["bad"]:
                    problems.append(f"grey-buttons @{w}px: renders as the browser's default button: {b}")
                if r["overflow"] > 1:
                    problems.append(f"overflow @{w}px: the shared view is {r['overflow']}px wider than the screen")
                if r.get("bleed"):
                    problems.append(f"bleed @{w}px: text is drawn outside its own button — " + "; ".join(r["bleed"][:3]))
            # AND THE BLEED DETECTOR PROVES IT CAN FAIL. With the labels shortened ("Shuffle", not
            # "Shuffle playlist"; "Keep 2", not "Add 2 to my library") nothing on screen overflows
            # any more, so a silent detector and a working one look identical. This plants exactly
            # what was reported - a long label in a narrow button that may not wrap - and requires
            # it to be caught, then takes it away again.
            await B.js("document.getElementById('probe').insertAdjacentHTML('beforeend',"
                       "'<div class=\"music-head\"><div class=\"music-head-primary\">"
                       "<button class=\"btn btn-ghost small\" id=\"bleedprobe\" style=\"white-space:nowrap;width:70px\">"
                       "Add 2 to my library</button></div></div>'); true")
            probe = await B.js(AUDIT)
            if not any("bleedprobe" in b for b in probe.get("bleed", [])):
                problems.append("the bleed detector did not catch a label drawn outside its own button")
            await B.js("document.getElementById('bleedprobe').closest('.music-head').remove(); true")
            await B.js("document.getElementById('probe').remove(); true")

            # ---- B adds both to the library ----
            await B.js("document.getElementById('msh-addall').click(); true")
            if not await B.until("window.__toasts.some(t => /^kept 2 songs/.test(t))", 30):
                problems.append("B-adds: no 'added 2 songs' — " + json.dumps(await B.js("window.__toasts")))
                return 1
            added = await B.js("[...__lib.keys()]")
            for s in added:
                own = json.load(urllib.request.urlopen(f"{base}/owners/{s}"))["owners"] or []
                if pk["B"] not in own:
                    problems.append(f"B-adds: B is not an owner of {s[:12]} — the copy would vanish with A's")

            # ---- C: sees nothing, and cannot open A's document to B ----
            c_in = await C.js("PCMusicShare.loadIn().then(l => l.length)")
            if c_in:
                problems.append(f"C-blind: C was shown {c_in} share(s)")
            c_read = await C.js(f"""(async () => {{ try{{ await __PC.nip44dec({json.dumps(pk['A'])}, {json.dumps(wire)}); return 'DECRYPTED'; }}
                                    catch(e){{ return 'refused'; }} }})()""")
            if c_read != "refused":
                problems.append("C-blind: C decrypted A's share to B")
            await C.js("PCMusicShare.renderIn(document.getElementById('feed'), window.__ctx); true")
            if not await C.until("/Nothing has been shared with you/.test(document.getElementById('feed').textContent)"):
                problems.append("C-blind: C's Shared with me is not empty")

            # ---- A revokes ----
            await A.js("PCMusicShare.renderOut(document.getElementById('feed'), {}); true")
            if not await A.until("document.querySelector('.msh-out .msh-revoke') && /Bob/.test(document.querySelector('.msh-out').textContent)"):
                problems.append("A-revokes: Shared by me does not list the share to Bob")
                return 1
            await A.js("document.querySelector('.msh-out .msh-revoke').click(); true")
            if not await A.until("window.__toasts.some(t => /^stopped sharing/.test(t))", 30):
                problems.append("A-revokes: no 'stopped sharing' — " + json.dumps(await A.js("window.__toasts")))

            # ---- B: the share is gone, the added songs are not ----
            await B.js("PCMusicShare.renderIn(document.getElementById('feed'), window.__ctx); true")
            if not await B.until("/Nothing has been shared with you/.test(document.getElementById('feed').textContent)"):
                problems.append("B-share-gone: B still lists the revoked share")
            keeps = await B.js("""(async () => { const out = [];
              for(const [s, t] of __lib){ try{
                const r = await fetch('/blossom/' + s); if(!r.ok){ out.push('HTTP ' + r.status); continue; }
                const { k, iv } = JSON.parse(await __PC.nip44dec(__PC.me().pubkey, t.m.keyenc));
                const pt = await PCMusicShare.open(PCMusicShare.unb64(k), PCMusicShare.unb64(iv), new Uint8Array(await r.arrayBuffer()));
                out.push(await __decode(pt)); }catch(e){ out.push(String(e && e.message || e)); } }
              return out; })()""")
            if len(keeps) != 2 or not all(isinstance(x, (int, float)) and abs(x - 0.5) < 0.05 for x in keeps):
                problems.append(f"B-keeps: B's added songs after the revoke: {keeps}")
            for ws in conns:
                await ws.close()
        return 1 if problems else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        subprocess.run(["rm", "-rf", PROFILE], check=False)


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    tmp = tempfile.mkdtemp(prefix="musicshare-check-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE)
    srv = make_server(tmp, Net())
    problems = []
    try:
        code = asyncio.run(drive(f"http://127.0.0.1:{srv.server_port}", problems))
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    for p in problems:
        print("FAIL  " + p)
    if code == 0:
        print("OK    music sharing: A shared, B played + added, C was blind, A revoked, B kept; no grey buttons")
    return code if code == 2 else (1 if problems else 0)


if __name__ == "__main__":
    sys.exit(main())
