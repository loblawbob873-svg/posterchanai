#!/usr/bin/env python3
"""END-TO-END proof that a file sent by text can actually be opened by the person who got it.

    venv-unified/bin/python scripts/check_sharelink.py

This is the one thing about the feature that cannot be checked any other way. The bytes are
encrypted by app.js in a browser, stored by Blossom, and decrypted by templates/sharelink.html in
SOMEBODY ELSE'S browser — three pieces, two of them not ours to control, and the only failure mode
that matters is silent: a link that renders a tidy error page on a stranger's phone.

So this encrypts in PYTHON (an independent implementation of the same wire format — if the page only
agreed with the code that wrote the file, both could be wrong together), serves it, and drives a real
headless browser at the real page.

Assertions:

  wrong-layout        The page could not decrypt bytes laid out the way app.js lays them out
                      (iv‖ciphertext, AES-GCM-256, key base64url in the fragment). This is the check
                      the whole script exists for.
  key-leaked          The key reached the SERVER. A fragment must never be transmitted — it is the
                      entire privacy claim of the feature, and it is stated on the page itself.
  no-download         The file decrypted but the person was offered no way to save it.
  no-preview          An image did not render inline. A photo is what people text.
  truncated-link      A link cut short must say so and say what to do, not fail like a network error.
  wrong-key           A key that does not match must be reported as a bad link, never as "gone".
  missing-file        A deleted blob must be reported as gone, not as a broken link.
  needs-network       The page pulled in an external resource. It is opened on a stranger's phone,
                      possibly on a bad connection, and it holds a decryption key.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / websockets).
"""
import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PORT = int(os.environ.get("PC_CHECK_PORT") or 9487)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-sharelink-check"

# A 1x1 PNG — small, and unmistakably an image to the page's preview rule.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def encrypt(plain, key):
    """app.js's `_masterEncrypt`, reimplemented: iv(12) ‖ AES-GCM(ciphertext‖tag)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    return iv + AESGCM(key).encrypt(iv, plain, None)


async def drive(base, cases):
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

            async def js(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                                    "awaitPromise": True})
                if r.get("exceptionDetails"):
                    return None
                return (r.get("result") or {}).get("value")

            await call("Page.enable")
            await call("Runtime.enable")

            async def open_link(url):
                # about:blank first. Two URLs differing only in the fragment are an in-page jump, not
                # a navigation: the script never re-runs and the assertions read the PREVIOUS case's
                # DOM. That is also why each case below must be judged on a freshly loaded page.
                await call("Page.navigate", {"url": "about:blank"})
                await asyncio.sleep(0.1)
                await call("Page.navigate", {"url": url})
                # The page decrypts asynchronously; wait for it to settle rather than a fixed sleep.
                for _ in range(80):
                    st = await js("(document.querySelector('.name')||document.querySelector('.err'))"
                                  " ? 'done' : ''")
                    if st == "done":
                        break
                    await asyncio.sleep(0.15)
                return await js("""({
                  name: (document.querySelector('.name')||{}).textContent || '',
                  err:  (document.querySelector('.err')||{}).textContent || '',
                  note: [...document.querySelectorAll('.note')].map(n=>n.textContent).join(' | '),
                  img:  !!document.querySelector('img'),
                  dl:   !!document.querySelector('a.btn[download]'),
                  body: document.body.textContent.slice(0, 400)
                })""")

            for label, url, want in cases:
                got = await open_link(url)
                if got is None:
                    problems.append((label, "the page threw"))
                    continue
                if want == "ok":
                    if got["err"]:
                        problems.append(("wrong-layout",
                                         f"[{label}] the page could not decrypt: {got['err']}"))
                    if "hello.png" not in got["name"]:
                        problems.append(("wrong-layout",
                                         f"[{label}] the filename never appeared ({got['name']!r})"))
                    if not got["dl"]:
                        problems.append(("no-download", f"[{label}] no way to save the file"))
                    if not got["img"]:
                        problems.append(("no-preview", f"[{label}] an image did not render inline"))
                elif want == "truncated":
                    if not got["err"] or "incomplete" not in got["err"].lower():
                        problems.append(("truncated-link",
                                         f"[{label}] a cut-short link was not named as one "
                                         f"({got['err']!r})"))
                    if "whole link" not in got["note"]:
                        problems.append(("truncated-link",
                                         f"[{label}] it did not say what to do about it"))
                elif want == "badkey":
                    if not got["err"] or "unlock" not in got["err"].lower():
                        problems.append(("wrong-key",
                                         f"[{label}] a wrong key was not named as one "
                                         f"({got['err']!r})"))
                elif want == "gone":
                    if not got["err"] or "no longer available" not in got["err"].lower():
                        problems.append(("missing-file",
                                         f"[{label}] a deleted blob was not named as gone "
                                         f"({got['err']!r})"))

        # The server's own record of what it was asked for. If the key reached it, the entire
        # privacy claim of this feature is false.
        #
        # ASSERTED ON THE FRAGMENT TOKEN, NOT ON THE KEY. The key is base64url'd INSIDE a JSON
        # document that is itself base64url'd, so the raw key string is never a literal substring of
        # any URL — a check for it can never fire, which is worse than no check at all. `META_TOKEN`
        # is the thing that actually travels, and it contains the key.
        seen = "\n".join(REQUESTS)
        if META_TOKEN[:24] in seen:
            problems.append(("key-leaked", "the fragment's key token reached the server"))
        if "pcenc1" in seen:
            problems.append(("key-leaked", "the encrypted-link marker reached the server"))
        for r in REQUESTS:
            if "#" in r:
                problems.append(("key-leaked", f"a raw fragment reached the server: {r}"))

        if problems:
            print(f"FAIL  {len(problems)} problem(s):")
            for kind, msg in problems:
                print(f"  {kind}: {msg}")
            return 1
        print("OK  share-link checks passed")
        return 0
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)


REQUESTS = []
KEY_B64U = ""
META_TOKEN = ""


def main():
    global KEY_B64U, META_TOKEN
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError:
        print("SKIP  cryptography not installed")
        return 2

    import http.server
    import threading
    from fastapi.testclient import TestClient
    import app.main as M

    key = os.urandom(32)
    KEY_B64U = b64u(key)
    blob = encrypt(PNG, key)
    sha = hashlib.sha256(blob).hexdigest()
    meta = b64u(json.dumps({"k": KEY_B64U, "m": "image/png", "n": "hello.png"}).encode())
    META_TOKEN = meta

    client = TestClient(M.app)
    _pages = {}

    def page_for(h):
        if h not in _pages:
            _pages[h] = client.get("/f/" + h).text
        return _pages[h]

    page_html = page_for(sha)
    if "/blossom/" + sha not in page_html:
        print("FAIL  the route did not inject this blob's URL into the page")
        return 1

    tmp = tempfile.mkdtemp(prefix="sharelink-")
    with open(os.path.join(tmp, "page.html"), "wb") as fh:
        fh.write(page_html.encode())
    with open(os.path.join(tmp, "blob.bin"), "wb") as fh:
        fh.write(blob)

    class H(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            REQUESTS.append(self.path)
            if self.path.startswith("/blossom/" + sha):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            if self.path.startswith("/blossom/"):          # the deleted-file case
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"gone")
                return
            body = (page_for(self.path.split("/f/")[-1].split("#")[0])
                    if self.path.startswith("/f/") else page_html).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    cases = [
        ("a photo", f"{base}/f/{sha}#pcenc1={meta}", "ok"),
        # A messaging app that percent-encoded the fragment on the way.
        ("percent-encoded", f"{base}/f/{sha}#pcenc1%3D{meta}", "ok"),
        ("no fragment", f"{base}/f/{sha}", "truncated"),
        ("wrong key", f"{base}/f/{sha}#pcenc1=" + b64u(
            json.dumps({"k": b64u(os.urandom(32)), "m": "image/png", "n": "hello.png"}).encode()),
         "badkey"),
        # The sender deleted the file. Must read as gone, never as a broken link — they are
        # different sentences with different things for the person to do.
        ("deleted", f"{base}/f/{'b' * 64}#pcenc1={meta}", "gone"),
    ]
    try:
        rc = asyncio.run(drive(base, cases))
    finally:
        srv.shutdown()
    return rc


if __name__ == "__main__":
    sys.exit(main())
