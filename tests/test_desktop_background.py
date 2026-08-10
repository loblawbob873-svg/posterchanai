"""desktop/background.js — the tray, close-to-tray, and starting at login.

Run: venv-unified/bin/python -m pytest tests/test_desktop_background.py

Electron cannot run here (it needs a display), so the real module is driven under node with a stubbed
`electron`. That tests everything except the drawing.

Why each assertion exists — all of these fail SILENTLY, which is the whole problem with an autostart
feature: nobody watches their machine boot to check:

  appimage      Inside an AppImage, process.execPath is a binary in a TEMPORARY mount that will not
                exist at the next boot. A login item pointing there is written successfully, reported
                as on, and starts nothing forever. APPIMAGE is the only durable path.
  quoting       An unquoted Exec= with a space in the path (~/Downloads, "Program Files") starts
                nothing, silently, and the entry still looks correct in every settings UI.
  hidden        Autostart without --hidden puts a window on screen at every login. That is not a
                cosmetic problem: it is the thing that makes people turn the feature back off.
  removal       Turning it off has to DELETE the file. A .desktop left behind keeps starting the app.
  xdg           ~/.config is a default, not the answer — XDG_CONFIG_HOME moves it, and writing to the
                wrong place means the setting reads back as off on the very next call.
  no-tray       A desktop session with no status area must not get close-to-tray, or closing the
                window hides it with nothing anywhere to bring it back: an app you cannot quit and
                cannot reach.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP = os.path.join(ROOT, "desktop")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run_js(tmp_path, body, *, platform="linux", env=None, tray_fails=False, argv=None):
    """Drive desktop/background.js under node with electron stubbed.

    `platform` is forced with defineProperty because the module branches on it and this box is only
    ever one of the three.
    """
    stub = tmp_path / "electron-stub.js"
    stub.write_text("""
      const calls = { login: [], tray: [] };
      global.__calls = calls;
      class Tray {
        constructor(img){ if (global.__trayFails) throw new Error('no status area'); calls.tray.push(img); }
        setToolTip(){} setContextMenu(){} on(){} isDestroyed(){ return false; } destroy(){}
      }
      module.exports = {
        app: {
          setLoginItemSettings: (o) => { calls.login.push(o); global.__loginItem = o; },
          getLoginItemSettings: () => global.__loginItem || { openAtLogin: false },
        },
        Menu: { buildFromTemplate: (t) => ({ template: t }) },
        Tray,
        nativeImage: { createFromPath: () => ({ isEmpty: () => false, resize: (o) => ({ resized: o }) }) },
      };
    """)

    # A real electron IS installed in desktop/node_modules, and module resolution starts at the
    # requiring file's own directory — so a stub dropped in a temp node_modules loses to it. (In a
    # plain node process the real package exports a STRING, the path to the binary, so every
    # destructured name comes out undefined and the module under test appears to be broken.)
    # Intercepting the load is the only way to test the real file at its real path.
    entry = tmp_path / "entry.js"
    entry.write_text("""
      Object.defineProperty(process, 'platform', { value: %s });
      global.__trayFails = %s;
      const Module = require('module');
      const stub = require(%s);
      const load = Module._load;
      Module._load = function (req) { return req === 'electron' ? stub : load.apply(this, arguments); };
      const bg = require(%s);
      const out = (o) => { process.stdout.write(JSON.stringify(o)); };
      %s
    """ % (json.dumps(platform), "true" if tray_fails else "false", json.dumps(str(stub)),
           json.dumps(os.path.join(DESKTOP, "background.js")), body))

    e = dict(os.environ)
    e.pop("APPIMAGE", None)
    e.update(env or {})
    r = subprocess.run(["node", str(entry)] + (argv or []),
                       capture_output=True, text=True, cwd=str(tmp_path), env=e, timeout=60)
    if r.returncode != 0:
        raise AssertionError("node failed:\n%s\n%s" % (r.stdout, r.stderr))
    return json.loads(r.stdout) if r.stdout.strip() else None


class TestLinuxAutostart:
    def test_it_writes_and_removes_the_desktop_file(self, tmp_path):
        cfgdir = tmp_path / "cfg"
        env = {"XDG_CONFIG_HOME": str(cfgdir)}
        res = run_js(tmp_path, """
          const before = bg.getAutostart();
          bg.setAutostart(true);
          const on = bg.getAutostart();
          const body = require('fs').readFileSync(bg._linuxAutostartPath(), 'utf8');
          bg.setAutostart(false);
          out({ before, on, body, after: bg.getAutostart(), path: bg._linuxAutostartPath() });
        """, env=env)
        assert res["before"] is False
        assert res["on"] is True
        assert res["after"] is False, "turning autostart off must DELETE the file, not just report off"
        assert str(cfgdir) in res["path"], "XDG_CONFIG_HOME ignored — the setting would not read back"
        assert "[Desktop Entry]" in res["body"]

    def test_the_command_is_quoted_and_hidden(self, tmp_path):
        res = run_js(tmp_path, """
          bg.setAutostart(true);
          out({ body: require('fs').readFileSync(bg._linuxAutostartPath(), 'utf8') });
        """, env={"XDG_CONFIG_HOME": str(tmp_path / "cfg")})
        exec_line = [l for l in res["body"].split("\n") if l.startswith("Exec=")][0]
        assert exec_line.startswith('Exec="'), "an unquoted path with a space starts nothing: %r" % exec_line
        assert "--hidden" in exec_line, "autostart without --hidden shows a window at every login"

    def test_an_appimage_points_at_the_appimage(self, tmp_path):
        """The trap. execPath inside an AppImage is a temp mount that is gone by the next boot."""
        img = str(tmp_path / "My Apps" / "PosterChan.AppImage")
        res = run_js(tmp_path, """
          bg.setAutostart(true);
          out({ body: require('fs').readFileSync(bg._linuxAutostartPath(), 'utf8'),
                cmd: bg._launchCommand() });
        """, env={"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "APPIMAGE": img})
        assert res["cmd"]["exe"] == img
        assert ('Exec="%s"' % img) in res["body"]


class TestWindowsAndMacAutostart:
    def test_it_uses_the_login_item_api_with_hidden(self, tmp_path):
        res = run_js(tmp_path, """
          bg.setAutostart(true);
          out({ calls: global.__calls.login, on: bg.getAutostart() });
        """, platform="win32")
        assert len(res["calls"]) == 1
        call = res["calls"][0]
        assert call["openAtLogin"] is True
        assert "--hidden" in call["args"], "Windows ignores openAsHidden — the argument is what works"
        assert res["on"] is True

    def test_turning_it_off_is_reported_off(self, tmp_path):
        res = run_js(tmp_path, """
          bg.setAutostart(true); bg.setAutostart(false);
          out({ on: bg.getAutostart(), last: global.__calls.login.slice(-1)[0] });
        """, platform="darwin")
        assert res["last"]["openAtLogin"] is False
        assert res["on"] is False


class TestLaunchedHidden:
    def test_the_flag_is_read_from_argv(self, tmp_path):
        res = run_js(tmp_path, "out({ hidden: bg.launchedHidden(), plain: bg.launchedHidden(['node','x']) });",
                     argv=["--hidden"])
        assert res["hidden"] is True
        assert res["plain"] is False


class TestTray:
    def test_a_session_with_no_tray_reports_unavailable(self, tmp_path):
        """main.js only turns a window close into a hide when this is true. If a failed tray still
        reported available, closing the window would hide it with no way to get it back."""
        res = run_js(tmp_path, "bg.init({}); out({ available: bg.available() });", tray_fails=True)
        assert res["available"] is False

    def test_a_working_tray_reports_available(self, tmp_path):
        res = run_js(tmp_path, "bg.init({}); out({ available: bg.available() });")
        assert res["available"] is True

    def test_the_icon_is_resized(self, tmp_path):
        """A 512px source dropped into a 16px slot is drawn cropped on Windows — you get a corner."""
        res = run_js(tmp_path, "bg.init({}); out({ tray: global.__calls.tray });", platform="win32")
        assert res["tray"][0]["resized"] == {"width": 16, "height": 16}


class TestMainWiring:
    """Source-level checks on desktop/main.js.

    These assert wiring, not behaviour — main.js cannot be loaded without a real Electron. Each one
    covers a way the pieces above could be present and correct and still add up to an app that
    cannot be quit or cannot be seen.
    """

    def source(self):
        return open(os.path.join(DESKTOP, "main.js"), encoding="utf-8").read()

    def test_close_to_tray_requires_a_tray(self):
        assert "!background.available()" in self.source()

    def test_before_quit_sets_quitting(self):
        """role:quit and Cmd+Q reach before-quit and nothing else. Without this the close handler
        turns the quit's own window close into a hide and the app will not exit."""
        src = self.source()
        i = src.index("app.on('before-quit'")
        assert "quitting = true" in src[i:i + 200]

    def test_a_hidden_start_with_no_tray_still_shows(self):
        assert "if (!background.available() && !win.isVisible()) showWindow();" in self.source()


class TestItStaysAliveWhileOutOfSight:
    """The renderer half. A tray is pointless if the hidden window stops working.

    Both of these were real: the client treats `document.hidden` as "nobody is looking, save the
    battery", which is right for a phone in a pocket and wrong for a desktop app that has just been
    given a tray to hide in. Chromium also reports hidden for a window merely COVERED by another one,
    so both fired without anything being minimised at all — which is what "not showing new posts when
    other window is focused" was.

    Source assertions: neither file can be loaded here (one is a 25k-line IIFE that wants a DOM, the
    other wants localStorage and a relay). They prove the guard is present, not that it works.
    """

    def client(self, name):
        return open(os.path.join(ROOT, "static", "js", "client", name), encoding="utf-8",
                    errors="replace").read()

    def test_the_timeline_is_not_torn_down_in_the_desktop_app(self):
        src = self.client("app.js")
        i = src.index("_tlHideTimer = setTimeout")
        window = src[max(0, i - 1200):i]
        assert "_isDesktopApp()" in window, (
            "the 20s hidden-teardown must be skipped in the desktop app; without it a covered window "
            "stops receiving posts until it is raised again")

    def test_sync_still_sweeps_while_hidden_in_the_desktop_app(self):
        src = self.client("sync.js")
        assert "window.pcShell" in src and "_idle" in src, (
            "sync's hidden-check must exempt the desktop app, or closing to the tray — the whole "
            "reason autostart exists — is exactly what stops it syncing")
        assert "if(document.hidden) return;" not in src, (
            "a bare document.hidden guard is back in sync.js; use _idle() so the desktop app is exempt")
