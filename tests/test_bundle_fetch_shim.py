"""A bundled app must read its OWN assets from the bundle, never from the instance.

Run: venv-unified/bin/python -m unittest tests.test_bundle_fetch_shim

Both build-www.sh scripts inject a `window.fetch` shim that rewrites root-relative URLs to whichever
instance the app talks to. That is right for `/api/...` and wrong for `/static/...`: the bundle
serves its own assets at exactly those paths.

The omission hid for as long as the APK has existed because nearly every bundled asset is pulled in
by a <script>/<link>/@font-face — the WebView resolves those against the page and the shim never
sees them. The translation catalogues are the exception: i18n.js `fetch()`es
`/static/i18n/<lang>.json`, so in the APK that became a cross-origin request to poster.place, where
static files carry no Access-Control-Allow-Origin (and the shim forces `credentials:'include'`,
which not even a wildcard would satisfy). It failed as a TypeError, i18n.js caught it, and the app
said "could not load that language — staying in English" with the file sitting in the bundle.

So this RUNS each shipped shim under node against a stub fetch and asserts where the request went.
A grep for `isLocal` would pass on a guard wired into the wrong branch.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node") or shutil.which("nodejs")

INSTANCE = "https://example-instance.test"

# Bundled assets (must stay local) and server calls (must be rewritten). The catalogues are first
# because they are the ones that were broken.
LOCAL = ["/static/i18n/ar.json", "/static/i18n/ja.json?v=123",
         "/static/js/client/app.js", "/static/css/client.css", "/sw.js"]
REMOTE = ["/api/auth/settings", "/client/config", "/blossom/abc", "/apk/version"]


def shim_js(script):
    """The shim exactly as the build script writes it into index.html."""
    with open(os.path.join(ROOT, script), encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"^shim = ('''.*?''')", src, re.S | re.M)
    if not m:
        raise AssertionError(f"{script}: could not find the injected shim")
    js = ast.literal_eval(m.group(1))            # the build script's own unescaping, exactly
    js = js.replace("__BUILD__", "0")            # mobile bakes the build number in
    return re.sub(r"</?script>", "", js)


def run_shim(script):
    js = shim_js(script)
    urls = json.dumps(LOCAL + REMOTE)
    prog = f"""
const seen = [];
globalThis.window = globalThis;
globalThis.localStorage = {{ getItem: () => {json.dumps(INSTANCE)}, setItem: () => {{}} }};
globalThis.document = {{ addEventListener: () => {{}}, body: {{ classList: {{ add: () => {{}} }} }} }};
globalThis.WebSocket = function(){{}};
globalThis.WebSocket.prototype = {{}};
// The desktop shim reads its instance from the preload bridge, the APK from localStorage.
globalThis.pcShell = {{ instanceSync: {json.dumps(INSTANCE)} }};
globalThis.fetch = function(i, o){{ seen.push(typeof i === 'string' ? i : (i && i.url)); return Promise.resolve(); }};
{js}
const out = {{}};
for (const u of {urls}) {{
  seen.length = 0;
  try {{ window.fetch(u, {{}}); }} catch (e) {{ seen.push('THREW: ' + e.message); }}
  out[u] = seen[0];
}}
console.log(JSON.stringify(out));
"""
    r = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(f"{script}: the shim did not run under node: {r.stderr[-500:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node is not installed")
class BundleFetchShim(unittest.TestCase):
    def _check(self, script):
        got = run_shim(script)
        for u in LOCAL:
            self.assertEqual(
                got[u], u,
                f"{script}: {u} was sent to {got[u]!r} — a bundled asset must be read from the "
                f"bundle. This is the bug that left the APK unable to load the Arabic and Japanese "
                f"catalogues.")
        for u in REMOTE:
            self.assertEqual(
                got[u], INSTANCE + u,
                f"{script}: {u} stayed root-relative — a server call must reach the instance.")

    def test_apk_shim_keeps_bundled_assets_local(self):
        self._check(os.path.join("mobile", "build-www.sh"))

    def test_desktop_shim_keeps_bundled_assets_local(self):
        self._check(os.path.join("desktop", "build-www.sh"))


if __name__ == "__main__":
    unittest.main()
