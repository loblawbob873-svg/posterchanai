"""Connecting to a SERVER must not decide your relays for you.

Run: venv-unified/bin/python -m pytest tests/client/test_auth_conn_relay_adopt.py

The sign-in screen pre-fills its relay box with the app's own suggestions, because an empty box asks
someone to already know which relays exist. Pressing "Connect" on the SERVER field then carried that
box into ClientSettings — `_persistAuthRelays` compared it against the SAVED list, which is empty for
anyone who has never chosen relays, so a box the app filled in itself always looked like an edit. The
app suggested six relays and then took its own suggestion as the user's answer, flipping the client
from one trusted relay onto six untrusted ones nobody picked.

That is not cosmetic. One of those six (`wss://offchain.pub/`) was unreachable, and an unreachable
relay in the pool used to hold every one-shot query to its 6s timeout, which emptied Home/Global/
Trending for the whole session (tests/test_client_relay_eose_gate.py covers that half). This test
covers the half that put the list there: a tablet on a relay list its owner never chose.

Both directions are asserted, because "never save" would be just as wrong as "always save":
  untouched  → Connect saves nothing; the app keeps using the built-in relay.
  edited     → Connect still carries the edit over its own reload, which is why it does this at all.

Bundled mode (`__PC_API_BASE__` defined) because the server field only exists there — the web page IS
its own server. Real Chrome, real templates/client.html, no network.
"""
import asyncio
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHROME = shutil.which("google-chrome-stable")
try:
    import websockets  # noqa: F401
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

# Open the connection pane, then drive the SERVER button twice: once with the relay box exactly as the
# app pre-filled it, once after a real edit. __PC_SET_INSTANCE__ is stubbed so the page does not
# navigate out from under the probe — it is the localStorage write we are inspecting, not the reload.
PROBE = r"""(() => {
  const q = s => document.querySelector(s);
  const st = () => { try { return JSON.parse(localStorage.getItem('pc_nostr_settings') || '{}'); }
                     catch (e) { return { ERR: 1 }; } };
  window.__PC_SET_INSTANCE__ = () => {};
  const gl = q('#guest-login2') || q('#guest-signup'); if (gl) gl.click();
  const b = q('#btn-auth-conn'); if (b) b.click();

  const out = { prefilled: ((q('#conn-relays') || {}).value || '').split('\n').filter(Boolean).length };

  // 1. untouched: exactly what _fillAuthConnFields put in the box.
  q('#conn-instance').value = 'example.org';
  q('#btn-conn-instance').click();
  let s = st();
  out.untouchedEnabled = s.relaysEnabled === true;
  out.untouchedSaved = s.relays || null;

  // 2. edited: a list the user actually typed.
  q('#conn-relays').value = 'wss://relay.example.one\nwss://relay.example.two';
  q('#btn-conn-instance').click();
  s = st();
  out.editedEnabled = s.relaysEnabled === true;
  out.editedSaved = s.relays || null;
  return out;
})()"""


def _render_shell():
    with open(os.path.join(ROOT, "templates", "client.html"), encoding="utf-8") as fh:
        html = fh.read()
    html = re.sub(r"\{%\s*if not nostr_only\s*%\}(.*?)\{%\s*endif\s*%\}", r"\1", html, flags=re.S)
    html = re.sub(r"\{%\s*if nostr_only\s*%\}.*?\{%\s*endif\s*%\}", "", html, flags=re.S)
    html = re.sub(r"\{%\s*if secure\s*%\}.*?\{%\s*endif\s*%\}", "", html, flags=re.S)
    html = html.replace('{{ default_theme|default("cyberpunk") }}', "cyberpunk").replace("{{ ver }}", "1")
    html = re.sub(r"\{\{.*?\}\}", "", html)
    # What build-www.sh injects: this is what makes it a bundled app rather than a web page.
    return html.replace("<head>", "<head><script>window.__PC_API_BASE__='';</script>", 1)


@unittest.skipUnless(CHROME and HAVE_WS, "needs chrome + websockets")
class ConnectDoesNotAdoptTheSuggestions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pcauthadopt-")
        os.makedirs(os.path.join(cls.tmp, "static"), exist_ok=True)
        for sub in ("js", "css", "vendor", "fonts"):
            src = os.path.join(ROOT, "static", sub)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(cls.tmp, "static", sub))
        with open(os.path.join(cls.tmp, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(_render_shell())

        directory = cls.tmp

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=directory, **k)

            def log_message(self, *a):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        cls.httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
        port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

        cdp = 9337
        profile = os.path.join(cls.tmp, "chrome")
        subprocess.run(["pkill", "-f", f"remote-debugging-port={cdp}"], capture_output=True)
        cls.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--remote-debugging-port={cdp}", f"--user-data-dir={profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        async def drive():
            import websockets as ws_mod
            page = None
            for _ in range(60):
                try:
                    page = [t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/list"))
                            if t["type"] == "page"][0]
                    break
                except Exception:
                    time.sleep(0.5)
            if not page:
                raise AssertionError("could not start chrome")
            async with ws_mod.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
                n = [0]

                async def call(m, p=None):
                    n[0] += 1
                    await ws.send(json.dumps({"id": n[0], "method": m, "params": p or {}}))
                    while True:
                        r = json.loads(await ws.recv())
                        if r.get("id") == n[0]:
                            return r.get("result")

                await call("Runtime.enable")
                await call("Page.enable")
                await call("Page.navigate", {"url": f"http://127.0.0.1:{port}/index.html"})
                await asyncio.sleep(8)
                r = await call("Runtime.evaluate",
                               {"expression": PROBE, "returnByValue": True, "awaitPromise": True})
                if r.get("exceptionDetails"):
                    raise AssertionError("probe threw: " + str(r["exceptionDetails"].get("text")))
                return r["result"]["value"]

        cls.out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(drive())

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
        except Exception:
            pass
        try:
            cls.httpd.shutdown()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_box_is_prefilled(self):
        """If this ever stops being true the rest of the test is asserting nothing."""
        self.assertGreater(self.out["prefilled"], 1,
                           "the relay box is no longer pre-filled — re-check what Connect can adopt")

    def test_connecting_to_a_server_does_not_adopt_the_prefilled_relays(self):
        self.assertFalse(
            self.out["untouchedEnabled"],
            "pressing Connect on the SERVER field turned on 'use my own relays' with a list the user "
            "never typed — the app's own suggestions, one of which can be down")
        self.assertIsNone(
            self.out["untouchedSaved"],
            f"and it saved them: {self.out['untouchedSaved']}")

    def test_an_actual_edit_is_still_carried_over_the_reload(self):
        """The whole reason _persistAuthRelays exists: the two settings are independent, so switching
        servers must not throw away relays typed just above it."""
        self.assertTrue(self.out["editedEnabled"], "a typed relay list was dropped on the reload")
        self.assertEqual(self.out["editedSaved"],
                         ["wss://relay.example.one", "wss://relay.example.two"])
