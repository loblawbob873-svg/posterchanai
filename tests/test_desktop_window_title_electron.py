"""The Windows/desktop app's window is called "PosterChan Desktop", whatever the page calls itself.

2026-10-26: "Windows App says PosterChan - Nostr.. we need better name. PosterChan Desktop." The window took its page's
<title> -- the web client's tab title -- so the taskbar and Alt-Tab said "PosterChan · Nostr". desktop/main.js's
appWindowTitle/keepAppTitle are lifted out of the shipped file and bound to a REAL Electron window loading a page with
that title, which then changes it twice (an unread count, then something else): the window must read "PosterChan
Desktop", keeping the count. Skips where there is no Electron runtime (as the native IPC test does).
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_native_window_reload_electron import _native_popen, _stop_native


def _missing_runtime(reason):
    # A plain skip, never the native IPC test's PC_REQUIRE_NATIVE_IPC_TEST failure: that flag belongs to that test, and
    # the node half below runs this file's logic everywhere (the gate has no display for Electron).
    pytest.skip(reason)

ROOT = Path(__file__).resolve().parents[1]


def _lift(src, name):
    m = re.search(r"^function %s\(.*?^}\n" % name, src, re.S | re.M)
    assert m, f"{name} is gone from desktop/main.js"
    return m.group(0)


def test_the_window_is_posterchan_desktop_and_keeps_an_unread_count(tmp_path):
    electron = next((p for p in (Path(os.environ.get("PC_ELECTRON_BINARY", "/nonexistent")),
                                 ROOT / "desktop/node_modules/electron/dist/electron") if p.is_file()), None)
    if electron is None:
        _missing_runtime("Electron runtime is required")
    main = (ROOT / "desktop/main.js").read_text()
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><title>PosterChan · Nostr</title><body>hi<script>"
                    "setTimeout(()=>document.title='(3) PosterChan · Nostr',300);"
                    "setTimeout(()=>document.title='Something else entirely',700);</script>")
    app = tmp_path / "app.cjs"
    app.write_text(_lift(main, "appWindowTitle") + _lift(main, "keepAppTitle") + r"""
const {app, BrowserWindow} = require('electron');
app.whenReady().then(async () => {
  const w = new BrowserWindow({show:false, title:'PosterChan Desktop', width:400, height:300});
  keepAppTitle(w);
  const seen = [];
  await w.loadFile(%s);
  seen.push(w.getTitle());
  await new Promise(r => setTimeout(r, 500)); seen.push(w.getTitle());
  await new Promise(r => setTimeout(r, 500)); seen.push(w.getTitle());
  console.log('TITLES ' + JSON.stringify(seen));
  app.exit(0);
});
""" % json.dumps(str(page)))
    (tmp_path / "profile").mkdir()

    def run(platform, env):
        proc = _native_popen([str(electron), "--no-sandbox", "--disable-gpu", "--ozone-platform=" + platform,
                              "--user-data-dir=" + str(tmp_path / "profile"), str(app)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            return proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            _stop_native(proc)
            pytest.fail("Electron did not finish")

    if os.environ.get("DISPLAY"):
        out, err = run("x11", dict(os.environ))
    else:                       # no X: a headless sway is a display (scripts/swayheadless.py)
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import swayheadless
        ok, why = swayheadless.available()
        if not ok:
            _missing_runtime("no display for Electron: " + why)
        rundir = Path(os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid())
        before = set(rundir.glob("wayland-*"))
        with swayheadless.headless_sway():
            fresh = sorted((p for p in rundir.glob("wayland-*") if p not in before and not p.name.endswith(".lock")),
                           key=lambda p: p.stat().st_mtime)
            if not fresh:
                _missing_runtime("the headless sway opened no wayland socket")
            env = dict(os.environ, WAYLAND_DISPLAY=fresh[-1].name, XDG_RUNTIME_DIR=str(rundir))
            env.pop("DISPLAY", None)
            out, err = run("wayland", env)
    line = next((x for x in out.splitlines() if x.startswith("TITLES ")), None)
    assert line, (out[-500:], err[-800:])
    assert json.loads(line[7:]) == ["PosterChan Desktop", "(3) PosterChan Desktop", "PosterChan Desktop"]


def test_the_shipped_title_code_under_node():
    """The same two functions, run by node against a window that emits page-title-updated the way Electron does -- so
    this holds even where no display exists to start Electron (server1 has none)."""
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    main = (ROOT / "desktop/main.js").read_text()
    js = _lift(main, "appWindowTitle") + _lift(main, "keepAppTitle") + r"""
const {EventEmitter} = require('events');
const w = Object.assign(new EventEmitter(), {title:'PosterChan Desktop', setTitle(t){ this.title=t; }});
keepAppTitle(w);
const seen = [];
for(const t of ['PosterChan · Nostr', '(3) PosterChan · Nostr', '(12) Bookmarks', 'Something else']){
  let prevented = false;
  w.emit('page-title-updated', {preventDefault(){ prevented = true; }}, t);
  seen.push([w.title, prevented]);
}
console.log(JSON.stringify(seen));
"""
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [["PosterChan Desktop", True], ["(3) PosterChan Desktop", True],
                                      ["(12) PosterChan Desktop", True], ["PosterChan Desktop", True]]
    # the window is created with that title, in the desktop app and in the OS shell alike
    win = main[main.index("function createWindow("):]
    assert "title: 'PosterChan Desktop'," in win[:win.index("webPreferences")]
