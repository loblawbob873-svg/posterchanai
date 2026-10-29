#!/usr/bin/env python3
"""Render docs/promo/posterchanos-promo.html into docs/promo/posterchan-os.webp (exactly 1080x1350).

    venv-unified/bin/python scripts/make_promo_poster.py [--out PATH] [--png PATH] [--check]

Drives headless Chrome over CDP: loads the page from disk, waits for the web fonts (Orbitron, Inter
from Google Fonts -- a poster drawn in a fallback face is refused, not saved) and the image layers,
then MEASURES that every card's text fits inside its card and that nothing leaves the canvas before
taking the screenshot. A poster that clips a line is an error here, never a file.

--check   measure only, write nothing.  --png  also keep the lossless screenshot.
Chrome: $PC_CHROME or /opt/google/chrome/chrome.  Port: $PC_CHECK_PORT (default 9497).
Exit 0 = written (or clean with --check), 1 = layout problems, 2 = could not run.
"""
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs/promo/posterchanos-promo.html"
OUT = ROOT / "docs/promo/posterchan-os.webp"
W, H = 1080, 1350
CHROME = os.environ.get("PC_CHROME") or "/opt/google/chrome/chrome"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9497)

MEASURE = r"""
(async () => {
  await document.fonts.ready;
  const imgs = [...document.images];
  await Promise.all(imgs.map(i => i.complete ? 0 : new Promise(r => { i.onload = i.onerror = r; })));
  const out = {problems: [], cards: 0};
  for (const f of ['900 40px Orbitron', '700 20px Inter', '400 16px Inter'])
    if (!document.fonts.check(f) || ![...document.fonts].some(x => x.status === 'loaded' && f.includes(x.family)))
      out.problems.push('font not loaded: ' + f);
  for (const i of imgs) if (!i.naturalWidth) out.problems.push('image did not load: ' + i.getAttribute('src'));
  const cards = [...document.querySelectorAll('ul.cards > li')];
  out.cards = cards.length;
  if (cards.length !== 12) out.problems.push('expected 12 cards, found ' + cards.length);
  for (const li of cards) {
    const box = li.getBoundingClientRect(), pad = parseFloat(getComputedStyle(li).paddingRight);
    for (const el of li.querySelectorAll('h2,p')) {
      const r = document.createRange(); r.selectNodeContents(el);
      const t = r.getBoundingClientRect();
      if (t.right > box.right - pad + 0.5 || t.bottom > box.bottom - 1 || t.top < box.top)
        out.problems.push(`text overflows its card: "${el.textContent.trim()}" (${Math.round(t.right)} > ${Math.round(box.right - pad)})`);
    }
  }
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('.stage')) continue;
    const r = el.getBoundingClientRect();
    if (r.width && (r.left < 0 || r.right > innerWidth + 0.5 || r.bottom > innerHeight + 0.5))
      out.problems.push('outside the canvas: <' + el.tagName.toLowerCase() + ' class="' + el.className + '">');
  }
  const last = cards[cards.length - 1], foot = document.querySelector('.foot');
  if (last && foot && last.getBoundingClientRect().bottom > foot.getBoundingClientRect().top - 20)
    out.problems.push('the cards run into the footer');
  return JSON.stringify(out);
})()
"""


class CDP:
    def __init__(self, ws_url):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})


def render(check_only=False, out=OUT, png=None):
    if not Path(CHROME).exists():
        print(f"could not run: no Chrome at {CHROME}")
        return 2
    prof = tempfile.mkdtemp(prefix="pc-promo-")
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
                             "--no-first-run", "--hide-scrollbars", "--allow-file-access-from-files",
                             "--force-color-profile=srgb", f"--window-size={W},{H}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2))
                target = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:
                time.sleep(0.1)
        if not target:
            print("could not run: Chrome's debugging port never answered")
            return 2
        c = CDP(target["webSocketDebuggerUrl"])
        c.call("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        c.call("Page.enable")
        c.call("Page.navigate", url=PAGE.as_uri())
        res = None
        for _ in range(3):   # the first evaluate can land before the navigation commits
            time.sleep(0.5)
            r = c.call("Runtime.evaluate", expression=MEASURE, awaitPromise=True, returnByValue=True)
            res = json.loads(r["result"].get("value") or "null")
            if res and res["cards"]:
                break
        if not res:
            print("could not run: the page did not evaluate")
            return 2
        for p in res["problems"]:
            print("PROBLEM:", p)
        if res["problems"]:
            return 1
        if check_only:
            print(f"clean: {res['cards']} cards, every line fits")
            return 0
        shot = c.call("Page.captureScreenshot", format="png",
                      clip={"x": 0, "y": 0, "width": W, "height": H, "scale": 1}, captureBeyondViewport=False)
        raw = base64.b64decode(shot["data"])
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.size != (W, H):
            print(f"PROBLEM: screenshot is {im.size}, not {(W, H)}")
            return 1
        if png:
            Path(png).write_bytes(raw)
        im.save(out, "WEBP", quality=88, method=6)
        print(f"wrote {out} ({Path(out).stat().st_size} bytes, {W}x{H}); {res['cards']} cards, every line fits")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(prof, ignore_errors=True)


def main(argv):
    def arg(name):
        return argv[argv.index(name) + 1] if name in argv else None
    return render(check_only="--check" in argv, out=Path(arg("--out") or OUT), png=arg("--png"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
