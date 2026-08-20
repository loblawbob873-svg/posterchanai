"""The shell's visible half: launcher, taskbar, panel — rendered and CLICKED.

The logic is tested next door; this is about the part that only shows up when somebody presses
something. It runs the shipped osshell.js against a fake compositor in jsdom-free node with a tiny
DOM shim, because the interesting failures are wiring: a launcher button that starts nothing, a
taskbar entry that focuses the wrong window, a panel that renders a plausible number for a
subsystem it could not read.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(ROOT, "static", "js", "client", "osshell.js")
NODE = shutil.which("node") or shutil.which("nodejs")

# A DOM small enough to read: elements with innerHTML that records the buttons it was given, and
# querySelectorAll returning them so handlers can be attached and fired.
DOM = r"""
function el(){
  const e = { _html: '', children: [], dataset: {}, disabled: false, onclick: null };
  Object.defineProperty(e, 'innerHTML', {
    get(){ return e._html; },
    set(v){
      e._html = String(v);
      e.children = [];
      const re = /<button[^>]*?(?:data-(app|win|os)="([^"]*)")[^>]*>([\s\S]*?)<\/button>/g;
      let m;
      while((m = re.exec(e._html))){
        const b = { dataset: {}, disabled: false, onclick: null, text: m[3] };
        b.dataset[m[1] === 'win' ? 'win' : (m[1] === 'app' ? 'app' : 'os')] = m[2];
        e.children.push(b);
      }
    },
  });
  e.querySelectorAll = (sel) => {
    const key = sel.indexOf('data-app') >= 0 ? 'app' : sel.indexOf('data-win') >= 0 ? 'win' : 'os';
    return e.children.filter(c => c.dataset[key] !== undefined);
  };
  e.appendChild = () => {};
  return e;
}
"""


@unittest.skipIf(not NODE, "no node on this node")
class Render(unittest.TestCase):
    def run_js(self, bridges, body):
        js = """
        %s
        const B = %s;
        for(const k in B) globalThis[k] = B[k];
        globalThis.PC = { toast: (m) => { (globalThis.__toasts = globalThis.__toasts || []).push(m); } };
        const S = require(%s);
        (async () => { const out = {}; const host = el();
        try { %s } catch(e){ out.threw = String(e.message || e); }
        out.toasts = globalThis.__toasts || [];
        process.stdout.write(JSON.stringify(out)); })();
        """ % (DOM, bridges, json.dumps(MOD), body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1200:])
        return json.loads(r.stdout)

    WM = """{ pcWM: {
        windows: async () => globalThis.__wins,
        focus: async (id) => { globalThis.__focus = id; return true; },
        launch: async (cands, opts) => {
            /* The main process resolves CANDIDATE command lines against the filesystem — only it
             * can. Here the first is taken, which is what a machine with the usual paths does. */
            const argv = (opts && opts.candidates) ? cands[0] : cands;
            globalThis.__argv = argv;
            globalThis.__wins = globalThis.__wins.concat([{id: 99, app: argv[0].split('/').pop(),
                                                           title: 'New window'}]);
            return { pid: 4, window: { id: 99 } }; },
        subscribe: async () => true,
        onEvent: () => () => {},
      },
      pcNet: { status: async () => ({ online: true, kind: 'wifi', name: 'Tribble', signal: 71 }) },
      /* THE SHAPE desktop/power.js ACTUALLY RETURNS, `profiles` included. It was omitted here, and
         a stub that omits a field can never catch the panel reading it wrong: `profiles` is an
         OBJECT and the panel treated it as an array, so a real laptop offering
         low-power/balanced/performance showed no power modes at all and nothing threw. */
      pcPower: { status: async () => ({ battery: {present: true, percent: 80, charging: false},
                                        brightness: {available: true, percent: 60},
                                        profiles: { available: true, kind: 'platform',
                                                    list: ['low-power', 'balanced', 'performance'],
                                                    active: 'balanced' },
                                        canHibernate: true }) },
      pcAudio: { status: async () => ({ output: { percent: 35, muted: false } }) },
      /* Tor as pcShell exposes it. Present here because a tray chip that cannot be tested is a
         tray chip that silently stops working — and because ABSENT and OFF are different states
         this has to be able to tell apart. */
      pcShell: { tor: { status: async () => ({ enabled: globalThis.__torOn !== false,
                                               bootstrapped: 100, country: 'us',
                                               countryName: 'United States' }),
                        set: async (o) => { globalThis.__torSet = o; return { enabled: !!o.enabled }; },
                        onStatus: () => {}, newCircuit: async () => true } } }"""

    def test_pressing_browser_launches_firefox(self):
        """The thing that was asked for: a launcher button that actually starts a program."""
        out = self.run_js(self.WM, """
          globalThis.__wins = [];
          await S.render(host);
          const btn = host.querySelectorAll('[data-app]').find(b => b.dataset.app === 'browser');
          out.found = !!btn;
          await btn.onclick();
          out.argv = globalThis.__argv;
        """)
        self.assertTrue(out["found"], "no Browser button was rendered")
        self.assertEqual(out["argv"], ["/usr/bin/firefox"])

    def test_pressing_it_again_focuses_the_window_it_opened(self):
        out = self.run_js(self.WM, """
          globalThis.__wins = [];
          await S.render(host);
          let btn = host.querySelectorAll('[data-app]').find(b => b.dataset.app === 'browser');
          await btn.onclick();                       // opens it, and re-renders
          globalThis.__argv = null;
          btn = host.querySelectorAll('[data-app]').find(b => b.dataset.app === 'browser');
          await btn.onclick();                       // now it is open
          out.argv = globalThis.__argv;
          out.focus = globalThis.__focus;
        """)
        self.assertIsNone(out["argv"], "it launched a second browser instead of focusing the first")
        self.assertEqual(out["focus"], 99)

    def test_pressing_terminal_opens_ours_rather_than_spawning_one(self):
        """The Terminal on this desktop is PosterChan's own — a PTY on this machine, with its
        history as ephemeral Nostr events. `foot` is still on the machine and still bound to
        $mod+Return, deliberately: it is the escape hatch for when the shell itself is what has
        gone wrong, which is exactly when a terminal drawn BY the shell cannot help you."""
        out = self.run_js(self.WM, """
          globalThis.__wins = [];
          S.setViewOpener(v => { globalThis.__view = v; });
          await S.render(host);
          const btn = host.querySelectorAll('[data-app]').find(b => b.dataset.app === 'terminal');
          await btn.onclick();
          out.view = globalThis.__view || null;
          out.argv = globalThis.__argv || null;
        """)
        self.assertEqual(out["view"], "terminal")
        self.assertIsNone(out["argv"], "it spawned somebody else's terminal emulator")

    def test_an_app_that_opens_nothing_tells_the_person(self):
        """Steam is optional on this profile; a button that silently does nothing is the worst
        possible answer to "is it installed?"."""
        bridges = self.WM.replace(
            "return { pid: 4, window: { id: 99 } }; }",
            "return { pid: 4, window: null }; }")
        out = self.run_js(bridges, """
          globalThis.__wins = [];
          await S.render(host);
          const btn = host.querySelectorAll('[data-app]').find(b => b.dataset.app === 'steam');
          await btn.onclick();
        """)
        self.assertTrue(any("installed" in t for t in out["toasts"]), out["toasts"])

    def test_the_taskbar_focuses_the_window_it_names(self):
        out = self.run_js(self.WM, """
          globalThis.__wins = [{id: 12, app: 'firefox', title: 'Gentoo Wiki'},
                               {id: 13, app: 'foot', title: 'shell'}];
          await S.render(host);
          const tasks = host.querySelectorAll('[data-win]');
          out.n = tasks.length;
          await tasks[1].onclick();
          out.focus = globalThis.__focus;
        """)
        self.assertEqual(out["n"], 2)
        self.assertEqual(out["focus"], 13)

    def test_the_desktop_itself_gets_no_taskbar_button(self):
        out = self.run_js(self.WM, """
          globalThis.__wins = [{id: 1, app: 'posterchan-desktop', title: 'PosterChan'}];
          await S.render(host);
          out.n = host.querySelectorAll('[data-win]').length;
        """)
        self.assertEqual(out["n"], 0)

    def test_the_panel_shows_what_it_read(self):
        out = self.run_js(self.WM, """
          globalThis.__wins = [];
          await S.render(host);
          out.html = host.innerHTML;
        """)
        for want in ("Tribble", "35%", "60%", "80%"):
            self.assertIn(want, out["html"], f"the panel does not show {want}")

    def test_the_profiles_are_read_as_the_bridge_returns_them(self):
        """`power.js` answers `profiles: { available, kind, list, active }` and the panel read it as
        an ARRAY. An object has no `.length`, so the profile row was never drawn and the machine
        offered no power modes at all — measured on a laptop whose kernel was reporting
        `low-power balanced performance` the whole time. Nothing threw; the reading was right and
        the panel could not show it.

        Driven through `profileMenu`, which is why that is a function rather than two lines inside
        the popover: the failure is a `.length` on the wrong kind of value, and it draws an empty
        row perfectly."""
        out = self.run_js(self.WM, """
          const st = await globalThis.pcPower.status();
          out.real = S.profileMenu(st);
          // The obvious-but-wrong shape a future bridge might answer with must still work.
          out.asArray = S.profileMenu({ profiles: ['a', 'b'], profile: 'b' });
          // And a machine with none must offer none rather than a row of dead buttons.
          out.none = S.profileMenu({ profiles: { available: false, list: [], active: '' } });
          out.missing = S.profileMenu({});
        """)
        self.assertEqual(out["real"]["list"], ["low-power", "balanced", "performance"],
                         "the machine's real power modes were not read out of the bridge's answer")
        self.assertEqual(out["real"]["active"], "balanced", "the profile in use was not read")
        self.assertEqual(out["asArray"], {"list": ["a", "b"], "active": "b"})
        self.assertEqual(out["none"]["list"], [])
        self.assertEqual(out["missing"]["list"], [])

    def test_tor_is_one_press_away_in_the_tray(self):
        """On PosterChanOS this is the switch that decides how every byte leaves the machine, and it
        lived in a menu bar the shell hides and a tray icon sway does not draw — so on the one
        platform where it matters most it was the hardest control here to reach. "If it's easy for
        them to turn on on PosterChanOS, then that's good"."""
        out = self.run_js(self.WM, """
          globalThis.__wins = [];
          await S.render(host);
          const tor = host.querySelectorAll('[data-os]').filter(b => b.dataset.os === 'tor');
          out.chip = tor.length;
          out.bound = tor.length === 1 && typeof tor[0].onclick === 'function';
          out.sum = S.panelSummary(await S.panelState()).tor;
        """)
        self.assertEqual(out["chip"], 1, "there is no Tor control in the tray")
        self.assertTrue(out["bound"], "the Tor chip is painted but nothing is wired to it")
        self.assertTrue(out["sum"]["present"])
        self.assertEqual(out["sum"]["country"], "us",
                         "the exit country is not carried, so the tray cannot say where it leaves")

    def test_a_build_with_no_tor_shows_no_tor_chip(self):
        """ABSENT is not OFF. A build with nothing to switch must not draw a control that can never
        turn anything on — the same rule the network chip follows for an unreadable NetworkManager,
        by the opposite route."""
        bridges = self.WM.replace("pcShell: { tor:", "pcShellUnused: { tor:")
        out = self.run_js(bridges, """
          globalThis.__wins = [];
          await S.render(host);
          out.chip = host.querySelectorAll('[data-os]').filter(b => b.dataset.os === 'tor').length;
          out.sum = S.panelSummary(await S.panelState()).tor;
        """)
        self.assertEqual(out["chip"], 0, "a Tor chip was drawn on a build with no Tor")
        self.assertFalse(out["sum"]["present"])

    def test_the_battery_opens_the_power_menu_rather_than_only_reporting(self):
        """A reading you cannot act on is the shape of control this shell keeps getting wrong: the
        battery was a `<span>`, and the power profiles — the thing you want when you look at a
        battery — were behind a separate ⏻ two chips along. Asked for as "clicking on battery
        should let me change power mode"."""
        # The fake DOM above only collects `<button …>` elements, so a chip appearing here at all
        # is the assertion that it is pressable — a `<span>` is parsed into nothing.
        out = self.run_js(self.WM, """
          globalThis.__wins = [];
          await S.render(host);
          const pw = host.querySelectorAll('[data-os]').filter(b => b.dataset.os === 'power');
          out.battery = pw.some(b => /80%/.test(b.text));
          out.bound = pw.length > 0 && pw.every(b => typeof b.onclick === 'function');
        """)
        self.assertTrue(out["battery"],
                        "the battery is not a button that opens the power menu")
        self.assertTrue(out["bound"], "a power chip is painted but nothing is wired to it")

    def test_a_subsystem_it_could_not_read_is_marked_not_faked(self):
        bridges = self.WM.replace(
            "pcNet: { status: async () => ({ online: true, kind: 'wifi', name: 'Tribble', signal: 71 }) }",
            "pcNet: { status: async () => { throw new Error('no NetworkManager'); } }")
        out = self.run_js(bridges, """
          globalThis.__wins = [];
          await S.render(host);
          out.html = host.innerHTML;
        """)
        self.assertIn("os-unknown", out["html"])
        self.assertNotIn("Tribble", out["html"])
        # ...and the rest of the panel is still there. A panel that blanks because one subsystem is
        # down tells you nothing about a battery that is still draining.
        self.assertIn("80%", out["html"])


if __name__ == "__main__":
    unittest.main()
