"""Startup apps on PosterChanOS: 'Users need a way to be able to define startup programs'.

Runs the shipped desktop/autostart.js under node against a throwaway HOME: entries are standard
~/.config/autostart .desktop files, they can be added (an installed app or a typed command), switched
off and on, and removed; the shell starts the enabled ones ONCE per login; and the desktop's own entry
is never started (it would open a second desktop on top of the first).
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "desktop", "autostart.js")
NODE = shutil.which("node")


@unittest.skipIf(not NODE, "no node")
class Autostart(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.run_dir = tempfile.mkdtemp()
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.bin)
        for prog in ("syncthing", "firefox"):
            p = os.path.join(self.bin, prog)
            open(p, "w").write("#!/bin/sh\n")
            os.chmod(p, 0o755)
        apps = os.path.join(self.home, ".local/share/applications")
        os.makedirs(apps)
        open(os.path.join(apps, "firefox.desktop"), "w").write(
            "[Desktop Entry]\nType=Application\nName=Firefox\nExec=firefox %u\nIcon=firefox\n")
        self.auto = os.path.join(self.home, ".config/autostart")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def js(self, body):
        env = {"HOME": self.home, "XDG_RUNTIME_DIR": self.run_dir, "PATH": self.bin + ":/usr/bin:/bin",
               "XDG_DATA_HOME": os.path.join(self.home, ".local/share"), "XDG_DATA_DIRS": "/nonexistent"}
        code = ("const A = require(%s); const env = %s;\n(async () => { const out = {};\n"
                "try { %s } catch(e){ out.threw = String(e.message || e); }\n"
                "process.stdout.write(JSON.stringify(out)); })();" % (json.dumps(MOD), json.dumps(env), body))
        r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30, env=dict(os.environ, **env))
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_a_command_and_an_installed_app_become_startup_entries(self):
        out = self.js("""A.add({name: 'Sync', exec: 'syncthing --no-browser'}, env);
                         A.addApp('firefox', env); out.list = A.list(env);""")
        self.assertNotIn("threw", out, out)
        names = {r["name"]: r for r in out["list"]}
        self.assertEqual(set(names), {"Sync", "Firefox"})
        self.assertTrue(all(r["enabled"] for r in out["list"]))
        text = open(os.path.join(self.auto, names["Firefox"]["id"])).read()
        self.assertIn("Exec=firefox %u", text)

    def test_switching_off_and_removing(self):
        out = self.js("""const id = A.add({name: 'Sync', exec: 'syncthing'}, env);
                         A.setEnabled(id, false, env); out.off = A.list(env)[0].enabled; out.runOff = A.toRun(env).length;
                         A.setEnabled(id, true, env); out.on = A.list(env)[0].enabled;
                         A.remove(id, env); out.after = A.list(env).length;""")
        self.assertEqual((out["off"], out["runOff"], out["on"], out["after"]), (False, 0, True, 0), out)

    def test_an_entry_from_another_program_is_honoured_including_its_off_switch(self):
        os.makedirs(self.auto)
        open(os.path.join(self.auto, "discord.desktop"), "w").write(
            "[Desktop Entry]\nType=Application\nName=Discord\nExec=syncthing\nX-GNOME-Autostart-enabled=false\n")
        open(os.path.join(self.auto, "gone.desktop"), "w").write(
            "[Desktop Entry]\nType=Application\nName=Gone\nExec=not-installed-anywhere\n")
        out = self.js("out.list = A.list(env); out.run = A.toRun(env).map(x => x.id);")
        self.assertEqual({r["name"]: r["enabled"] for r in out["list"]}, {"Discord": False, "Gone": True})
        self.assertEqual(out["run"], [], "an off entry ran, or a program that is not installed was started")

    def test_the_desktop_itself_is_never_started(self):
        os.makedirs(self.auto)
        open(os.path.join(self.auto, "posterchanai.desktop"), "w").write(
            "[Desktop Entry]\nType=Application\nName=PosterChan\nExec=syncthing\n")
        out = self.js("out.list = A.list(env); out.run = A.toRun(env); "
                      "try { A.remove('posterchanai.desktop', env); } catch(e){ out.refused = e.message; }")
        self.assertEqual(out["list"], [])
        self.assertEqual(out["run"], [])
        self.assertIn("not a startup app", out["refused"])

    def test_they_start_once_per_login(self):
        out = self.js("""A.add({name: 'Sync', exec: 'syncthing --no-browser'}, env);
                         const ran = []; const launch = async (argv) => { ran.push(argv); return {pid: 1}; };
                         out.first = await A.runOnce(launch, env, () => {});
                         out.second = await A.runOnce(launch, env, () => {});
                         out.ran = ran;""")
        self.assertEqual(out["ran"], [["syncthing", "--no-browser"]], out)
        self.assertEqual(out["second"], [], "a shell restart started every startup app again")

    def test_a_path_cannot_be_named(self):
        out = self.js("try { A.setEnabled('../../.bashrc', false, env); } catch(e){ out.refused = e.message; }")
        self.assertIn("not a startup app", out["refused"])


def test_the_shell_starts_them_through_the_start_menus_launcher():
    src = open(os.path.join(ROOT, "desktop", "main.js")).read()
    assert "async function launchCommand(argv, opts)" in src
    assert "return launchCommand(argv, opts);" in src, "pc:wm:launch no longer shares the launcher"
    i = src.index("app.whenReady().then(async () => {")
    assert "if(SHELL_MODE) setTimeout(() => { autostart.runOnce(launchAutostart)" in src[i:i + 6000]
