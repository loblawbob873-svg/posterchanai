"""The desktop app plays YouTube in the app: its main process names the embedding site, so the embed is not refused.

Reported 2026-10-27: "windows app: ... youtube videos open in firefox instead of playing". The app's page is app://,
Chromium sends YouTube's embed no Referer, YouTube answers "Error 153" -- so the client handed every video to the
system browser. desktop/main.js now adds a Referer to the embed requests (the configured instance, else
poster.place). Measured in a real headless Electron on 2026-10-27: no Referer -> Error 153; https://poster.place/ ->
plays; https://www.youtube.com/ -> Error 152-4.

The pure half runs the shipped functions under node. The LIVE half loads a real YouTube embed from an app:// page in
a real Electron through the shipped hook; it needs YouTube and an Electron binary, so it runs with PC_YT_LIVE=1.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text()


def _lift(name):
    m = re.search(r"^function %s\(.*?^}\n" % name, MAIN, re.S | re.M)
    assert m, f"{name} is gone from desktop/main.js"
    return m.group(0)


def _shipped():
    const = re.search(r"^const DEFAULT_INSTANCE = .*?;\n", MAIN, re.M).group(0)
    urls = re.search(r"^const YT_EMBED_URLS = .*?;\n", MAIN, re.M).group(0)
    return const + urls + _lift("ytEmbedReferer") + _lift("withYtReferer")


def test_the_referer_is_the_instance_or_poster_place_and_never_overrides_one():
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    js = _shipped() + r"""
console.log(JSON.stringify([
  withYtReferer({}, 'https://example.social'),            // the configured instance
  withYtReferer({}, 'https://example.social/'),           // trailing slash
  withYtReferer({}, ''),                                   // relays only: no instance
  withYtReferer({}, 'http://192.168.0.5:3051'),           // not an https site: the default
  withYtReferer({Referer: 'https://kept.example/'}, 'https://example.social'),
  YT_EMBED_URLS]));"""
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    a, b, c, d, e, urls = json.loads(out.stdout)
    assert a["Referer"] == b["Referer"] == "https://example.social/"
    assert c["Referer"] == d["Referer"] == "https://poster.place/"
    assert e["Referer"] == "https://kept.example/", "an existing Referer was overwritten"
    assert urls == ["https://www.youtube.com/embed/*", "https://www.youtube-nocookie.com/embed/*"]
    ses = MAIN[MAIN.index("const ses = session.defaultSession;"):][:400]
    assert "onBeforeSendHeaders({ urls: YT_EMBED_URLS }" in ses and "withYtReferer(d.requestHeaders, instance())" in ses
    app = (ROOT / "static/js/client/app.js").read_text()
    assert "location.protocol !== 'app:'" in app, "the desktop app still hands YouTube to the system browser"


@pytest.mark.skipif(os.environ.get("PC_YT_LIVE") != "1", reason="live: needs YouTube and an Electron binary (PC_YT_LIVE=1)")
def test_a_real_electron_plays_the_embed_through_the_shipped_hook(tmp_path):
    electron = next((p for p in (Path(os.environ.get("PC_ELECTRON_BINARY", "/nonexistent")),
                                 ROOT / "desktop/node_modules/electron/dist/electron",
                                 Path.home() / "posterchanai/desktop/node_modules/electron/dist/electron") if p.is_file()), None)
    if electron is None:
        pytest.skip("Electron runtime is required")
    app = tmp_path / "main.cjs"
    app.write_text(_shipped() + r"""
const { app, BrowserWindow, protocol, session } = require('electron');
protocol.registerSchemesAsPrivileged([{ scheme: 'app', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
app.whenReady().then(async () => {
  protocol.handle('app', () => new Response('<!doctype html><body><iframe src="https://www.youtube.com/embed/aqz-KE-bpKQ" width="640" height="360" allow="autoplay; encrypted-media"></iframe></body>', { headers: { 'content-type': 'text/html' } }));
  session.defaultSession.webRequest.onBeforeSendHeaders({ urls: YT_EMBED_URLS }, (d, cb) => cb({ requestHeaders: withYtReferer(d.requestHeaders, 'https://poster.place') }));
  const w = new BrowserWindow({ show: false, width: 800, height: 600, webPreferences: { offscreen: true } });
  await w.loadURL('app://posterchan/index.html');
  await new Promise(r => setTimeout(r, 12000));
  const f = w.webContents.mainFrame.frames[0];
  const txt = f ? await f.executeJavaScript("(document.body&&document.body.innerText||'').slice(0,200)+'|err:'+!!document.querySelector('.ytp-error')") : 'NO FRAME';
  console.log('RESULT ' + JSON.stringify(txt)); app.exit(0);
});""")
    out = subprocess.run([str(electron), "--no-sandbox", "--disable-gpu", "--ozone-platform=headless",
                          "--user-data-dir=" + str(tmp_path / "p"), str(app)], capture_output=True, text=True, timeout=90)
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, (out.stdout[-500:], out.stderr[-800:])
    txt = json.loads(line[7:])
    assert "err:false" in txt and "Error 15" not in txt, ("YouTube refused the embed", txt)
