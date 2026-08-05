"""Every tab in the popup must actually reveal its panel.

This exists because two features shipped that could not be reached. `show()` toggled `hidden` on a
HARDCODED list of three pane ids, so adding a pane plus a button that calls show() for it looked
complete and did nothing: Bookmarks and Relays each opened onto a blank popup. Sites had been doing
the same for longer — its button passes an id that was never in that list either.

Nothing about this fails loudly. There is no console error, no exception, no missing element: a panel
simply shows nothing. I then spent a long time diagnosing why bookmark sync published nothing, when
the truth was that the toggle to switch it on had never been visible, and the "Sync" being pressed was
the vault's relay-sync button in the footer.

So: load the real popup.html in a browser, click every tab, and require the matching pane to become
visible. The extension APIs are stubbed — this asserts nothing about what the buttons DO, only that
the panel a person needs is on the screen.
"""
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")
CHROME = shutil.which("google-chrome-stable")
try:
    import websockets  # noqa: F401
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

# tab button id -> the pane it must reveal
TABS = {
    "sites-tab": "pane-sites",
    "bm-tab": "pane-bm",
    "relay-tab": "pane-relays",
}

# Enough of the WebExtension surface for popup.js to evaluate and boot. Every message resolves to an
# empty object, so the popup lands on its unpaired state — which is fine: the panes are still there.
STUB = """
window.__sent = [];
window.chrome = {
  runtime: { sendMessage: async (m) => { window.__sent.push(m && m.type); return {}; },
             getManifest: () => ({ version: '0.0.0-test' }), lastError: null },
  storage: { local: { get: async () => ({}), set: async () => {} } },
  tabs: { query: async () => [{ id: 1, url: 'https://example.com/' }], sendMessage: async () => {} },
  permissions: { request: async () => true, contains: async () => true },
};
"""


@pytest.mark.skipif(not (CHROME and HAVE_WS), reason="needs chrome + websockets")
def test_every_tab_reveals_its_pane():
    import asyncio

    tmp = tempfile.mkdtemp(prefix="pcpopup-")
    try:
        for f in ("popup.html", "popup.js", "popup.css", "vaultcore.js"):
            src = os.path.join(EXT, f)
            if os.path.isfile(src):
                shutil.copy(src, tmp)
        # The stub has to run BEFORE popup.js, which reads `browser ?? chrome` at its first line.
        with open(os.path.join(tmp, "popup.html"), encoding="utf-8") as fh:
            html = fh.read()
        html = html.replace("<script src=", f"<script>{STUB}</script>\n<script src=", 1)
        with open(os.path.join(tmp, "popup.html"), "w", encoding="utf-8") as fh:
            fh.write(html)

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=tmp, **k)

            def log_message(self, *a):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        cdp, profile = 9355, os.path.join(tmp, "chrome")
        subprocess.run(["pkill", "-f", f"remote-debugging-port={cdp}"], capture_output=True)
        proc = subprocess.Popen(
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
            assert page, "chrome did not start"
            async with ws_mod.connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as ws:
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
                await call("Page.navigate", {"url": f"http://127.0.0.1:{port}/popup.html"})
                await asyncio.sleep(3)
                expr = """(() => {
                  const vis = (id) => { const e = document.getElementById(id);
                    return !!e && !e.classList.contains('hidden'); };
                  const out = {};
                  for (const [tab, pane] of Object.entries(%s)) {
                    const b = document.getElementById(tab);
                    out[tab] = { button: !!b, pane: !!document.getElementById(pane), shown: false };
                    if (!b) continue;
                    b.click();
                    out[tab].shown = vis(pane);
                  }
                  return out;
                })()""" % json.dumps(TABS)
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                if r.get("exceptionDetails"):
                    raise AssertionError("popup threw: " + str(r["exceptionDetails"].get("text")))
                panes = r["result"]["value"]

                # …and that the controls inside those panes actually TALK to the background. A pane
                # that renders and whose buttons go nowhere is the same failure one step later.
                r2 = await call("Runtime.evaluate", {"expression": '''(async () => {
                  window.__sent = [];
                  document.getElementById('bm-tab').click();
                  document.getElementById('bm-on').checked = true;
                  document.getElementById('bm-on').dispatchEvent(new Event('change'));
                  document.getElementById('relay-tab').click();
                  document.getElementById('relay-list').value = 'wss://relay.poster.place';
                  document.getElementById('relay-save').click();
                  await new Promise(r => setTimeout(r, 400));
                  return window.__sent;
                })()''', "returnByValue": True, "awaitPromise": True})
                return panes, (r2["result"]["value"] or [])

        out, sent = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(drive())
        proc.terminate()

        for tab, pane in TABS.items():
            st = out[tab]
            assert st["button"], f"#{tab} is missing from popup.html"
            assert st["pane"], f"#{pane} is missing from popup.html"
            assert st["shown"], (
                f"clicking #{tab} did not reveal #{pane} — the panel is in the DOM and nothing shows "
                "it, which looks exactly like a feature that does not work")

        assert "bm-enable" in sent, (
            "the bookmark toggle did not message the background; the pane renders and the switch "
            f"does nothing (messages seen: {sent})")
        assert "relays-set" in sent, (
            f"saving relays did not message the background (messages seen: {sent})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_edit_pane_exists_and_is_wired():
    """Editing was the gap: the popup could fill, generate and show a one-time code, but correcting a
    username meant opening the app.

    The browser test above proves the panes are reachable; these are the two things about EDITING that
    fail silently — a form that never sends, and a save that is not authoritative. `full: true` is what
    tells the writer an emptied box means CLEAR: the save bar's backfill exists because that bar knows
    only a username and a password, and applying it to an edit makes deleting a note impossible (you
    clear it, save, and it comes back)."""
    html = open(os.path.join(EXT, "popup.html"), encoding="utf-8").read()
    js = open(os.path.join(EXT, "popup.js"), encoding="utf-8").read()
    bg = open(os.path.join(EXT, "background.js"), encoding="utf-8").read()

    for el in ("pane-edit", "ed-title", "ed-user", "ed-pass", "ed-url", "ed-totp", "ed-notes", "ed-save"):
        assert f'id="{el}"' in html, f"the edit form is missing #{el}"
    assert 'data-a="edit"' in js or 'data-a="edit"' in html, "no Edit action on a row"
    assert "type:'save', item, full:true" in js, "the edit form does not save authoritatively"
    assert "async function saveItem(item, full){" in bg
    # Braced branches, checked by behaviour in the test below; here just that the split exists.
    assert "if(full){" in bg and "} else {" in bg, \
        "saveItem no longer distinguishes an authoritative edit from a save-bar update"


def test_an_edit_clears_and_a_save_bar_preserves():
    """The two writers share one merge, and they need opposite behaviour from it.

    The save bar knows only a username and a password, so an empty field there means "I don't know
    it" — backfilled, or updating a rotated password wipes the TOTP secret. The edit form shows every
    field it changes, so an empty field means CLEAR, and the website list is authoritative or a typo
    in a URL can never be corrected (the union keeps the wrong one forever).

    Reviewing this found a dangling `else`: unbraced, it bound to the inner `if` inside the loop, so
    `full` did nothing and a partial save reassigned created/src once per key. Harmless, and not what
    the code said — which is the kind of thing that is true until somebody edits near it."""
    import subprocess as sp
    src = open(os.path.join(EXT, "background.js"), encoding="utf-8").read()
    assert "if(full){" in src and "} else {" in src, "the merge branches are unbraced again"

    out = sp.run(["node", "-e", """
      const fs = require('fs');
      const src = fs.readFileSync(process.argv[1], 'utf8');
      const m = src.match(/const merged = Object\\.assign\\(\\{\\}, prev, item\\);[\\s\\S]*?item = merged;/);
      const merge = new Function('prev','item','full',
        'const merged=Object.assign({},prev,item);' +
        m[0].replace('const merged = Object.assign({}, prev, item);','') + 'return merged;');
      const prev = { uris:['https://old.example/'], totp:'SECRET', notes:'keep', created:1, src:'app' };
      const edit = { uris:['https://new.example/'], totp:'', notes:'' };
      console.log(JSON.stringify({ bar: merge(prev, edit, false), full: merge(prev, edit, true) }));
    """, os.path.join(EXT, "background.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)

    assert r["bar"]["totp"] == "SECRET", "the save bar wiped a TOTP secret it never knew about"
    assert r["bar"]["notes"] == "keep"
    assert len(r["bar"]["uris"]) == 2, "the save bar should ADD a URL, not replace the known one"

    assert r["full"]["totp"] == "", "an edit could not clear the one-time code secret"
    assert r["full"]["notes"] == "", "an edit could not clear the notes"
    assert r["full"]["uris"] == ["https://new.example/"], \
        "an edit could not correct a wrong website — the old one is kept alongside it"
    assert r["full"]["created"] == 1 and r["full"]["src"] == "app", "provenance must not be editable"
