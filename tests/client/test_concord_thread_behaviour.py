"""How a Concord thread behaves, driven rather than described.

The pieces existed — threadIndex/threadRootId/threadView, the "N replies" button, the thread bar
with its Back — and nothing had ever run them against the shapes a real channel produces: a reply to
a reply, a reply whose parent was never fetched, two threads side by side, and a malformed `e` chain
from a relay that owes us nothing.

We write proper NIP-22 (kind 1111 with E/K/P root plus e/k/p parent) and Armada's reader accepts
1111 beside 9 and 1068, so a thread started in either client is a thread in the other. What could
not be checked from here is Armada's own UI: cord-reader.js is its protocol bundle, not its screens.
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(ROOT, "tests", "client", "concord_thread_behaviour_runtime.mjs")
CONCORD = os.path.join(ROOT, "static", "js", "client", "concord.js")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class ThreadsBehave(unittest.TestCase):
    def test_threads_group_read_and_survive_malformed_input(self):
        r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-4000:])

    def test_a_reply_is_written_as_nip22_so_armada_can_read_it(self):
        """Uppercase E/K/P name the ROOT, lowercase e/k/p the parent, and the kind is 1111 —
        which is what Armada's reader accepts beside 9. A bare `["e", id]` would be read as
        something else entirely by a strict reader."""
        src = open(CONCORD, encoding="utf-8").read()
        send = src[src.index("send.onclick=async()=>{"):]
        send = send[:send.index("\n")]
        self.assertIn("['K',String(target.kind||9)],['E',messageId(target),'',target.pubkey||'']", send)
        self.assertIn("['k',String(target.kind||9)],['e',messageId(target),'',target.pubkey||'']", send)
        self.assertIn("wireKind=target?1111:9", send)

    def test_opening_a_thread_targets_it_for_the_next_reply(self):
        """Replying inside a thread means replying to the thread, not to the channel — otherwise
        the answer lands outside the conversation it belongs to."""
        src = open(CONCORD, encoding="utf-8").read()
        open_thread = src[src.index("$$('[data-cc-thread]')"):]
        open_thread = open_thread[:open_thread.index("{ const back=$('#cc-thread-back')")]
        self.assertIn("state.thread=b.dataset.ccThread", open_thread)
        self.assertIn("replyTarget=all.find(x=>messageId(x)===state.thread)", open_thread)

    def test_there_is_a_way_back_out_of_a_thread(self):
        src = open(CONCORD, encoding="utf-8").read()
        self.assertIn("back.onclick=()=>{ state.thread=null; replyTarget=null; render(); }", src)
