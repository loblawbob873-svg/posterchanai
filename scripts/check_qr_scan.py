#!/usr/bin/env python3
"""The built-in QR scanner, driven with a FAKE CAMERA pointed at a real signer QR.

    venv-unified/bin/python scripts/check_qr_scan.py [base_url]

Signer → "Sign in another device" → Scan QR code opens a camera and looks for a `nostrconnect://`
code. Chrome can be handed a video file as its camera (`--use-file-for-fake-video-capture`), so this
renders the QR the client itself would draw — through the client's own `qr.js` — into a Y4M, points
Chrome at it, and drives the real scanner. A throwaway relay stands in for the far side, so a
successful scan goes all the way to a live NIP-46 signer session rather than stopping at "decoded".

WHY THIS EXISTS. The scanner was reported as "the QR code never gets scanned", and every layer
looked correct in isolation: the encoder round-trips through jsQR in `test_client_qr_encoder.py`, the
vendored jsQR sets `window.jsQR`, and the modal, the video element and the tick loop all read fine.
Nothing in the code says how big a QR is on a screen, or how much of the camera's frame it fills —
and that is the whole question, because the signer URI carries a 15-entry `perms` list which puts the
symbol at **version 18, 89x89 modules**. A test that renders it at 4 pixels per module proves the
decoder works and nothing else; the interesting case is the one where a person is holding a laptop.

So the scenarios are FRAMINGS, not code paths:

  filled    The QR fills the frame — the best case anyone gets. If this fails the scanner is broken.
  aimed     It occupies about a third of the frame's width, which is what pointing a webcam at
            another screen from arm's length looks like. Roughly ONE pixel per module at 640x480.
  blurred   Filling the frame, plus the softness of a webcam that has not focused yet.
  dead-barcodedetector
            Android's WebView, reproduced: a BarcodeDetector that constructs, reports `qr_code` as
            supported, and then resolves every `detect()` to an empty array. That is what a phone
            without the Play Services barcode module does — silently, with nothing to catch — and the
            scanner used to commit to it on sight with no way back to jsQR. The symptom was total:
            the phone could NEVER scan, while Amber (native ML Kit) read the same code every time.

WHAT THE QR CARRIES IS NOT WHAT THE LINK CARRIES, and that is the single biggest thing that makes a
scan work. The full URI's `perms` list is 66% of its bytes and puts the symbol at version 18 (89x89
modules); the QR leaves it out and is version 8 (49x49). Measured across framings, that is the
difference between needing the code to fill ~75% of the camera frame and ~40% — half the distance.
Nothing is lost: `perms` is optional in NIP-46 and advisory to the signer, our own signer ignores it,
Amber prompts per action anyway, and the tap/paste route still carries the full URI to the same
session. `aimed` and `blurred` below exist to fail if it is ever put back.

CALIBRATED BY MEASUREMENT, and the numbers are the useful part. A sharp frame decodes down to **one
pixel per module** — `aimed` passes, which is far better than the code's own comments assumed. What
kills it is SOFTNESS, and only in combination with size: at 2px/module a single 3x3 blur pass is
already unreadable, at 4px/module the same blur decodes fine. So the boundary is "blur is fatal below
about 4 pixels per module", which is why the scanner's on-screen hint tells you to fill the frame
rather than to hold still.

A CORRECTION, kept because the wrong version of it was written down first. An earlier pass concluded
"shortening the URI does not rescue the soft case — density is not the variable", from a single
framing where both the long and the short form happened to land below the threshold. Measured across
framings that is simply false: the short form decodes from 40% frame fill and the long one needs 75%.
The lesson is about the method, not the QR — one framing is one data point, and a threshold cannot be
found from a data point on either side of it.

Exit 0 = clean, 1 = failures (printed), 2 = could not run (no Chrome / no node / no instance).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9499)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-qrscan-check"

W, H, FRAMES = 640, 480, 60
# The third-party scenario runs at a modern camera's resolution, because that is the whole point of
# asking getUserMedia for one: their code is version 19 and only pixels-per-module decides.
BIG_W, BIG_H = 1280, 720
# PC_CAMW/PC_CAMH stand in for a better camera. Pixels per module is the only thing that decides
# whether a dense code reads, and it is the product of the SENSOR resolution and how much of the
# frame the symbol fills — so being able to move the first one is what turns "ask for a bigger
# stream" from a plausible fix into a measured one.
BIG_W = int(os.environ.get("PC_CAMW", BIG_W))
BIG_H = int(os.environ.get("PC_CAMH", BIG_H))

# The URI shape `beginNostrConnect` builds, including the full perms list — the thing that makes the
# symbol dense. Built here rather than scraped from the running client so the check states plainly
# what it is testing; test_client_qr_encoder.py already guards that this matches production.
_KINDS = (0, 1, 3, 4, 5, 6, 7, 1059, 9734, 10000, 10002, 10003, 10050, 27235, 30078)
_PERMS = ("get_public_key%2Cnip04_encrypt%2Cnip04_decrypt%2Cnip44_encrypt%2Cnip44_decrypt"
          + "".join("%2Csign_event%3A" + str(k) for k in _KINDS))


# A THIRD-PARTY code, of the shape Primal and friends print: name, url, an icon URL and the full
# perms list. Ours is deliberately short (perms live in the tap-link, not the QR) and other apps make
# no such choice — so their symbol is denser than anything we generate, and "I cannot scan primal's
# QR" is a size problem on somebody else's payload that only our scanner can fix.
def third_party_uri(app_pk: str, relay: str, secret: str) -> str:
    import urllib.parse
    q = urllib.parse.quote
    return (f"nostrconnect://{app_pk}?relay={q(relay, safe='')}&secret={secret}"
            f"&perms={_PERMS}&name={q('Primal')}"
            f"&url={q('https://primal.net', safe='')}"
            f"&image={q('https://primal.net/assets/primal-logo-512.png', safe='')}")


def signer_uri(app_pk: str, relay: str, secret: str, perms: bool = False) -> str:
    """What the QR carries. `perms` is what the LINK carries and the QR deliberately does not.

    Kept as a switch so the difference stays measurable from here: with the perms list the symbol is
    version 18 (89x89) and needs to fill ~75% of the camera frame; without it, version 8 (49x49) and
    ~40%. That is the whole reason the short form exists.
    """
    import urllib.parse
    return (f"nostrconnect://{app_pk}?relay={urllib.parse.quote(relay, safe='')}"
            f"&secret={secret}" + (f"&perms={_PERMS}" if perms else "") + "&name=PosterChan")


# The Y4M writer runs under node so the modules come from the SHIPPED encoder — a second
# implementation here could be wrong in exactly the way the real one is right.
_Y4M_JS = r"""
const fs=require('fs'); global.window={};
new Function(fs.readFileSync(process.argv[2],'utf8'))();
const PCQR=global.window.PCQR;
const uri=process.argv[3], out=process.argv[4];
const W=+process.argv[5], H=+process.argv[6], FRAMES=+process.argv[7];
const fill=parseFloat(process.argv[8]), blur=+process.argv[9];
const q=PCQR.modules(uri), border=4, dim=q.size+border*2;
const side=Math.floor(Math.min(W,H)*fill);
const s=Math.max(1,Math.floor(side/dim)), px=s*dim, ox=(W-px)>>1, oy=(H-px)>>1;
let Y=new Float64Array(W*H).fill(255);
for(let y=0;y<q.size;y++)for(let x=0;x<q.size;x++){
  if(!q.mod[y][x])continue;
  for(let yy=0;yy<s;yy++)for(let xx=0;xx<s;xx++){
    const X=ox+(x+border)*s+xx, Yy=oy+(y+border)*s+yy;
    if(X>=0&&X<W&&Yy>=0&&Yy<H) Y[Yy*W+X]=0;
  }
}
for(let p=0;p<blur;p++){
  const src=Float64Array.from(Y);
  for(let y=1;y<H-1;y++)for(let x=1;x<W-1;x++){
    let a=0; for(let dy=-1;dy<=1;dy++)for(let dx=-1;dx<=1;dx++) a+=src[(y+dy)*W+(x+dx)];
    Y[y*W+x]=a/9;
  }
}
const yb=Buffer.alloc(W*H); for(let i=0;i<W*H;i++) yb[i]=Math.max(0,Math.min(255,Math.round(Y[i])));
const U=Buffer.alloc((W>>1)*(H>>1),128), V=Buffer.alloc((W>>1)*(H>>1),128);
const parts=[Buffer.from(`YUV4MPEG2 W${W} H${H} F30:1 Ip A1:1 C420\n`)];
for(let f=0;f<FRAMES;f++) parts.push(Buffer.from('FRAME\n'),yb,U,V);
fs.writeFileSync(out,Buffer.concat(parts));
console.log(JSON.stringify({version:(q.size-17)/4,modules:q.size,pxPerModule:s}));
"""


def make_video(uri: str, path: str, fill: float, blur: int, w: int = W, h: int = H) -> dict:
    js = os.path.join(os.path.dirname(path), "_y4m.js")
    with open(js, "w", encoding="utf-8") as fh:
        fh.write(_Y4M_JS)
    r = subprocess.run(
        ["node", js, os.path.join(ROOT, "static/js/client/qr.js"), uri, path,
         str(w), str(h), str(FRAMES), str(fill), str(blur)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return json.loads(r.stdout.strip().splitlines()[-1])


LOGIN = """(async (nsec)=>{
  const sl=ms=>new Promise(r=>setTimeout(r,ms)); const $=s=>document.querySelector(s);
  document.body.classList.remove('guest');
  $('#auth-gate').classList.remove('hidden'); $('#app').classList.add('hidden');
  $('#auth-login').classList.remove('hidden');
  $('#nsec-input').value=nsec; $('#btn-nsec-login').click();
  for(let i=0;i<150;i++){ await sl(200);
    const m=window.__PC&&window.__PC.me&&window.__PC.me();
    if(m&&m.pubkey) return {ok:true,pk:m.pubkey}; }
  return {ok:false,err:'never signed in with the nsec'};})"""

# THE RELAY IS THE WITNESS, not the page. A decoded QR ends in `Nip46Signer.start`, which opens the
# relay named in the URI and publishes a kind-24133 ack — so "did the scan work" is answered by
# whether that event arrived, on the far side, in another process. Judging from the DOM was tried and
# is a trap twice over: the modal CLOSES on success, so its absence describes a pass and a cancel
# identically, and `toast` cannot be intercepted at all — app.js calls its own module-local binding,
# so wrapping `window.toast` silently observes nothing.
SCAN = """(async (seconds)=>{
  const sl=ms=>new Promise(r=>setTimeout(r,ms)); const $=s=>document.querySelector(s);
  window.__PC.switchView('signer');
  for(let i=0;i<60&&!$('#signer-scan-qr');i++) await sl(200);
  const b=$('#signer-scan-qr');
  if(!b) return {err:'Signer has no "Scan sign-in QR" button'};
  if(b.disabled) return {err:'the button is disabled for mode '+((window.__PC.me()||{}).mode)};
  b.click();
  /* Watch for the OUTCOME, not for the video element. A successful scan closes the modal, and on a
   * clean frame that can happen before the first poll — so waiting for #qr-video to appear and
   * calling its absence "the scanner never opened" reports a pass as a failure. Ask once whether the
   * scanner ever existed, and otherwise judge by what the app did with what it read. */
  let opened=false;
  for(let i=0;i<seconds*5;i++){ await sl(200); if($('#qr-video')) opened=true; }
  return {opened:opened, hint:(($('#qr-hint'))||{}).textContent||''};})"""


async def run(url):
    import websockets
    from app.services.nostr import bech32, bip340

    # THIS INSTANCE'S OWN RELAY, not a throwaway one. Since 2026-08-14 the signer refuses to pair on
    # any relay but `CFG.relay_url` — a QR is a picture anyone can print, and it must not be able to
    # aim the half of the app that holds the key. A MiniRelay here would therefore be refused, and
    # the check would report a scanner failure for a scan that worked perfectly.
    import urllib.request as _u
    with _u.urlopen(url + "/client/config", timeout=10) as r:
        relay_url = json.load(r).get("relay_url") or ""
    if not relay_url:
        print("SKIP  this instance publishes no relay_url, so there is nothing to pair on")
        return 2

    if not shutil.which("node"):
        print("SKIP  node not installed")
        return 2
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2

    tmp = PROFILE + "-media"
    os.makedirs(tmp, exist_ok=True)

    app_sk = os.urandom(32)
    app_pk = bip340.pubkey_from_seckey(app_sk).hex()
    uri = signer_uri(app_pk, relay_url, "k9x2m4p7qz")

    # Framings, calibrated against what a camera actually delivers (see the module docstring). The
    # last two are the ones that FAIL if the perms list ever goes back into the QR: at v18 they are
    # 1 and 2 pixels per module, which no amount of holding still recovers.
    # (name, frame fill, blur passes, use a third-party-shaped URI)
    cases = [("filled", 0.92, 0), ("aimed", 0.40, 1), ("blurred", 0.55, 2),
             # ANDROID'S WEBVIEW, reproduced. BarcodeDetector exists and constructs, and `detect()`
             # resolves to an empty array for ever because the Play Services module behind it is not
             # installed — no error, no rejection, nothing to catch. The scanner used to commit to it
             # on sight with no way back to jsQR, so the phone could NEVER scan while Amber (native
             # ML Kit) read the same code off the same screen every time.
             ("dead-barcodedetector", 0.92, 0),
             # Somebody else's code, denser than any we print. Reported as "unable to scan
             # primal.net's QR": their payload carries name, url, an icon and the whole perms list,
             # so it lands several versions above ours and it is not ours to shorten.
             ("third-party", 0.55, 1)]
    only = os.environ.get("PC_ONLY")
    if only:
        cases = [c for c in cases if c[0] == only]
    # PC_FILL overrides how much of the frame the symbol occupies, which is the ONE variable that
    # decides whether a dense third-party code reads: pixels per module. It exists so "I cannot scan
    # primal's QR" can be turned into a threshold ("it reads down to N% of the frame") instead of a
    # yes/no, because the fixed 0.55 above passes and the phone in someone's hand still did not.
    fill_override = os.environ.get("PC_FILL")
    if fill_override:
        cases = [(n, float(fill_override), b) for (n, f, b) in cases]

    problems = []
    try:
        for name, fill, blur in cases:
            lying = name == "dead-barcodedetector"
            # A third-party payload pairs on the same relay on purpose: the point of the scenario is
            # whether the CAMERA can read a denser code, and pairing elsewhere would additionally
            # trip the foreign-relay prompt and test two things at once.
            this_uri = third_party_uri(app_pk, relay_url, "k9x2m4p7qz") \
                if name == "third-party" else uri
            video = os.path.join(tmp, f"{name}.y4m")
            info = make_video(this_uri, video, fill, blur,
                              *( (BIG_W, BIG_H) if name == "third-party" else (W, H) ))
            subprocess.run(["rm", "-rf", PROFILE], check=False)
            proc = subprocess.Popen(
                [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                 f"--use-file-for-fake-video-capture={video}",
                 f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                page = None
                for _ in range(60):
                    try:
                        tabs = json.load(urllib.request.urlopen(
                            f"http://127.0.0.1:{PORT}/json/list"))
                        page = [t for t in tabs if t["type"] == "page"][0]
                        break
                    except Exception:
                        await asyncio.sleep(0.5)
                if not page:
                    print("SKIP  could not start Chrome")
                    return 2
                async with websockets.connect(page["webSocketDebuggerUrl"],
                                              max_size=64 * 1024 * 1024) as ws:
                    n = [0]

                    async def call(m, p=None):
                        n[0] += 1
                        await ws.send(json.dumps({"id": n[0], "method": m, "params": p or {}}))
                        while True:
                            r = json.loads(await ws.recv())
                            if r.get("id") == n[0]:
                                return r.get("result")

                    async def js(e, aw=True):
                        r = await call("Runtime.evaluate",
                                       {"expression": e, "returnByValue": True, "awaitPromise": aw})
                        if r.get("exceptionDetails"):
                            if os.environ.get("PC_DEBUG"):
                                print("  DEBUG EXC:", json.dumps(r["exceptionDetails"])[:400])
                            return None
                        return r["result"].get("value")

                    await call("Runtime.enable")
                    await call("Page.enable")
                    if lying:
                        # On EVERY new document, so it is in place before a line of the client runs —
                        # which is how the real one behaves too.
                        await call("Page.addScriptToEvaluateOnNewDocument", {"source": """
                            (() => {
                              class Dead {
                                static async getSupportedFormats(){ return ['qr_code']; }
                                async detect(){ return []; }   // for ever, and never throws
                              }
                              window.BarcodeDetector = Dead;
                            })()"""})
                    await call("Page.navigate", {"url": url + "/client"})
                    for _ in range(120):
                        await asyncio.sleep(0.25)
                        if await js("!!document.querySelector('#btn-nsec-login')", False):
                            break
                    nsec = bech32.encode("nsec", os.urandom(32))
                    who = await js(f"({LOGIN})({json.dumps(nsec)})") or {}
                    if not who.get("ok"):
                        print(f"SKIP  {who.get('err') or 'could not sign in'}")
                        return 2
                    # Subscribed BEFORE the scan: the ack is published the instant the QR is read,
                    # and a subscription opened afterwards would miss it and report a false failure.
                    acked_evt = asyncio.Event()

                    async def _watch():
                        try:
                            async with websockets.connect(relay_url, open_timeout=15,
                                                          max_size=None) as w:
                                await w.send(json.dumps(["REQ", "scanchk",
                                                         {"kinds": [24133], "#p": [app_pk],
                                                          "since": int(time.time()) - 60}]))
                                while True:
                                    m = json.loads(await w.recv())
                                    if m[0] == "EVENT" and m[2].get("kind") == 24133:
                                        acked_evt.set()
                                        return
                        except Exception:
                            pass

                    watcher = asyncio.ensure_future(_watch())
                    res = await js(f"({SCAN})(20)") or {}
                    try:
                        await asyncio.wait_for(acked_evt.wait(), 10)
                    except asyncio.TimeoutError:
                        pass
                    watcher.cancel()
                    acked = acked_evt.is_set()
                    if not acked:
                        problems.append((name, res.get("err") or (
                            "the scanner opened but nothing reached the relay" if res.get("opened")
                            else "the scanner never opened"),
                                         f"QR v{info['version']} ({info['modules']}x"
                                         f"{info['modules']}), {info['pxPerModule']}px/module, "
                                         f"fill={fill}, blur={blur}"))
                    elif os.environ.get("PC_DEBUG"):
                        print(f"  DEBUG {name}: decoded and acked ({info})", flush=True)
            finally:
                proc.terminate()
    finally:
        subprocess.run(["rm", "-rf", PROFILE], check=False)
        shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        print(f"FAIL  {len(problems)} framing(s) the scanner could not read:")
        for name, err, detail in problems:
            print(f"  [{name}] {err} — {detail}")
        return 1
    print("OK  the QR scanner reads a signer code and opens a signer session")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    try:
        with urllib.request.urlopen(BASE + "/client", timeout=8) as r:
            if r.status != 200:
                raise RuntimeError(r.status)
    except Exception as e:
        print(f"SKIP  {BASE}/client is not reachable ({e})")
        return 2
    return asyncio.run(run(BASE))


if __name__ == "__main__":
    sys.exit(main())
