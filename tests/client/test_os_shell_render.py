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
      pcPower: { status: async () => ({ battery: {present: true, percent: 80, charging: false},
                                        brightness: {available: true, percent: 60},
                                        canHibernate: true }) },
      pcAudio: { status: async () => ({ output: { percent: 35, muted: false } }) } }"""

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
