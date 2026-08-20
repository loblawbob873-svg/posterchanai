"""The PosterChanOS surface: launcher, taskbar, panel.

This half exists only when PosterChan IS the desktop. Everywhere else — a browser tab, the APK, the
desktop app on somebody's Windows machine — the bridges do not exist, and the module must be ABSENT
rather than broken. That is what lets one client be an app on one machine and an operating system on
another.

The judgement in a shell is not in its markup. It is in which windows belong on a taskbar, what a
launcher does when the thing is already running, and what a panel says when it cannot read
something — because a wifi icon at full strength on a machine whose NetworkManager is dead is a lie
that costs somebody an hour.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(ROOT, "static", "js", "client", "osshell.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class Shell(unittest.TestCase):
    def run_js(self, body, bridges="{}"):
        js = """
        const B = %s;
        for(const k in B) globalThis[k] = B[k];
        const S = require(%s);
        (async () => { const out = {};
        try { %s } catch(e){ out.threw = String(e.message || e); }
        process.stdout.write(JSON.stringify(out)); })();
        """ % (bridges, json.dumps(MOD), body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return json.loads(r.stdout)

    def test_a_bridge_that_cannot_reach_a_compositor_is_still_absent(self):
        """THE WINDOWS BUG. The desktop app's preload exposes pcWM on every platform — it cannot
        know what it is running on top of — so "the bridge exists" answered yes on Windows and the
        OS shell drew itself there: a launcher for programs that are not installed, a taskbar of
        compositor windows that cannot exist, a tray reporting a machine nothing had asked. The
        honest question is whether a compositor ANSWERS, and only a real call settles it."""
        bridges = """{ pcWM: { windows: async () => { throw new Error('no compositor socket'); },
                               focus: async () => true } }"""
        out = self.run_js("out.d = await S.detect(); out.a = S.available();", bridges)
        self.assertFalse(out["d"])
        self.assertFalse(out["a"])

    def test_a_compositor_that_answers_is_present(self):
        bridges = "{ pcWM: { windows: async () => [], focus: async () => true } }"
        out = self.run_js("out.d = await S.detect(); out.a = S.available();", bridges)
        self.assertTrue(out["d"])
        self.assertTrue(out["a"])

    def test_it_is_absent_off_a_compositor(self):
        """A browser tab has no pcWM, and every other entry point must simply not use this."""
        self.assertFalse(self.run_js("out.a = S.available();")["a"])

    def test_the_desktop_does_not_list_itself(self):
        """A desktop with itself on its own taskbar is a mirror pointed at a mirror."""
        rows = self.run_js("""out.r = S.taskbarRows([
            {id:1, app:'posterchan-desktop', title:'PosterChan'},
            {id:2, app:'posterchan', title:'PosterChan'},
            {id:3, app:'firefox', title:'Gentoo Wiki'}]);""")["r"]
        self.assertEqual([r["app"] for r in rows], ["firefox"])

    def test_a_window_with_no_title_yet_is_not_shown(self):
        """It is still opening. A nameless button that renames itself a second later is worse than
        one that arrives late."""
        rows = self.run_js("out.r = S.taskbarRows([{id:9, app:'foot', title:'   '}]);")["r"]
        self.assertEqual(rows, [])

    def test_a_long_title_is_trimmed_not_wrapped(self):
        rows = self.run_js("out.r = S.taskbarRows([{id:1, app:'firefox', title:'%s'}]);"
                           % ("x" * 200))["r"]
        self.assertLessEqual(len(rows[0]["label"]), 48)
        self.assertTrue(rows[0]["label"].endswith("…"))

    def test_the_machines_own_apps_join_the_launcher(self):
        """"Should be able to manage/open any game/app under PosterChan Desktop." The built-in list
        is three entries; everything else installed on the machine comes from the .desktop scan."""
        bridges = """{ pcWM: { windows: async () => [], focus: async () => true },
                       pcApps: { list: async () => ({ apps: [
                         { id: 'btop', name: 'btop', match: 'btop', argv: ['/usr/bin/foot','-e','/usr/bin/btop'], group: 'System' },
                         { id: 'org.qbittorrent.qBittorrent', name: 'qBittorrent',
                           match: 'qbittorrent', argv: ['/usr/bin/qbittorrent'], group: 'Internet' } ] }) } }"""
        out = self.run_js("out.a = (await S.allApps()).map(x => x.name);", bridges)
        self.assertIn("Browser", out["a"], "the built-ins were lost")
        self.assertIn("qBittorrent", out["a"], "an installed program is missing from the launcher")
        self.assertIn("btop", out["a"])

    def test_a_program_the_shell_already_names_is_not_offered_twice(self):
        """MEASURED ON THE TEST LAPTOP, and an exact match does not catch it. The built-in Browser
        matches `firefox`; Gentoo ships `firefox-bin.desktop`, whose program is `firefox-bin`. An
        `===` comparison saw two different apps and the menu offered "Browser" AND "Mozilla Firefox
        (bin)" — one program, two rows, both starting the same browser.

        A prefix AT A SEPARATOR rather than `includes`, because `steamlink` is not `steam` and a
        containment test would swallow it — silently, since the symptom of that is a program that is
        installed and simply never appears in the menu."""
        bridges = """{ pcWM: { windows: async () => [], focus: async () => true },
                       pcApps: { list: async () => ({ apps: [
                         { id: 'firefox-bin', name: 'Mozilla Firefox (bin)', match: 'firefox-bin',
                           argv: ['/usr/bin/firefox-bin'] },
                         { id: 'steam', name: 'Steam', match: 'steam', argv: ['/usr/bin/steam'] },
                         { id: 'steamlink', name: 'Steam Link', match: 'steamlink',
                           argv: ['/usr/bin/steamlink'] } ] }) } }"""
        out = self.run_js("out.a = (await S.allApps()).map(x => x.name);", bridges)
        self.assertEqual(len([n for n in out["a"] if "firefox" in n.lower() or n == "Browser"]), 1,
                         "firefox is in the launcher twice: " + repr(out["a"]))
        self.assertEqual(len([n for n in out["a"] if n == "Steam"]), 1,
                         "Steam is in the launcher twice: " + repr(out["a"]))
        self.assertIn("Steam Link", out["a"],
                      "a different program was swallowed by the deduplication: " + repr(out["a"]))

    def test_a_machine_app_is_launched_with_its_OWN_argv(self):
        """A built-in names several possible command lines because it cannot know how this
        distribution installed the program; a scanned entry already carries the one argv its
        .desktop file names. Passed as a candidate LIST, the launcher would try to exec its first
        word as a whole command line."""
        bridges = """{ pcWM: { windows: async () => [],
                               focus: async () => true,
                               launch: async (argv, opts) => { globalThis.__argv = argv;
                                                               globalThis.__opts = opts;
                                                               return { pid: 7, window: { id: 3 } }; } },
                       pcApps: { list: async () => ({ apps: [
                         { id: 'btop', name: 'btop', match: 'btop',
                           argv: ['/usr/bin/foot', '-e', '/usr/bin/btop'] } ] }) } }"""
        out = self.run_js("""
          await S.allApps();
          out.r = await S.launch('app:btop');
          out.argv = globalThis.__argv;
          out.candidates = !!(globalThis.__opts && globalThis.__opts.candidates);
        """, bridges)
        self.assertEqual(out["argv"], ["/usr/bin/foot", "-e", "/usr/bin/btop"])
        self.assertFalse(out["candidates"],
                         "a resolved argv was passed as a list of candidate command lines")

    def test_no_scan_bridge_means_the_built_ins_and_nothing_broken(self):
        """A browser tab, the APK, and the desktop app on Windows have no pcApps at all."""
        bridges = "{ pcWM: { windows: async () => [], focus: async () => true } }"
        out = self.run_js("out.m = await S.machineApps(); out.a = (await S.allApps()).length;", bridges)
        self.assertEqual(out["m"], [])
        self.assertEqual(out["a"], 3)

    def test_launching_something_already_open_focuses_it(self):
        """A second browser window is almost never what "Browser" meant."""
        bridges = """{ pcWM: {
            windows: async () => [{id: 7, app: 'firefox', title: 'open'}],
            focus: async (id) => { globalThis.__focused = id; return true; },
            launch: async () => { globalThis.__launched = true; return {pid: 1, window: {id: 2}}; },
        } }"""
        out = self.run_js("out.r = await S.launch('browser'); out.f = globalThis.__focused;"
                          "out.l = !!globalThis.__launched;", bridges)
        self.assertEqual(out["f"], 7)
        self.assertFalse(out["l"], "it started a second copy instead of focusing the first")

    def test_launching_something_closed_starts_it(self):
        bridges = """{ pcWM: {
            windows: async () => [],
            focus: async () => true,
            launch: async (cands, opts) => {
                globalThis.__argv = (opts && opts.candidates) ? cands[0] : cands;
                return {pid: 5, window: {id: 11}}; },
        } }"""
        out = self.run_js("out.r = await S.launch('browser'); out.argv = globalThis.__argv;", bridges)
        self.assertEqual(out["r"]["window"], 11)
        self.assertIn("firefox", out["argv"][0])

    def test_the_terminal_is_ours_and_starts_no_process(self):
        """The Terminal opens PosterChan's own — a PTY on this machine through the desktop bridge,
        with its history as ephemeral Nostr events. Launching `foot` here would spawn somebody
        else's emulator and throw all of that away."""
        bridges = """{ pcWM: {
            windows: async () => [], focus: async () => true,
            launch: async (c) => { globalThis.__argv = c; return {pid: 5, window: {id: 11}}; },
        } }"""
        out = self.run_js("S.setViewOpener(v => { globalThis.__view = v; });"
                          "out.r = await S.launch('terminal');"
                          "out.view = globalThis.__view; out.argv = globalThis.__argv || null;",
                          bridges)
        self.assertEqual(out["view"], "terminal")
        self.assertEqual(out["r"]["view"], "terminal")
        self.assertIsNone(out["argv"], "it spawned a process for a screen of our own")

    def test_a_view_app_on_a_desktop_that_cannot_open_it_says_so(self):
        """Rather than throwing, or reporting a launch that did not happen."""
        bridges = """{ pcWM: { windows: async () => [], focus: async () => true,
                               launch: async () => ({pid: 1, window: {id: 2}}) } }"""
        out = self.run_js("S.setViewOpener(null); out.r = await S.launch('terminal');", bridges)
        self.assertIn("Terminal", out["r"]["why"])

    def test_a_launch_that_opens_nothing_says_so(self):
        """Usually the program is not installed — Steam is optional here — and "nothing happened"
        is the least useful thing a launcher can say."""
        bridges = """{ pcWM: {
            windows: async () => [], focus: async () => true,
            launch: async () => ({ pid: 3, window: null }),
        } }"""
        out = self.run_js("out.r = await S.launch('steam');", bridges)
        self.assertIsNone(out["r"]["window"])
        self.assertIn("installed", out["r"]["why"])

    def test_a_subsystem_that_could_not_be_read_shows_as_unknown(self):
        """Not as a plausible default. A wifi icon at full strength on a machine whose
        NetworkManager is dead is a lie that costs somebody an hour."""
        out = self.run_js("out.s = S.panelSummary({});")["s"]
        self.assertFalse(out["net"]["known"])
        self.assertFalse(out["battery"]["known"])
        self.assertFalse(out["volume"]["known"])
        self.assertFalse(out["brightness"]["known"])

    def test_offline_is_different_from_unknown(self):
        out = self.run_js("out.s = S.panelSummary({net:{online:false}});")["s"]
        self.assertTrue(out["net"]["known"])
        self.assertEqual(out["net"]["text"], "offline")

    def test_a_machine_with_no_battery_is_known_and_absent(self):
        """A tower. Different from "the battery could not be read", which is a fault."""
        out = self.run_js("out.s = S.panelSummary({power:{battery:{present:false}}});")["s"]
        self.assertTrue(out["battery"]["known"])
        self.assertFalse(out["battery"]["present"])

    def test_a_muted_output_keeps_its_level(self):
        out = self.run_js("out.s = S.panelSummary({audio:{output:{percent:40, muted:true}}});")["s"]
        self.assertEqual(out["volume"]["percent"], 40)
        self.assertTrue(out["volume"]["muted"])

    def test_one_read_not_four_polls(self):
        """Four separate polls against four subsystems is four chances to be half-updated, and a
        panel showing yesterday's battery beside today's volume is one people stop trusting."""
        bridges = """{ pcWM: { windows: async () => [] },
            pcNet: { status: async () => ({online:true, kind:'wifi', name:'Tribble', signal:71}) },
            pcPower: { status: async () => ({battery:{present:true,percent:80,charging:false},
                                             brightness:{available:true,percent:60},
                                             canHibernate:true}) },
            pcAudio: { status: async () => ({output:{percent:35, muted:false}}) } }"""
        out = self.run_js("const st = await S.panelState(); out.s = S.panelSummary(st);", bridges)
        s = out["s"]
        self.assertEqual(s["net"]["text"], "Tribble")
        self.assertEqual(s["net"]["signal"], 71)
        self.assertEqual(s["battery"]["percent"], 80)
        self.assertEqual(s["volume"]["percent"], 35)
        self.assertEqual(s["brightness"]["percent"], 60)
        self.assertTrue(s["canHibernate"])

    def test_one_dead_subsystem_does_not_blank_the_others(self):
        """A panel that goes empty because NetworkManager is down tells you nothing about your
        battery, which is still there and still draining."""
        bridges = """{ pcWM: { windows: async () => [] },
            pcNet: { status: async () => { throw new Error('no NetworkManager'); } },
            pcPower: { status: async () => ({battery:{present:true,percent:12,charging:false}}) } }"""
        out = self.run_js("const st = await S.panelState(); out.s = S.panelSummary(st);", bridges)
        self.assertFalse(out["s"]["net"]["known"])
        self.assertEqual(out["s"]["battery"]["percent"], 12)

    def test_provisioning_reports_its_failure_rather_than_throwing(self):
        """Sign-in must not fail because the account step did — the person is signed in either way,
        and what they need is to be told the machine has nowhere to put their files."""
        bridges = """{ pcOS: { provision: async () => { throw new Error('sudo refused'); } } }"""
        out = self.run_js("out.r = await S.ensureAccount('npub1xyz');", bridges)
        self.assertFalse(out["r"]["ok"])
        self.assertIn("sudo refused", out["r"]["why"])


if __name__ == "__main__":
    unittest.main()
