"""PCTerm.render(host) — the terminal, put somewhere other than #feed.

Run: venv-unified/bin/python -m pytest tests/client/test_terminal_embed.py

PosterChan Code's bottom panel is a REAL shell, not a second implementation of one. That is only
safe because term.js hands out the SAME singleton rather than a second instance: there is one xterm,
one PTY and one session id in that file, and two live mounts would share `term`/`ws`/`sid` and write
every physical keystroke twice — the exact failure `renderEpoch` was added for.

So the embed contract is narrow and worth pinning:

  * `render(host)` mounts into what it is given, and `render()` with nothing still means `#feed`, so
    the Terminal VIEW is untouched;
  * the module still exposes ONE terminal, not a factory;
  * leaving detaches rather than kills, because the session is meant to outlive the screen.

Each check was verified to fail with its rule removed.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TERM = os.path.join(ROOT, "static", "js", "client", "term.js")
CODE = os.path.join(ROOT, "static", "js", "client", "code.js")


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", " ", src)


class TheEmbedContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(TERM, encoding="utf-8") as f:
            cls.term = strip_comments(f.read())
        with open(CODE, encoding="utf-8") as f:
            cls.code = strip_comments(f.read())

    def test_render_takes_a_host_and_defaults_to_the_feed(self):
        """The default is what keeps the Terminal view byte-identical in behaviour. A signature that
        REQUIRED a host would leave the view mounting into `undefined` — a black screen reachable
        from the sidebar, with nothing in any log."""
        self.assertIn("async function render(host)", self.term)
        self.assertIn("const feed = host || $('#feed');", self.term)

    def test_there_is_still_exactly_one_terminal(self):
        """A factory would be the obvious way to give the editor its own shell, and it is the wrong
        one: two live mounts share this file's `term`/`ws`/`sid` and every key is written twice."""
        self.assertEqual(self.term.count("async function render(host)"), 1)
        self.assertIn("window.PCTerm = { render,", self.term)
        self.assertNotIn("function createTerminal", self.term)

    def test_the_editor_hands_over_a_container_rather_than_cloning_the_module(self):
        self.assertIn("T.render(host)", self.code)
        # No second xterm in the editor: if this ever appears, the singleton has been forked.
        self.assertNotIn("new Terminal(", self.code)

    def test_the_editor_waits_for_the_module_instead_of_assuming_it(self):
        """term.js has its own <script> tag, so the global is coming — but not necessarily by the
        time a cold APK or a rebuilt renderer paints this panel."""
        self.assertIn("if(window.PCTerm) return go();", self.code)
        self.assertIn("clearInterval(poll)", self.code)

    def test_the_panel_says_so_when_the_terminal_never_arrives(self):
        """An empty panel is indistinguishable from a shell that has not printed anything yet."""
        self.assertIn("did not load in this build", self.code)

    def test_leaving_detaches_and_does_not_kill_the_session(self):
        """The shell keeps running and the id is kept, so coming back reattaches. This is what makes
        a session started on a laptop resumable on a phone, and the editor must not have changed
        it."""
        i = self.term.index("function unmount()")
        seg = self.term[i:i + 900]
        self.assertIn("_bye()", seg)
        self.assertNotIn("kill", seg.lower())


if __name__ == "__main__":
    unittest.main()
