"""PosterChan as the shell of a Wayland compositor — the window-management half, RUN.

The goal is that a browser and a Steam game appear on the PosterChan desktop. Those two have
opposite requirements from an embedder's point of view: a browser could be reparented into our
window (XReparentWindow, SetParent), but a GAME cannot — reparenting costs the direct-rendering
path, Vulkan surfaces do not survive it, and any screencast approach adds a copy per frame to the
one workload that cannot afford one. The only arrangement where both are true at once is the
ordinary one: a compositor owns the screen, both are ordinary clients, and PosterChan decides where
they go.

So PosterChan's window control is PROTOCOL, not pixels — which is what makes it testable on a
machine with no display, no compositor and no graphics stack at all. Every test here stands up a
Unix socket that speaks the real i3/sway IPC wire format and drives the SHIPPED client against it.

What this cannot cover: whether sway actually honours the commands, and anything about pixels. Those
need a machine with a screen, and they are named here so nobody mistakes this for having run on one.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WM = os.path.join(ROOT, "desktop", "wm.js")
NODE = shutil.which("node") or shutil.which("nodejs")

# A compositor that answers the way sway does: same 14-byte header, same reply shapes, replies in
# the order asked. `SCRIPT` is spliced in per test.
HARNESS = r"""
const net = require('net'), fs = require('fs'), os = require('os'), path = require('path');
const MAGIC = Buffer.from('i3-ipc'), HEAD = 14;
const sock = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pcwm-')), 's');
const seen = [];                       // [{type, payload}] — what the client actually sent

const TREE = __TREE__;

function frame(type, obj){
  const body = Buffer.from(JSON.stringify(obj), 'utf8');
  const b = Buffer.allocUnsafe(HEAD + body.length);
  MAGIC.copy(b, 0); b.writeUInt32LE(body.length, 6); b.writeUInt32LE(type >>> 0, 10);
  body.copy(b, HEAD); return b;
}

const server = net.createServer((c) => {
  let buf = Buffer.alloc(0);
  c.on('data', (chunk) => {
    buf = Buffer.concat([buf, chunk]);
    for(;;){
      if(buf.length < HEAD) return;
      const len = buf.readUInt32LE(6), type = buf.readUInt32LE(10);
      if(buf.length < HEAD + len) return;
      const payload = buf.subarray(HEAD, HEAD + len).toString('utf8');
      buf = buf.subarray(HEAD + len);
      seen.push({ type, payload });
      if(type === 0){                                    // RUN_COMMAND
        const fail = /THIS_WILL_FAIL/.test(payload);
        c.write(frame(0, fail ? [{ success:false, error:'no such container' }] : [{ success:true }]));
      } else if(type === 4){ c.write(frame(4, TREE)); }   // GET_TREE
      else if(type === 7){ c.write(frame(7, { human_readable:'fake 1.9' })); }
      else if(type === 2){                                // SUBSCRIBE, then push an event
        c.write(frame(2, { success:true }));
        setTimeout(() => c.write(frame(0x80000003,
          { change:'new', container:{ id: 42, name:'Mozilla Firefox' } })), 30);
      } else { c.write(frame(type, {})); }
    }
  });
});

server.listen(sock, async () => {
  process.env.SWAYSOCK = sock;
  const { WM } = require(__WM__);
  const wm = new WM(sock);
  const out = {};
  try { __SCRIPT__ }
  catch(e){ out.threw = String((e && e.message) || e); }
  out.seen = seen;
  process.stdout.write(JSON.stringify(out));
  server.close();
  process.exit(0);
});
"""

# One browser (Wayland, app_id) and one game (XWayland, window_properties.class) — the two shapes a
# shell has to handle, and the second is the one a naive reader misses entirely.
TREE = {
    "id": 1, "type": "root", "name": "root",
    "nodes": [{
        "id": 2, "type": "output", "name": "HDMI-A-1",
        "nodes": [{
            "id": 3, "type": "workspace", "name": "1",
            "nodes": [
                {"id": 10, "type": "con", "name": "PosterChan", "app_id": "posterchan",
                 "pid": 100, "focused": False, "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}},
                {"id": 11, "type": "con", "name": "Mozilla Firefox", "app_id": "firefox",
                 "pid": 200, "focused": True, "rect": {"x": 0, "y": 0, "width": 1280, "height": 800}},
            ],
            "floating_nodes": [
                {"id": 12, "type": "floating_con", "name": "Portal 2", "app_id": None,
                 "window": 4194304, "pid": 300, "fullscreen_mode": 1,
                 "window_properties": {"class": "portal2_linux", "instance": "portal2",
                                       "title": "Portal 2"},
                 "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}},
            ],
        }],
    }],
}


@unittest.skipIf(not NODE, "no node on this node")
class CompositorIPC(unittest.TestCase):
    def run_js(self, script):
        js = (HARNESS.replace("__WM__", json.dumps(WM))
                     .replace("__TREE__", json.dumps(TREE))
                     .replace("__SCRIPT__", script))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "probe.js")
            with open(p, "w") as fh:
                fh.write(js)
            r = subprocess.run([NODE, p], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_it_speaks_the_real_wire_format(self):
        """The header is the magic string, a uint32 LE length and a uint32 LE type. Getting it wrong
        is silent — the socket simply never answers."""
        out = self.run_js("out.v = await wm.version();")
        self.assertEqual(out.get("v", {}).get("human_readable"), "fake 1.9", out)

    def test_a_refused_command_is_an_error_and_not_a_shrug(self):
        """A command's reply is an ARRAY of per-command results, and a failure is reported IN it —
        `{success:false, error:...}` over a perfectly ordinary transport. Read as "it returned, so
        it worked", every refusal is silent, and a desktop whose window commands quietly do nothing
        is indistinguishable from one whose compositor has gone away."""
        out = self.run_js("try{ await wm.command('THIS_WILL_FAIL'); out.ok = true; }"
                          "catch(e){ out.err = String(e.message); }")
        self.assertNotIn("ok", out, "a refused command reported success")
        self.assertIn("no such container", out.get("err", ""))

    def test_it_finds_the_browser_and_the_game(self):
        out = self.run_js("out.w = await wm.windows();")
        by = {w["app"]: w for w in out["w"]}
        self.assertIn("firefox", by, out["w"])
        self.assertIn("portal2_linux", by,
                      "an XWayland window was missed — it has no app_id, only a class, and that is "
                      "how Steam and most games appear")
        self.assertTrue(by["portal2_linux"]["xwayland"])
        self.assertTrue(by["portal2_linux"]["fullscreen"])
        self.assertEqual(by["firefox"]["pid"], 200)

    def test_containers_and_workspaces_are_not_windows(self):
        """A tree walk that returns every node hands the taskbar a row for the workspace and one for
        every split container — furniture the person never opened."""
        out = self.run_js("out.w = await wm.windows();")
        self.assertEqual(len(out["w"]), 3, [w["title"] for w in out["w"]])
        self.assertEqual({w["workspace"] for w in out["w"]}, {"1"})

    def test_windows_are_addressed_by_id_not_by_title(self):
        """A title is the page a browser is showing and changes as it loads; an app_id is shared by
        every window of an app. con_id is the only stable handle."""
        out = self.run_js("await wm.focus(11); await wm.close(12);")
        cmds = [s["payload"] for s in out["seen"] if s["type"] == 0]
        self.assertIn("[con_id=11] focus", cmds)
        self.assertIn("[con_id=12] kill", cmds)

    def test_placing_a_window_makes_it_floating_first(self):
        """Position and size mean nothing to a TILED window — the layout owns it, and the move is
        silently a no-op. A desktop that places windows has to say so first."""
        out = self.run_js("await wm.place(11, 100, 50, 800, 600);")
        cmds = [s["payload"] for s in out["seen"] if s["type"] == 0]
        self.assertEqual(cmds[0], "[con_id=11] floating enable", cmds)
        self.assertIn("[con_id=11] resize set 800 600", cmds)
        self.assertIn("[con_id=11] move absolute position 100 50", cmds)

    def test_events_arrive_on_their_own_socket(self):
        """sway will not answer ordinary requests on a subscribed connection, so a shell that
        subscribes on its command socket loses every reply after it."""
        out = self.run_js("""
          const got = [];
          wm.on('window', (e) => got.push(e.change));
          await wm.subscribe(['window']);
          await new Promise(r => setTimeout(r, 250));
          out.events = got;
          out.stillAnswers = (await wm.version()).human_readable;
        """)
        self.assertEqual(out.get("events"), ["new"], out)
        self.assertEqual(out.get("stillAnswers"), "fake 1.9",
                         "the command socket stopped answering once something subscribed")

    def test_an_event_type_is_read_unsigned(self):
        """An event is the ordinary shape with the HIGH BIT of the type set. Read signed it arrives
        as a large negative number and matches no case, so events are silently dropped."""
        src = open(WM, encoding="utf-8").read()
        self.assertIn("readUInt32LE(MAGIC.length + 4)", src)
        self.assertNotIn("readInt32LE", src)

    def test_a_stream_is_reassembled(self):
        """Replies arrive in pieces and several share a chunk. "One chunk is one message" works on a
        quiet socket and fails the moment anything is busy — which is when a shell is doing something
        interesting."""
        out = self.run_js("""
          const a = wm.version(), b = wm.tree(), c = wm.version();
          const [x, y, z] = await Promise.all([a, b, c]);
          out.ordered = [!!x.human_readable, y.type, !!z.human_readable];
        """)
        self.assertEqual(out.get("ordered"), [True, "root", True], out)

    def test_a_window_is_matched_by_pid_not_by_name(self):
        """Steam starts, forks, and the window belongs to a CHILD; a browser's title is the page and
        its app_id is shared with every window it already had open. Both of those pick the wrong
        window on the second launch."""
        out = self.run_js("out.w = await wm.waitForWindow(999, 600, [300]);")
        self.assertTrue(out.get("w"), "a child pid did not match its parent's launch")
        self.assertEqual(out["w"]["app"], "portal2_linux")

    def test_waiting_for_a_window_that_never_comes_answers_null(self):
        """An app that fails to start must not hang the desktop for ever — and must not be reported
        as launched either."""
        out = self.run_js("out.w = await wm.waitForWindow(4242, 600);")
        self.assertIsNone(out.get("w", "sentinel"))


if __name__ == "__main__":
    unittest.main()
