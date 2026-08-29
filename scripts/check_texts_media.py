#!/usr/bin/env python3
"""Picture messages in TEXTS, in a real browser, on a device that is not the phone.

Run BEFORE deploying an sms.js change:

    venv-unified/bin/python scripts/check_texts_media.py

WHY THIS EXISTS. tests/client/sms_sim.js runs the shipped sms.js under node, which is the right
tool for the archive's protocol — but it has no DOM, and the bug that hid every picture on the old
messages for weeks lived entirely in the DOM: `hydrateAtt` RETURNED, rather than continued, on the
first placeholder that had left the document, and every repaint of the same conversation does that.
A simulator with no elements cannot see an element being replaced. check_client_mobile.py never
opens this screen, and neither does any other check here.

Nothing here needs a relay, a login, a phone or a network: sms.js takes every helper off
`window.__PC` and reads `window.Relay`/`window.Store`, so the archive is seeded as events and the
encrypted drive is a stub that hands back real pictures with real dimensions.

Assertions, each one a way this screen has actually failed:

  media-never-drawn      a picture message in an opened thread never became an <img> with a real
                         box. This is the headline symptom: "not showing any media from old SMS
                         messages", with the bytes present and nothing in any log.
  repaint-strands-tail   a repaint landing mid-hydration left attachments behind it as bare
                         "Photo…" placeholders. paint() rebuilds #feed on a keystroke, a receipt,
                         a live event and every batch of the cold-load drain, so this is the
                         ordinary case, not a race — and it got WORSE the longer the archive took
                         to load, which is why the slowness and the missing media read as two bugs.
                         MEASURED, and worth knowing: with the address cache below in place this
                         converges even with the old return-instead-of-continue loop, because each
                         repaint's own pass re-runs and its earlier reads are now free. The two
                         halves only strand a thread TOGETHER. The loop's own rules — skip a dead
                         element rather than abort, check isConnected BEFORE the read, leave a
                         drawn one alone — are pinned in tests/client/test_sms_media_recovery.py,
                         which can see a single element being replaced; a browser cannot.
  lost-preview-loses-it  the thumbnail blob is gone and the ORIGINAL is intact, and the bubble drew
                         an error anyway. A preview is an optimisation and must fail like one. It
                         bites the oldest messages first: theirs were written by the oldest builds.
  redraw-refetches       reopening a drawn conversation read every attachment out of encrypted
                         storage again. `ATT` is keyed on the phone's provider row id, which is 0
                         for everything that arrived through the archive — so on every device the
                         archive exists to SERVE, nothing was ever cached.
  attachment-overflows   a picture pushed the conversation sideways at phone width.
  bubble-collapsed       an attachment-only bubble (a photo with no caption) rendered as a sliver.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / websockets).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Phone first, because that is where an attachment overflows, and one desktop width because the
# thread is a different column there.
WIDTHS = [(390, 844, True), (1280, 860, False)]
# PC_CHECK_PORT / PC_CHECK_PROFILE: the checks run CONCURRENTLY and four of them once shared 9473.
PORT = int(os.environ.get("PC_CHECK_PORT") or 9486)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-texts-media-check"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div id="feed"></div>
<nav class="mobilenav glass"><button class="nav-item"><b>Home</b></button></nav>
<div id="modal-root"></div><div id="toast-root"></div>
<script src="/static/js/client/sprite.js"></script>
<script>
/* EVERY CONSOLE ERROR THE PAGE PRODUCES, KEPT. The user should never be the first person to see
   one of these. Today they reported "importKey: Argument 2 is not an object", "files-index save
   HTTP 503" and "channel is not readable with this membership" off their own screen — each of
   which this harness had already loaded the code for and simply was not looking. */
window.__err = [];
for (const k of ['error', 'warn']) {
  const o = console[k].bind(console);
  console[k] = (...a) => { try { window.__err.push(a.map(x => String((x && x.message) || x)).join(' ').slice(0, 200)); } catch (_) {} o(...a); };
}
window.addEventListener('error', e => { try { window.__err.push('uncaught: ' + String(e.message)); } catch (_) {} });
window.addEventListener('unhandledrejection', e => { try { window.__err.push('unhandled: ' + String((e.reason && e.reason.message) || e.reason)); } catch (_) {} });
</script>
<script>
const $  = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const enc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.ICO = () => '';
window.__encCalls = [];      // every encrypted-drive read the client asked for, in order
window.__missing = {};       // sha -> true: these bytes are NOT in the store (a lost thumbnail)
window.__encDelay = 0;       // ms per attachment read — a repaint has to be able to land mid-flight

/* THE ARCHIVE IN THE SHAPE THIS DEPLOYMENT ACTUALLY HAS. Measured on the production relay: every
   `pcai:sms:` event is a Blossom POINTER, not an inline body — 220-388 bytes of ciphertext — so a
   fixture that inlines the message exercises neither the fetch nor the cache in front of it. */
const BODIES = {};           // blob sha -> the JSON the drive holds
let _n = 0;
function archiveEvent(doc, body, at){
  const blob = 'b'.repeat(63) + (++_n).toString(16);
  BODIES[blob] = JSON.stringify(body);
  return { id:'ev'+doc, pubkey:'me', kind:30078, created_at:at,
           content: JSON.stringify({v:1, blob, mime:'application/json'}),
           tags: [['d', doc], ['l', 'pcai-sms']] };
}

const PHOTO2 = 'c'.repeat(64), THUMB_GONE = 'd'.repeat(64);
/* EVERY PICTURE ITS OWN CONTENT HASH. Twelve bubbles sharing one sha collapse to a single read the
   moment anything caches by address, so a shared-sha fixture agrees with the fix and cannot see the
   bug it exists for. */
const photoSha = i => (i.toString(16).padStart(2,'0')).repeat(32);
const EVENTS = [];
const NOW = Date.now();
/* A conversation long enough that a repaint can land in the middle of hydrating it — which is the
   whole point. Twelve pictures, no captions, plus a couple of ordinary texts around them. */
EVENTS.push(archiveEvent('pcai:sms:t0', {address:'+15550100', body:'morning', date:NOW-100000,
                                         incoming:true, name:'Alex'}, 1000));
for(let i=0;i<12;i++)
  EVENTS.push(archiveEvent('pcai:sms:p'+i, {address:'+15550100', body:'', date:NOW-90000+i*1000,
    incoming:true, name:'Alex', mms:true,
    att:[{ct:'image/jpeg', name:'p'+i+'.jpg', bytes:120000, sha:photoSha(i+16), thumb:''}]}, 1001+i));
/* AND ONE WHOSE PREVIEW IS GONE while its original is intact. */
EVENTS.push(archiveEvent('pcai:sms:lostpreview', {address:'+15550100', body:'', date:NOW-1000,
  incoming:true, name:'Alex', mms:true,
  att:[{ct:'image/jpeg', name:'old.jpg', bytes:200000, sha:PHOTO2, thumb:THUMB_GONE}]}, 1100));
window.__missing[THUMB_GONE] = true;

window.Store = { query: () => EVENTS.slice() };
window.Relay = {
  query: async () => EVENTS.slice(),
  subscribe: () => ({id:'sub'}),
  close(){},
};
window.__PC = {
  $, $$, enc,
  toast(){}, uiConfirm: async () => true, uiPrompt: async () => '',
  osNotify(){}, switchView(){}, copyValue(){},
  openLightbox(){}, openEmojiPopover(){}, closeModal(){}, modal(){},
  capPlugin: () => null,                       // NOT the phone: no SMS plugin at all
  nip44enc: async (pk, s) => s,
  nip44dec: async (pk, s) => s,                // identity: the crypto is not what is under audit
  publish: async () => ({ok:false, ev:null}),
  /* The encrypted drive. Real pictures with real dimensions, because an <img> that collapses to
     3px makes every layout assertion below pass for the wrong reason. */
  encFileUrl: async (sha) => {
    window.__encCalls.push(sha);
    if(window.__encDelay && !BODIES[sha])
      await new Promise(r => setTimeout(r, window.__encDelay));
    if(window.__missing[sha]) throw new Error('blob HTTP 404');
    if(BODIES[sha]) return 'data:application/json,' + encodeURIComponent(BODIES[sha]);
    return 'data:image/svg+xml,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="450">' +
      '<rect width="600" height="450" fill="#3a6"/></svg>');
  },
  saveBlobAs: async () => {},
  get ME(){ return {pubkey:'me'}; },
  get VIEW(){ return 'texts'; },
};
</script>
<script src="/static/js/client/sms.js"></script>
<script>
(async function(){
  const S = window.PCSms;
  /* `?repaint=1` — the mode this file exists for. The reads are slowed down so a repaint genuinely
     lands while hydration is in flight (on a real archive a read is a network fetch plus an AES
     decrypt of a megabyte), and the conversation is then repainted out from under itself twice,
     exactly as a keystroke, a delivery receipt, a live archive event or a cold-load drain batch
     does. It has to happen on the FIRST draw: after one complete pass every attachment is
     remembered by content address and there is nothing left in flight to interrupt. */
  const repaint = /[?&]repaint=1/.test(location.search);
  if(repaint) window.__encDelay = 120;
  await S.render();
  for(let i=0;i<80 && !document.querySelector('.sms-thread');i++)
    await new Promise(r=>setTimeout(r,50));
  const open = () => { const row = document.querySelector('.sms-thread'); if(row) row.click(); };
  open();
  if(repaint){
    for(let round=0; round<2; round++){
      await new Promise(r=>setTimeout(r, 200));
      S._state().open = ''; await S.render(); open();
    }
    window.__encDelay = 0;
  }
  window.__ready = true;
})();
</script>
</body></html>"""

# ---------------------------------------------------------------------------- the audits

SETTLE = """(async () => {
  for(let i=0;i<120;i++){
    await new Promise(r=>setTimeout(r,50));
    const left = document.querySelectorAll('.sms-att:not([data-done])').length;
    if(!left && document.querySelectorAll('.sms-att').length) return true;
  }
  return false;
})()"""

AUDIT = """(() => {
  const atts = Array.from(document.querySelectorAll('.sms-att'));
  const imgs = atts.map(a => a.querySelector('img'));
  const box = a => { const r = a.getBoundingClientRect(); return {w:Math.round(r.width), h:Math.round(r.height)}; };
  const bubbles = Array.from(document.querySelectorAll('.bubble'));
  return {
    atts: atts.length,
    drawn: imgs.filter(Boolean).length,
    sized: imgs.filter(i => i && i.getBoundingClientRect().width > 8
                              && i.getBoundingClientRect().height > 8).length,
    texts: atts.filter(a => !a.querySelector('img')).map(a => a.textContent.trim()),
    widest: Math.max(0, ...atts.map(a => box(a).w)),
    docWidth: document.documentElement.scrollWidth,
    winWidth: window.innerWidth,
    shortest: Math.min(999, ...bubbles.filter(b => b.classList.contains('has-att'))
                                      .map(b => Math.round(b.getBoundingClientRect().height))),
    reads: window.__encCalls.length,
  };
})()"""

# REOPENING A CONVERSATION THAT IS ALREADY DRAWN. Nothing should be read out of encrypted storage
# a second time: `ATT` is keyed on the phone's provider row id, which is 0 for every attachment that
# arrived through the archive, so on the devices the archive exists to serve it cached nothing at all.
REDRAW = """(async () => {
  const S = window.PCSms;
  window.__encCalls.length = 0;
  S._state().open = ''; await S.render();
  const row = document.querySelector('.sms-thread'); if(row) row.click();
  for(let i=0;i<60;i++){
    await new Promise(r=>setTimeout(r,50));
    const total = document.querySelectorAll('.sms-att').length;
    if(total && document.querySelectorAll('.sms-att img').length === total) break;
  }
  return { reads: window.__encCalls.length };
})()"""


async def drive(url):
    import websockets
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    try:
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2

        problems = []
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            for w, h, phone in WIDTHS:
                label = f"{w}px"
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2 if phone else 1,
                            "mobile": phone})
                await call("Page.navigate", {"url": url})
                ready = False
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        ready = True
                        break
                if not ready:
                    print(f"SKIP  {label}: the page never opened the conversation")
                    return 2
                if not await js(SETTLE, awaited=True):
                    problems.append(f"{label}: media-never-drawn  hydration never finished — some "
                                    f"attachments were left as placeholders")

                r = await js(AUDIT)
                if r is None:
                    print(f"SKIP  {label}: page did not evaluate")
                    return 2
                if r["atts"] < 13:
                    print(f"SKIP  {label}: the fixture thread did not render "
                          f"({r['atts']} attachments)")
                    return 2
                if r["sized"] < r["atts"]:
                    problems.append(
                        f"{label}: media-never-drawn  {r['atts'] - r['sized']} of {r['atts']} "
                        f"picture messages have no drawn image: {r['texts'][:3]}")
                if r["texts"]:
                    # The lost-preview bubble must NOT be among them.
                    problems.append(
                        f"{label}: lost-preview-loses-it  a bubble fell back to text instead of "
                        f"the original picture: {r['texts'][:3]}")
                if r["docWidth"] > r["winWidth"] + 1:
                    problems.append(
                        f"{label}: attachment-overflows  the page scrolls sideways "
                        f"({r['docWidth']}px in {r['winWidth']}px; widest attachment "
                        f"{r['widest']}px)")
                if r["shortest"] < 40:
                    problems.append(
                        f"{label}: bubble-collapsed  an attachment-only bubble is "
                        f"{r['shortest']}px tall")

                before = r["reads"]
                rd = await js(REDRAW, awaited=True)
                if rd is None:
                    print(f"SKIP  {label}: the redraw probe did not evaluate")
                    return 2
                if rd["reads"] > 2:
                    problems.append(
                        f"{label}: redraw-refetches  reopening a fully drawn conversation read "
                        f"{rd['reads']} attachments out of encrypted storage again "
                        f"(first draw took {before})")

                # …and the same page again, repainted twice while its pictures are still being read.
                await call("Page.navigate", {"url": url + "?repaint=1"})
                ready = False
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        ready = True
                        break
                if not ready:
                    print(f"SKIP  {label}: the repaint page never opened the conversation")
                    return 2
                await js(SETTLE, awaited=True)
                rp = await js(AUDIT)
                if os.environ.get("PC_CHECK_DEBUG"):
                    print(f"DEBUG {label}: first={r} redraw={rd} repaint={rp}")
                if rp is None:
                    print(f"SKIP  {label}: the repaint page did not evaluate")
                    return 2
                # A bubble still showing the bare "Photo…" placeholder is a STRANDED one. A bubble
                # that says why it could not be opened is a different (already reported) finding —
                # conflating them makes one broken attachment look like an abandoned thread.
                stranded = [t for t in rp["texts"] if t.endswith("\u2026")]
                if stranded:
                    problems.append(
                        f"{label}: repaint-strands-tail  a repaint during hydration left "
                        f"{len(stranded)} of {rp['atts']} attachments as bare placeholders — this "
                        f"is the 'no media on the old messages' bug: {stranded[:3]}")

                # NOTHING THE PAGE THREW IS ACCEPTABLE — and this is read LAST, after a quiet
                # second, because the failures that reach the user are the LATE ones: a rejected
                # promise inside a batch decrypt, a 503 from a save that went out after the first
                # paint. An earlier read passes at 350ms of page life and sees none of them.
                # Today's three reports — "importKey: Argument 2 is not an object", "files-index
                # save HTTP 503", "channel is not readable" — were all of that shape.
                await asyncio.sleep(1.0)
                spilled = await js("(window.__err||[]).filter(x=>"
                                   "/error|failed|typeerror|uncaught|unhandled/i.test(x))"
                                   ".slice(0,8)") or []
                if spilled:
                    problems.append(f"{label}: console-errors  the page reported: {spilled}")

        if problems:
            print("TEXTS MEDIA REGRESSIONS")
            for p in problems:
                print("  " + p)
            return 1
        print("OK  texts media: pictures draw, survive a repaint, and are read once")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        # shutil, not `rm -rf`: chrome leaves a component-extension directory rm cannot empty, so
        # every good run printed "Directory not empty" to stderr. Suite noise trains people to
        # ignore the output, which is the opposite of what a check is for.
        shutil.rmtree(PROFILE, ignore_errors=True)


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="textscheck-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE)

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            return os.path.join(tmp, path.lstrip("/") or "index.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/index.html"
    try:
        return asyncio.run(drive(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
