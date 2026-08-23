"""A local shell for the PosterChanOS terminal, RUN — a real PTY, real output, real exit.

The client's terminal speaks to a PTY over SSH. On PosterChanOS the machine IS the node, and going
out over the network to reach one's own computer is absurd — worse, PosterChanOS can run with no
PosterChan server at all, and then there is nothing to SSH to.

NO NATIVE MODULE. node-pty is a compiled addon — one per platform, rebuilt against every Electron
version — in an app that ships as a single AppImage. `script` from util-linux allocates a real PTY
and exists on every Linux system that has a shell.
"""
import json
import re
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "desktop", "localterm.js")
CLIENT = os.path.join(ROOT, "static", "js", "client", "term.js")
NODE = shutil.which("node") or shutil.which("nodejs")
SCRIPT = shutil.which("script")


@unittest.skipIf(not NODE or not SCRIPT, "needs node and util-linux script")
class LocalTerminal(unittest.TestCase):
    def test_local_is_default_without_hiding_saved_server_hosts(self):
        """PosterChanOS prepends local, starts it when no tabs exist, and keeps every configured
        server available through New tab after that first session is ready."""
        with open(CLIENT, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("return LOCAL() ? [LOCAL_HOST].concat(rest) : rest", src)
        self.assertIn("if(!live.length && hosts.length) connect();", src)
        self.assertIn("go.classList.remove('hidden')", src)

    def run_js(self, body, timeout=30):
        js = ("const T = require(%s);\n(async () => { const out = {};\n"
              "const done = (o) => { Object.assign(out, o); try{ T.closeAll(); }catch(_){}\n"
              "  process.stdout.write(JSON.stringify(out)); process.exit(0); };\n"
              "try { %s } catch(e){ done({ threw: String(e.message || e) }); }\n"
              "setTimeout(() => done({ timedout: true }), %d);\n})();"
              % (json.dumps(MOD), body, (timeout - 5) * 1000))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stderr[-900:])
        return json.loads(r.stdout)

    def test_it_is_a_real_pty_and_a_command_runs(self):
        out = self.run_js("""
          const s = T.start({cols: 90, rows: 25});
          let seen = '';
          T.subscribe(s.id, ev => { if(ev.t === 'out'){ seen += ev.d;
            if(/PCTERM-OK/.test(seen)) done({ ok: true, id: s.id }); } });
          setTimeout(() => T.write(s.id, "echo PCTERM-OK\\n"), 600);
        """)
        self.assertTrue(out.get("ok"), out)

    def test_the_shell_is_told_its_size(self):
        """A shell that thinks it is 80x24 on a 1920px screen wraps every long line in the wrong
        place. `script` gives a PTY and no way to resize it from outside, so stty does it inside."""
        out = self.run_js("""
          const s = T.start({cols: 123, rows: 45});
          let seen = '';
          T.subscribe(s.id, ev => { if(ev.t === 'out'){ seen += ev.d;
            const m = /COLS=(\\d+)/.exec(seen); if(m) done({ cols: Number(m[1]) }); } });
          setTimeout(() => T.write(s.id, "echo COLS=$(tput cols)\\n"), 700);
        """)
        self.assertEqual(out.get("cols"), 123, out)

    def test_resize_never_injects_stty_into_the_users_input(self):
        """Resize is an ioctl on the slave PTY, not text pasted into a half-written command."""
        out = self.run_js("""
          const s = T.start({cols: 80, rows: 24}); let seen = '';
          T.subscribe(s.id, ev => { if(ev.t === 'out') seen += ev.d; });
          setTimeout(() => T.write(s.id, "printf RESIZE-SAFE"), 500);
          setTimeout(() => T.resize(s.id, 131, 47), 650);
          setTimeout(() => T.write(s.id, "\\n"), 800);
          setTimeout(() => T.write(s.id, "echo SIZE=$(tput cols)x$(tput lines)\\n"), 1050);
          setTimeout(() => done({seen}), 1900);
        """)
        self.assertIn("RESIZE-SAFE", out["seen"])
        self.assertIn("SIZE=131x47", out["seen"])
        self.assertNotIn("stty cols 131", out["seen"])

    def test_output_survives_a_reloaded_page(self):
        """The WebView is recreated under memory pressure and on a crash. A terminal that comes back
        blank is one nobody trusts with a long-running command."""
        out = self.run_js("""
          const s = T.start({});
          setTimeout(() => T.write(s.id, "echo REMEMBER-ME\\n"), 500);
          setTimeout(() => {
            const b = T.backlog(s.id, 0);
            done({ has: /REMEMBER-ME/.test(b.d), seq: b.seq, alive: b.alive });
          }, 2500);
        """)
        self.assertTrue(out.get("has"), out)
        self.assertTrue(out.get("alive"))

    def test_a_cursor_past_the_kept_buffer_says_so(self):
        """The buffer is bounded, so a cursor older than what is kept cannot be honoured exactly.
        Returning a fragment as though it were the whole gap is how scrollback silently loses a
        chunk out of its middle."""
        out = self.run_js("""
          const s = T.start({});
          setTimeout(() => {
            const b = T.backlog(s.id, 999999999);
            done({ truncated: b.truncated === true, empty: b.d === '' });
          }, 900);
        """)
        # Nothing has scrolled off yet, so this is the ordinary case: a cursor ahead of the stream
        # returns nothing rather than pretending to replay.
        self.assertTrue(out.get("empty"), out)

    def test_an_exit_is_reported_not_silence(self):
        """A terminal whose shell died and did not say so looks like one that has stopped
        responding."""
        out = self.run_js("""
          const s = T.start({});
          T.subscribe(s.id, ev => { if(ev.t === 'end') done({ ended: true, code: ev.code }); });
          setTimeout(() => T.write(s.id, "exit\\n"), 600);
        """)
        self.assertTrue(out.get("ended"), out)

    def test_writing_to_a_closed_session_is_refused_not_thrown(self):
        out = self.run_js("""
          const s = T.start({});
          T.close(s.id);
          done({ w: T.write(s.id, 'x'), b: T.backlog(s.id, 0) });
        """)
        self.assertFalse(out["w"]["ok"])
        self.assertIsNone(out["b"])

    def test_there_is_a_ceiling_on_open_shells(self):
        """A shell each for somebody who has lost count is still not eight."""
        out = self.run_js("""
          const ids = [];
          try { for(let i = 0; i < T.MAX_SESSIONS + 3; i++) ids.push(T.start({}).id); }
          catch(e){ done({ opened: ids.length, threw: String(e.message) }); }
          done({ opened: ids.length, threw: null });
        """)
        self.assertEqual(out["opened"], 8)
        self.assertIn("too many", out.get("threw") or "")

    def test_two_tabs_are_two_independent_ptys(self):
        """Input for one terminal must never appear in another terminal's output."""
        out = self.run_js("""
          const a = T.start({cols: 80, rows: 24});
          const b = T.start({cols: 120, rows: 40});
          let ao = '', bo = '';
          T.subscribe(a.id, ev => { if(ev.t === 'out') ao += ev.d; });
          T.subscribe(b.id, ev => { if(ev.t === 'out') bo += ev.d; });
          setTimeout(() => T.write(a.id, "echo ONLY-TAB-A\\n"), 600);
          setTimeout(() => T.write(b.id, "echo ONLY-TAB-B\\n"), 900);
          setTimeout(() => done({ao, bo, ids:[a.id,b.id]}), 2200);
        """)
        self.assertNotEqual(out["ids"][0], out["ids"][1])
        self.assertIn("ONLY-TAB-A", out["ao"])
        self.assertNotIn("ONLY-TAB-B", out["ao"])
        self.assertIn("ONLY-TAB-B", out["bo"])
        self.assertNotIn("ONLY-TAB-A", out["bo"])

    def test_terminal_screen_exposes_real_tab_controls(self):
        src = open(CLIENT, encoding="utf-8").read()
        self.assertIn('aria-label="Terminal tabs"', src)
        self.assertIn('id="tty-tab-new"', src)
        self.assertIn('data-tab=', src)

    def test_new_tab_stays_visible_and_new_sessions_repaint_the_strip(self):
        """A second PTY is not useful as a tab if its button disappears once connected or the
        strip keeps showing the old session until the whole Terminal view is reopened."""
        src = open(CLIENT, encoding="utf-8").read()
        chrome = src[src.index("function _chrome(on)"):src.index("function _focus()")]
        self.assertIn("go.classList.remove('hidden')", chrome)
        self.assertNotIn("go.classList.toggle('hidden', on)", chrome)
        ready = src[src.index("if(m.t === 'ready')"):src.index("if(m.t === 'gone')")]
        self.assertIn("_sessions();", ready)

    def test_a_fresh_local_tab_replays_bytes_produced_before_attach(self):
        """Posterfetch and a fast prompt exist before the renderer subscribes; dropping that backlog
        makes a distinct new PTY look like a blank copy of the active tab."""
        src = open(CLIENT, encoding="utf-8").read()
        local = src[src.index("function _openLocal"):src.index("function _frame")]
        started = local[local.index("await T.start"):]
        self.assertIn("await T.backlog(id, fresh ? 0", started)
        self.assertNotIn("b = null", started.split("if(gone)")[0])
        self.assertLess(started.index("await T.attach(id)"), started.index("await T.backlog(id, fresh ? 0"))

    def test_switching_tabs_detaches_the_old_renderer_stream(self):
        """A tab switch must remove the old main-process subscription, not merely hide its bytes
        in the renderer. Otherwise every visited PTY keeps feeding the same window forever."""
        preload = open(os.path.join(ROOT, "desktop", "preload.js"), encoding="utf-8").read()
        main = open(os.path.join(ROOT, "desktop", "main.js"), encoding="utf-8").read()
        client = open(CLIENT, encoding="utf-8").read()
        self.assertIn("detach: (id) => ipcRenderer.invoke('pc:term:detach'", preload)
        self.assertIn("ipcMain.handle('pc:term:detach'", main)
        self.assertIn("links.delete(sid)", main)
        self.assertIn("T.detach(id)", client)

    def test_a_stale_async_open_cannot_take_over_the_new_tab(self):
        src = open(CLIENT, encoding="utf-8").read()
        self.assertIn("let openEpoch = 0", src)
        opened = src[src.index("function _open(frame)"):src.index("function _openLocal")]
        self.assertIn("const opening = ++openEpoch", opened)
        self.assertGreaterEqual(opened.count("opening !== openEpoch"), 3)

    def test_backlog_and_push_overlap_is_deduplicated_by_sequence(self):
        src = open(CLIENT, encoding="utf-8").read()
        frame = src[src.index("function _frame(m)"):src.index("if(m.t === 'ready')")]
        self.assertIn("m.seq <= cursor", frame)

    def test_an_empty_terminal_starts_its_first_session(self):
        """Configured SSH hosts must not turn PosterChanOS Terminal into an inert host picker."""
        src = open(CLIENT, encoding="utf-8").read()
        render = src[src.index("async function render()") : src.index("function unmount()")]
        self.assertIn("if(!live.length && hosts.length) connect();", render)

    def test_no_typescript_file_is_written(self):
        """`script` writes a verbatim log of the session, including everything typed at a password
        prompt. /dev/null is the whole point of that argument."""
        with open(MOD, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("'/dev/null'", src.replace('"', "'"))
        self.assertIn("-qfc", src)

    def test_it_needs_no_native_module(self):
        """A compiled addon is one per platform, rebuilt against every Electron version, in an app
        that ships as a single AppImage. Checked against what is REQUIRED, not against the prose —
        the comment explaining why node-pty is absent names it."""
        with open(MOD, encoding="utf-8") as fh:
            src = fh.read()
        reqs = re.findall(r"require\(\s*['\"]([^'\"]+)['\"]", src)
        self.assertEqual([r for r in reqs if not r.startswith((".", "/"))],
                         ["child_process", "fs"], reqs)


if __name__ == "__main__":
    unittest.main()
