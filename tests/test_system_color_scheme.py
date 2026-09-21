"""FIREFOX WAS LIGHT ON A DARK DESKTOP, because nothing ever told the portal the desktop is dark.

MEASURED on the laptop (192.168.0.154) with a private D-Bus session, gsettings on a /tmp keyfile
backend, a headless Wayfire and a throwaway Firefox 156 profile driven over Marionette — with the
session's own GTK_THEME=Adwaita:dark and GTK_APPLICATION_PREFER_DARK_THEME=1 in its environment:

    org.gnome.desktop.interface color-scheme   portal color-scheme   chromeColorSchemeIsDark  page prefers dark
    'default'  (what the machine had)           0                     false                    false
    'prefer-dark'                               1                     true                     true
    'prefer-light'                              2                     false                    false

Firefox's GTK look-and-feel reads the portal FIRST and maps 0 ("no preference") to LIGHT, which beats
the GTK theme — so exporting GTK_THEME, the only thing the session did, could never have worked.

Two halves, both run here rather than grepped:
  * the session SEEDS prefer-dark at login when the key is still 'default' (pc-shell-start-wayfire),
    and leaves any other value alone;
  * the desktop KEEPS it in step with the theme (app.js → pcOS.setColorScheme → main →
    desktop/colorscheme.js), only in the OS shell, and only writes on a difference.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "os" / "bin" / "pc-shell-start-wayfire"
PKG_LAUNCHER = ROOT / "os" / "overlay" / "app-misc" / "posterchanos-shell" / "files" / "pc-shell-start-wayfire"
COLORSCHEME = ROOT / "desktop" / "colorscheme.js"
MAIN = ROOT / "desktop" / "main.js"
PRELOAD = ROOT / "desktop" / "preload.js"
APP = ROOT / "static" / "js" / "client" / "app.js"
CSS = ROOT / "static" / "css" / "client.css"
NODE = shutil.which("node")


def _seed_function() -> str:
    src = LAUNCHER.read_text(encoding="utf-8")
    start = src.index("pc_seed_color_scheme() {")
    end = src.index("\n}\n", start) + 3
    return src[start:end]


class TheSessionSeedsDark(unittest.TestCase):
    """Runs the launcher's own function under sh with a stateful stub gsettings."""

    def _run(self, initial, *, bus=True):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = tmp / "run"; run.mkdir()
            sock = None
            if bus:
                sock = socket.socket(socket.AF_UNIX); sock.bind(str(run / "bus"))
            state = tmp / "value"; state.write_text(initial)
            log = tmp / "log"
            gs = tmp / "gsettings"
            gs.write_text(
                "#!/bin/sh\n"
                f'echo "$DBUS_SESSION_BUS_ADDRESS $*" >>"{log}"\n'
                f'case "$1" in get) printf "\'%s\'\\n" "$(cat "{state}")";; '
                f'set) printf "%s" "$4" >"{state}";; esac\n')
            gs.chmod(0o755)
            script = tmp / "seed.sh"
            script.write_text("set -u\n" + _seed_function() + "pc_seed_color_scheme\n")
            env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(run), "PC_GSETTINGS": str(gs)}
            r = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True, timeout=20)
            if sock:
                sock.close()
            self.assertEqual(r.returncode, 0, r.stderr)
            return state.read_text(), (log.read_text() if log.exists() else "")

    def test_an_unset_key_becomes_dark(self):
        value, log = self._run("default")
        self.assertEqual(value, "prefer-dark")
        self.assertIn("set org.gnome.desktop.interface color-scheme prefer-dark", log)

    def test_it_talks_to_this_sessions_bus(self):
        _, log = self._run("default")
        self.assertIn("unix:path=", log)
        self.assertIn("/run/bus", log.split()[0])

    def test_a_choice_is_left_alone(self):
        for choice in ("prefer-light", "prefer-dark"):
            with self.subTest(choice=choice):
                value, log = self._run(choice)
                self.assertEqual(value, choice)
                self.assertNotIn(" set ", log)

    def test_no_bus_means_no_write(self):
        """gsettings without a bus writes to a memory backend nothing else reads — a lie."""
        value, log = self._run("default", bus=False)
        self.assertEqual(value, "default")
        self.assertEqual(log, "")

    def test_the_launcher_calls_it_and_both_copies_agree(self):
        src = LAUNCHER.read_text(encoding="utf-8")
        self.assertRegex(src, r"\npc_seed_color_scheme\n")
        self.assertEqual(src, PKG_LAUNCHER.read_text(encoding="utf-8"))


@unittest.skipIf(not NODE, "node is unavailable")
class TheDesktopKeepsItInStep(unittest.TestCase):
    """desktop/colorscheme.js, run under node against a fake gsettings runner."""

    def _apply(self, scheme, current, fail=None):
        js = r"""
const cs = require(process.argv[1]);
let value = process.argv[3]; const calls = []; const fail = process.argv[4] || '';
const runner = (bin, args, opts, cb) => {
  calls.push([bin].concat(args).join(' '));
  if (fail === args[0]) return cb(new Error('boom'));
  if (args[0] === 'get') return cb(null, "'" + value + "'\n");
  value = args[3]; cb(null, '');
};
cs.apply(process.argv[2], runner, {XDG_RUNTIME_DIR: '/run/user/9'}).then(r =>
  console.log(JSON.stringify({r, value, calls})));
"""
        out = subprocess.run([NODE, "-e", js, str(COLORSCHEME), scheme, current, fail or ""],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_dark_theme_writes_prefer_dark(self):
        got = self._apply("dark", "default")
        self.assertEqual(got["value"], "prefer-dark")
        self.assertTrue(got["r"]["ok"] and got["r"]["changed"])

    def test_light_theme_writes_prefer_light(self):
        got = self._apply("light", "prefer-dark")
        self.assertEqual(got["value"], "prefer-light")

    def test_no_difference_no_write(self):
        """Every window applies the theme on load; a write is a broadcast to every GTK app."""
        got = self._apply("dark", "prefer-dark")
        self.assertFalse(got["r"]["changed"])
        self.assertEqual([c for c in got["calls"] if " set " in c], [])

    def test_nonsense_is_refused_before_anything_runs(self):
        got = self._apply("purple", "default")
        self.assertFalse(got["r"]["ok"])
        self.assertEqual(got["calls"], [])

    def test_a_failed_read_does_not_write(self):
        got = self._apply("dark", "default", fail="get")
        self.assertFalse(got["r"]["ok"])
        self.assertEqual(got["value"], "default")

    def test_the_bus_is_found_when_not_inherited(self):
        js = ("const cs=require(process.argv[1]);"
              "console.log(cs.envFor({XDG_RUNTIME_DIR:'/run/user/7'}).DBUS_SESSION_BUS_ADDRESS)")
        out = subprocess.run([NODE, "-e", js, str(COLORSCHEME)], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "unix:path=/run/user/7/bus")


class TheWiring(unittest.TestCase):
    def test_only_the_os_shell_writes_the_machine_setting(self):
        main = MAIN.read_text(encoding="utf-8")
        i = main.index("ipcMain.handle('pc:os:color-scheme'")
        body = main[i:main.index("});", i)]
        self.assertIn("fsGuard(e)", body)
        self.assertIn("!SHELL_MODE", body, "a Windows/GNOME user's theme pick would rewrite their desktop")
        self.assertIn("require('./colorscheme').apply(", body)

    def test_preload_exposes_it(self):
        self.assertIn("setColorScheme: (scheme) => ipcRenderer.invoke('pc:os:color-scheme'",
                      PRELOAD.read_text(encoding="utf-8"))

    def test_the_client_mirrors_every_theme_change_and_once_at_boot(self):
        app = APP.read_text(encoding="utf-8")
        apply_fn = app[app.index("function applyTheme(slug, persist){"):app.index("function _mirrorColorScheme(){")]
        self.assertIn("_mirrorColorScheme();", apply_fn)
        mirror = app[app.index("function _mirrorColorScheme(){"):]
        mirror = mirror[:mirror.index("\n  _mirrorColorScheme();")]
        # The stylesheet's color-scheme group is the single source of truth — no slug list here.
        self.assertIn("getComputedStyle(document.documentElement).colorScheme", mirror)
        self.assertNotRegex(mirror, r"'(cherryblossom|professional|win98|winxp)'")
        self.assertRegex(app, r"\n  _mirrorColorScheme\(\);\n", "never pushed at boot")

    def test_the_stylesheet_says_dark_by_default(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(":root{color-scheme:dark}", css)
        self.assertRegex(css, r'data-theme="professional"\][^{]*\{color-scheme:light\}|'
                              r'data-theme="professional"\],[^{]*\{color-scheme:light\}')


if __name__ == "__main__":
    unittest.main()
