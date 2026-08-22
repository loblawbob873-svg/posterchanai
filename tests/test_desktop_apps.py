"""Every app installed on the machine, in the start menu — and only the ones that belong there.

The launcher offered three hardcoded entries and a comment arguing that a menu scraped from
/usr/share/applications is the thing PosterChanOS exists not to be. That argument was about a menu
of ninety unusable entries, and the answer is the SPEC, not refusing to look: measured on the test
laptop, 19 .desktop files of which FIVE belong in a menu, and every one of the other fourteen says
so in its own file — NoDisplay, Hidden, a Type that is not Application, an OnlyShowIn naming a
desktop this is not.

The parser is RUN under node against real .desktop text. Every failure here is silent in the same
way: a menu that is either full of things that cannot be clicked, or missing the program somebody
installed, with nothing anywhere to say why.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "desktop", "apps.js")
OS_UI = os.path.join(ROOT, "static", "js", "client", "os.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "needs node")
class DesktopEntries(unittest.TestCase):
    def js(self, body):
        src = ("const A = require(%s);\nconst out = {};\n%s\n"
               "process.stdout.write(JSON.stringify(out));" % (json.dumps(MOD), body))
        r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-900:])
        return json.loads(r.stdout)

    def test_opening_start_forces_a_fresh_installed_app_scan(self):
        src = open(OS_UI, encoding="utf-8").read()
        self.assertIn("PCOSShell.allApps(true)", src,
                      "apps installed since the previous Start opening remain invisible")

    # ---- Exec is not a command line you can split on spaces ------------------------------------
    def test_field_codes_are_removed_not_passed_through(self):
        """`Exec=firefox %u` means "firefox, and a URL here if you have one". Passed through, firefox
        opens a tab for a file literally named `%u`. A launcher click carries no URL."""
        out = self.js("out.a = A.execArgv('/usr/bin/firefox %u');"
                      "out.b = A.execArgv('/usr/bin/gimp-2.10 %U');"
                      "out.c = A.execArgv('env FOO=1 thing %f --flag');")
        self.assertEqual(out["a"], ["/usr/bin/firefox"])
        self.assertEqual(out["b"], ["/usr/bin/gimp-2.10"])
        self.assertEqual(out["c"], ["env", "FOO=1", "thing", "--flag"])

    def test_an_escaped_percent_survives(self):
        out = self.js("out.a = A.execArgv('thing %%literal %i');")
        self.assertEqual(out["a"], ["thing", "%literal"])

    def test_a_quoted_path_with_a_space_stays_one_argument(self):
        """`split(' ')` turns /opt/My App/run into two arguments and starts nothing."""
        out = self.js("""out.a = A.execArgv('"/opt/My App/run" --flag %F');""")
        self.assertEqual(out["a"], ["/opt/My App/run", "--flag"])

    # ---- what belongs in a menu ---------------------------------------------------------------
    def test_the_fourteen_that_do_not_belong_are_all_marked(self):
        """Measured on the real machine. Every one of these is in /usr/share/applications and none
        of them is a program anybody would pick from a menu — and each says so in its own file."""
        cases = [
            ("NoDisplay", "[Desktop Entry]\nType=Application\nName=Pinentry\nExec=/usr/bin/p\nNoDisplay=true\n"),
            ("Hidden", "[Desktop Entry]\nType=Application\nName=Old\nExec=/usr/bin/p\nHidden=true\n"),
            ("not an application", "[Desktop Entry]\nType=Link\nName=A site\nURL=https://x\n"),
            ("nothing to run", "[Desktop Entry]\nType=Application\nName=Stub\n"),
            ("no Name", "[Desktop Entry]\nType=Application\nExec=/usr/bin/p\n"),
        ]
        for want, text in cases:
            out = self.js("out.v = A.menuable(A.parseEntry(%s));" % json.dumps(text))
            self.assertFalse(out["v"]["ok"], f"{want}: a {want} entry was offered in the menu")
            self.assertIn(want.split()[0], out["v"]["why"])

    def test_an_entry_for_another_desktop_is_not_shown_here(self):
        gnome = "[Desktop Entry]\nType=Application\nName=G\nExec=/usr/bin/g\nOnlyShowIn=GNOME;\n"
        ours = "[Desktop Entry]\nType=Application\nName=S\nExec=/usr/bin/s\nOnlyShowIn=sway;GNOME;\n"
        notus = "[Desktop Entry]\nType=Application\nName=N\nExec=/usr/bin/n\nNotShowIn=sway;\n"
        out = self.js("out.g = A.menuable(A.parseEntry(%s));"
                      "out.s = A.menuable(A.parseEntry(%s));"
                      "out.n = A.menuable(A.parseEntry(%s));"
                      % (json.dumps(gnome), json.dumps(ours), json.dumps(notus)))
        self.assertFalse(out["g"]["ok"])
        self.assertTrue(out["s"]["ok"], "an entry that names sway was hidden from sway")
        self.assertFalse(out["n"]["ok"])

    def test_a_desktop_ACTION_does_not_overwrite_the_app(self):
        """A file's `[Desktop Action …]` groups carry their own Name and Exec. Read as part of the
        entry they replace the app's, so "Firefox" becomes "Open a New Private Window" and the menu
        launches the wrong thing."""
        text = ("[Desktop Entry]\nType=Application\nName=Firefox\nExec=/usr/bin/firefox %u\n"
                "Actions=new-private-window;\n\n"
                "[Desktop Action new-private-window]\nName=Open a New Private Window\n"
                "Exec=/usr/bin/firefox --private-window %u\n")
        out = self.js("out.e = A.parseEntry(%s);" % json.dumps(text))
        self.assertEqual(out["e"]["Name"], "Firefox")
        self.assertEqual(out["e"]["Exec"], "/usr/bin/firefox %u")

    def test_a_localized_name_does_not_replace_the_plain_one(self):
        text = "[Desktop Entry]\nType=Application\nName=Files\nName[de]=Dateien\nExec=/usr/bin/f\n"
        out = self.js("out.e = A.parseEntry(%s);" % json.dumps(text))
        self.assertEqual(out["e"]["Name"], "Files")

    def test_games_are_grouped_as_games(self):
        """Asked for by name: "any game/app under PosterChan Desktop"."""
        out = self.js("out.g = A.groupOf({Categories:'Game;ActionGame;'});"
                      "out.m = A.groupOf({Categories:'AudioVideo;Player;'});"
                      "out.o = A.groupOf({});")
        self.assertEqual(out["g"], "Games")
        self.assertEqual(out["m"], "Media")
        self.assertEqual(out["o"], "Other")

    # ---- the directories ----------------------------------------------------------------------
    def test_the_users_own_entries_win_over_the_systems(self):
        """A copy in ~/.local/share/applications REPLACES the system one — that is what the
        desktop-file ID is for. Appearing beside it means the menu has the app twice, one of which
        does the wrong thing."""
        out = self.js("""
          const env = { HOME: '/home/x', XDG_DATA_DIRS: '/usr/share' };
          out.dirs = A.appDirs(env);
          out.id = A.entryId('/usr/share/applications', '/usr/share/applications/kde/foo.desktop');
        """)
        self.assertEqual(out["dirs"][0], "/home/x/.local/share/applications")
        self.assertIn("/usr/share/applications", out["dirs"])
        self.assertEqual(out["id"], "kde-foo")

    def test_a_machine_with_no_XDG_variables_still_finds_its_apps(self):
        """Reading the environment alone finds nothing on a machine that sets neither variable, and
        every such machine still has /usr/share/applications."""
        out = self.js("out.dirs = A.appDirs({});")
        self.assertIn("/usr/share/applications", out["dirs"])

    # ---- end to end, against real files on a real disk ------------------------------------------
    def test_a_real_directory_scans_to_only_what_belongs(self):
        d = tempfile.mkdtemp(prefix="apps-")
        apps = os.path.join(d, "applications")
        os.makedirs(apps)
        sh = shutil.which("sh") or "/bin/sh"
        files = {
            "good.desktop": f"[Desktop Entry]\nType=Application\nName=Good App\nExec={sh} -c true\n"
                            f"Categories=Game;\nComment=plays\n",
            "hidden.desktop": f"[Desktop Entry]\nType=Application\nName=Hidden\nExec={sh}\nNoDisplay=true\n",
            "gone.desktop": "[Desktop Entry]\nType=Application\nName=Gone\n"
                            "Exec=/nonexistent/bin/gone\n",
            "tryexec.desktop": f"[Desktop Entry]\nType=Application\nName=Try\nExec={sh}\n"
                               "TryExec=/nonexistent/bin/nope\n",
            "link.desktop": "[Desktop Entry]\nType=Link\nName=Site\nURL=https://x\n",
        }
        for n, t in files.items():
            open(os.path.join(apps, n), "w").write(t)
        out = self.js("out.r = A.scan({ dirs: [%s], env: { PATH: %s } });"
                      % (json.dumps(apps), json.dumps(os.environ.get("PATH", "/bin:/usr/bin"))))
        names = [a["name"] for a in out["r"]["apps"]]
        self.assertEqual(names, ["Good App"],
                         "the scan offered entries that cannot be clicked, or dropped one that can")
        app = out["r"]["apps"][0]
        self.assertEqual(app["group"], "Games")
        self.assertEqual(app["argv"][0], sh)
        self.assertEqual(len(out["r"]["skipped"]), 4)
        # An Exec whose program is not on this machine is a menu entry that can only disappoint —
        # and most files carry no TryExec at all, so it has to be checked directly too.
        why = " ".join(s["why"] for s in out["r"]["skipped"])
        self.assertIn("not installed", why)
        shutil.rmtree(d, ignore_errors=True)

    def test_the_check_can_fail(self):
        """The scan's whole value is the filtering; a version that returned everything would pass
        every test above that only asserts a good entry is present."""
        out = self.js("""
          const e = A.parseEntry('[Desktop Entry]\\nType=Application\\nName=X\\nExec=/bin/x\\nNoDisplay=true\\n');
          out.strict = A.menuable(e).ok;
          out.lax = ((e.Type || 'Application') === 'Application');   // the naive version
        """)
        self.assertFalse(out["strict"])
        self.assertTrue(out["lax"], "the naive check would have shown it — so the guard is doing work")


    def test_a_daemon_is_not_offered_as_an_app(self):
        """A DAEMON HAS NO WINDOW, EVER, and the spec has no field that says so.

        `foot-server.desktop` runs `foot --server`: a background process that draws nothing and
        waits for clients. It passes every other check -- an Application, not NoDisplay, installed
        -- so it reached the start menu, and clicking it opened a frame that waited twenty seconds
        for a window that was never coming and then said "Foot Server did not open -- is it
        installed?". It IS installed. Reported as a menu entry that gives "a black screen with a
        circle".
        """
        srv = ("[Desktop Entry]\nType=Application\nName=Foot Server\n"
               "Exec=foot --server\n")
        out = self.js("out.v = A.menuable(A.parseEntry(%s));" % json.dumps(srv))
        self.assertFalse(out["v"]["ok"], "a daemon is still offered in the menu")
        self.assertIn("daemon", out["v"]["why"])

    def test_an_ordinary_app_is_untouched_by_the_daemon_rule(self):
        """Matched on whole ARGUMENTS, so a program whose path merely contains the word stays."""
        for exec_line in ("Exec=/opt/serverview/bin/view %U",
                          "Exec=firefox --new-window %u",
                          "Exec=foot"):
            entry = "[Desktop Entry]\nType=Application\nName=Thing\n" + exec_line + "\n"
            out = self.js("out.v = A.menuable(A.parseEntry(%s));" % json.dumps(entry))
            self.assertTrue(out["v"]["ok"], f"{exec_line} was wrongly treated as a daemon")


if __name__ == "__main__":
    unittest.main()
