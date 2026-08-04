"""The relay editor on the sign-in screen, IN A BROWSER.

Relays are where your posts live, and the control for them used to be in Settings — which is behind
the login. So wanting your own relays meant first signing in to somebody else's instance to go and
find the switch. The native builds got a pre-login chooser; the web did not, on the reasoning that a
browser has nothing to choose. That was true of the SERVER (the instance is the site you opened) and
wrong about the relays, which are a device setting and need no key at all.

So the row shows everywhere and the PANE varies. Both halves are asserted here because each is a way
to ship something that looks right:

  row-visible    hidden in a browser is the original bug.
  relays-filled  an empty box asks the user to already know which relays exist.
  no-server-sec  the server section must NOT appear in a browser: "Use no server" cannot mean anything
                 on a page that was served BY that server, and offering it is offering a broken choice.
  it-saves       the edit has to reach ClientSettings ('relays' + relaysEnabled), because connectRelays()
                 reads exactly that on the reload — this is the whole point of the screen.

Chrome renders the REAL templates/client.html with the same substitution build-www.sh does, minus the
bundle injection, so `__PC_API_BASE__` is undefined exactly as it is on the web. No server, no network.
"""
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

PROBE = r"""(() => {
  const q = s => document.querySelector(s);
  const vis = el => !!el && !el.classList.contains('hidden') && el.offsetParent !== null;
  // The web client starts in GUEST mode; the sign-in card is behind the guest prompt.
  const gl = q('#guest-login2') || q('#guest-signup'); if (gl) gl.click();
  const out = { bundled: typeof window.__PC_API_BASE__ !== 'undefined',
                rowVisible: vis(q('#auth-conn-row')),
                label: (q('#auth-conn-label') || {}).textContent || '' };
  const b = q('#btn-auth-conn'); if (b) b.click();
  out.paneOpen = vis(q('#auth-conn'));
  out.prefilled = ((q('#conn-relays') || {}).value || '').split('\n').filter(Boolean).length;
  out.serverSection = vis(q('#conn-server-sec'));
  const t = q('#conn-relays'); if (t) t.value = 'wss://relay.example.one\nwss://relay.example.two';
  const s = q('#btn-conn-relays'); if (s) s.click();
  try { const st = JSON.parse(localStorage.getItem('pc_nostr_settings') || '{}');
        out.saved = st.relays || null; out.enabled = st.relaysEnabled === true; }
  catch (e) { out.saved = 'ERR'; }
  out.err = (q('#conn-error') || {}).textContent || '';
  return out;
})()"""


def _render_shell():
    """templates/client.html as the SERVER would send it — the four Jinja values, no bundle shim."""
    with open(os.path.join(ROOT, "templates", "client.html"), encoding="utf-8") as fh:
        html = fh.read()
    html = re.sub(r"\{%\s*if not nostr_only\s*%\}(.*?)\{%\s*endif\s*%\}", r"\1", html, flags=re.S)
    html = re.sub(r"\{%\s*if nostr_only\s*%\}.*?\{%\s*endif\s*%\}", "", html, flags=re.S)
    html = re.sub(r"\{%\s*if secure\s*%\}.*?\{%\s*endif\s*%\}", "", html, flags=re.S)
    html = html.replace('{{ default_theme|default("cyberpunk") }}', "cyberpunk").replace("{{ ver }}", "1")
    return re.sub(r"\{\{.*?\}\}", "", html)


@unittest.skipUnless(CHROME and HAVE_WS, "needs chrome + websockets")
class AuthConnectionRowOnTheWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import asyncio

        cls.tmp = tempfile.mkdtemp(prefix="pcauthconn-")
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

        cdp = 9336
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

    def test_this_is_the_web_case(self):
        self.assertFalse(self.out["bundled"],
                         "__PC_API_BASE__ is defined — this rendered as a BUNDLE, so it proves nothing "
                         "about the browser")

    def test_the_row_is_there_before_login(self):
        self.assertTrue(self.out["rowVisible"],
                        "no relay control on the sign-in screen in a browser — the setting is then only "
                        "reachable from Settings, which is behind the login")
        self.assertIn("Relays", self.out["label"],
                      f"the row should name what it edits here, says {self.out['label']!r}")

    def test_the_pane_opens_prefilled(self):
        self.assertTrue(self.out["paneOpen"], "the chooser does not open")
        self.assertGreaterEqual(self.out["prefilled"], 2,
                                "the relay box is not pre-filled; an empty box asks the user to already "
                                "know which relays exist")

    def test_no_server_section_in_a_browser(self):
        self.assertFalse(self.out["serverSection"],
                         "the server section is showing in a browser — 'Use no server' cannot mean "
                         "anything on a page served BY that server")

    def test_the_edit_is_saved_where_the_relay_pool_reads_it(self):
        self.assertEqual(self.out["saved"], ["wss://relay.example.one", "wss://relay.example.two"],
                         f"relays did not persist ({self.out['saved']!r}); error was "
                         f"{self.out['err']!r}")
        self.assertTrue(self.out["enabled"],
                        "relaysEnabled stayed off, so connectRelays() would ignore the list and keep "
                        "using the built-in relay")
