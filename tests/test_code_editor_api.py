"""PosterChan Code — the workspace jail, the atomic save, and the two beautifiers.

Run: venv-unified/bin/python -m unittest tests.test_code_editor_api

No database and no HTTP: the gate is stubbed and the endpoints are awaited directly, because what
these cover is the part that fails SILENTLY or DESTRUCTIVELY.

- THE PATH JAIL. This router can read and write real files on the node, so `_resolve` is the whole
  security of the feature. Both halves are exercised: `..` (the obvious one) and a SYMLINK inside
  the workspace pointing out of it (the one that gets missed, because the string looks fine).
- THAT SAVING CANNOT LEAVE AN EMPTY FILE. `open(path,'w')` truncates before it writes; a failure in
  between is somebody's code replaced by nothing.
- THAT A CONCURRENT CHANGE IS REFUSED, not silently overwritten. The terminal sits beside this
  editor, so "something else touched the file" is the ordinary case here, not the exotic one.
- THAT A SYNTAX ERROR IS A SENTENCE AND NOT A 500, since people format while mid-edit.
- THAT A BINARY FILE IS REFUSED. Opened in a textarea, the bytes that do not survive the round trip
  are gone on the next save -- the editor would be the thing that destroyed the file.

Each check here was verified to fail with its rule removed.
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException

from app.routers import code as C


def run(coro):
    return asyncio.run(coro)


class _User:
    is_admin = True
    id = 1


ALLOW = mock.patch.object(C.node_service, "user_allowed", lambda db, user: True)


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        os.makedirs(os.path.join(self.tmp, "sub"))
        with open(os.path.join(self.tmp, "hello.py"), "w") as f:
            f.write("x   =  [1,2 ,3]\n")
        with open(os.path.join(self.tmp, "sub", "run.sh"), "w") as f:
            f.write("if [ -f x ]; then\necho hi\nfi\n")
        self._root = mock.patch.object(C, "_root", lambda: self.tmp)
        self._root.start()
        ALLOW.start()
        self.addCleanup(self._root.stop)
        self.addCleanup(ALLOW.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ---- the jail ---------------------------------------------------------------------------

    def test_a_relative_escape_is_refused(self):
        """The obvious half, and the only one a string check catches."""
        for bad in ("../etc/passwd", "sub/../../etc/passwd", "....//etc/passwd", "/etc/passwd"):
            with self.subTest(path=bad):
                try:
                    got = C._resolve(bad, must_exist=False)
                except HTTPException as e:
                    self.assertIn(e.status_code, (400, 403))
                    continue
                # An absolute path is stripped to a relative one and lands INSIDE, which is also a
                # correct answer -- what must never happen is a resolved path outside the root.
                self.assertEqual(os.path.commonpath([self.tmp, got]), self.tmp, bad)

    def test_a_symlink_out_of_the_workspace_is_refused(self):
        """THE HALF THAT GETS MISSED. The path contains no `..` and reads as an ordinary file in the
        workspace; only resolving it first shows where it goes. Checking containment before
        resolving passes this every time."""
        outside = os.path.join(tempfile.mkdtemp(), "secret.txt")
        with open(outside, "w") as f:
            f.write("not yours\n")
        self.addCleanup(shutil.rmtree, os.path.dirname(outside), True)
        link = os.path.join(self.tmp, "innocent.txt")
        os.symlink(outside, link)
        with self.assertRaises(HTTPException) as cm:
            C._resolve("innocent.txt")
        self.assertEqual(cm.exception.status_code, 403)

    def test_a_sibling_directory_sharing_a_prefix_is_not_inside(self):
        """`/srv/app` and `/srv/app-secrets` share a string prefix and are different directories.
        `startswith` says yes; `commonpath` says no."""
        sib = self.tmp + "-secrets"
        os.makedirs(sib, exist_ok=True)
        self.addCleanup(shutil.rmtree, sib, True)
        with open(os.path.join(sib, "k.txt"), "w") as f:
            f.write("x")
        with self.assertRaises(HTTPException):
            C._resolve(os.path.join("..", os.path.basename(sib), "k.txt"))

    # ---- reading ----------------------------------------------------------------------------

    def test_the_tree_lists_and_marks_directories_first(self):
        got = run(C.tree(path="", db=None, current_user=_User()))
        names = [e["name"] for e in got["entries"]]
        self.assertEqual(names, ["sub", "hello.py"], "directories are not sorted to the top")
        self.assertEqual([e["lang"] for e in got["entries"] if e["name"] == "hello.py"], ["python"])

    def test_a_binary_file_is_refused_rather_than_mangled(self):
        with open(os.path.join(self.tmp, "blob.bin"), "wb") as f:
            f.write(b"\x89PNG\x00\x1a\n")
        with self.assertRaises(HTTPException) as cm:
            run(C.read_file(path="blob.bin", db=None, current_user=_User()))
        self.assertEqual(cm.exception.status_code, 415)

    def test_a_file_over_the_ceiling_is_refused_rather_than_streamed_into_a_textarea(self):
        big = os.path.join(self.tmp, "big.py")
        with open(big, "w") as f:
            f.write("#" * (C.MAX_BYTES + 10))
        with self.assertRaises(HTTPException) as cm:
            run(C.read_file(path="big.py", db=None, current_user=_User()))
        self.assertEqual(cm.exception.status_code, 413)

    # ---- writing ----------------------------------------------------------------------------

    def test_saving_replaces_the_file_atomically_and_leaves_no_temp_behind(self):
        body = C.WriteBody(path="hello.py", text="y = 2\n")
        got = run(C.write_file(body=body, db=None, current_user=_User()))
        self.assertTrue(got["ok"])
        with open(os.path.join(self.tmp, "hello.py")) as f:
            self.assertEqual(f.read(), "y = 2\n")
        leftovers = [n for n in os.listdir(self.tmp) if "pccode-tmp" in n]
        self.assertEqual(leftovers, [], "a temp file was left in the workspace")

    def test_a_file_changed_underneath_the_editor_is_refused_not_overwritten(self):
        """The terminal sits beside this editor, so this is the ORDINARY case. Overwriting silently
        is the one outcome that loses work with nothing to say so."""
        path = os.path.join(self.tmp, "hello.py")
        stale = int(os.path.getmtime(path)) - 60
        body = C.WriteBody(path="hello.py", text="clobbered\n", mtime=stale)
        with self.assertRaises(HTTPException) as cm:
            run(C.write_file(body=body, db=None, current_user=_User()))
        self.assertEqual(cm.exception.status_code, 409)
        with open(path) as f:
            self.assertEqual(f.read(), "x   =  [1,2 ,3]\n", "the file was overwritten anyway")

    def test_a_save_outside_the_workspace_is_refused(self):
        body = C.WriteBody(path="../escaped.py", text="nope\n")
        with self.assertRaises(HTTPException):
            run(C.write_file(body=body, db=None, current_user=_User()))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp), "escaped.py")))

    # ---- the gate ---------------------------------------------------------------------------

    def test_every_endpoint_shares_the_terminals_gate(self):
        """Editing a node's files and running commands on it are the same privilege. Two gates is
        how one of them ends up quietly wider than anybody meant."""
        with mock.patch.object(C.node_service, "user_allowed", lambda db, user: False):
            for call in (lambda: run(C.tree(path="", db=None, current_user=_User())),
                         lambda: run(C.read_file(path="hello.py", db=None, current_user=_User())),
                         lambda: run(C.config(db=None, current_user=_User())),
                         lambda: run(C.write_file(body=C.WriteBody(path="a.py", text="x"),
                                                  db=None, current_user=_User())),
                         lambda: run(C.format_source(body=C.FormatBody(language="python",
                                                                       source="x=1"),
                                                     db=None, current_user=_User()))):
                with self.assertRaises(HTTPException) as cm:
                    call()
                self.assertEqual(cm.exception.status_code, 403)


class Beautifiers(unittest.TestCase):
    def setUp(self):
        ALLOW.start()
        self.addCleanup(ALLOW.stop)

    def _fmt(self, lang, src, indent=4):
        return run(C.format_source(body=C.FormatBody(language=lang, source=src, indent=indent),
                                   db=None, current_user=_User()))

    @unittest.skipIf(not C._engines().get("python"), "black is not installed on this node")
    def test_python_is_formatted(self):
        got = self._fmt("python", "x   =  [1,2 ,3]\ndef  f( a ):\n  return a\n")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["engine"], "black")
        self.assertIn("x = [1, 2, 3]", got["source"])
        self.assertIn("def f(a):", got["source"])

    @unittest.skipIf(not C._engines().get("python"), "black is not installed on this node")
    def test_broken_python_is_a_sentence_not_a_500(self):
        """People format while mid-edit. The buffer comes back untouched."""
        src = "def f(:\n"
        got = self._fmt("python", src)
        self.assertFalse(got["ok"])
        self.assertEqual(got["source"], src, "the buffer was altered by a failed format")
        self.assertTrue(got["error"], "a failure with nothing to show the person")

    @unittest.skipIf(not C._engines().get("bash"), "beautysh is not installed on this node")
    def test_bash_is_indented(self):
        got = self._fmt("bash", "if [ -f x ]; then\necho hi\nfi\n", indent=2)
        self.assertTrue(got["ok"], got)
        self.assertIn("\n  echo hi", got["source"])

    @unittest.skipIf(not C._engines().get("bash"), "beautysh is not installed on this node")
    def test_an_unbalanced_block_is_left_alone(self):
        """beautysh reports trouble as a flag beside a best-effort result rather than raising.
        Returned as success, it would reindent a half-typed script around a block that is not
        there."""
        got = self._fmt("bash", "if [ -f x ]; then\necho hi\n")
        self.assertFalse(got["ok"], "a broken script was reported as successfully formatted")

    def test_json_is_pretty_printed_without_escaping_the_worlds_alphabets(self):
        got = self._fmt("json", '{"b":1,"a":"café"}', indent=2)
        self.assertTrue(got["ok"], got)
        self.assertIn("café", got["source"], "non-ASCII was re-encoded into escapes")

    def test_a_language_with_no_formatter_says_so_and_returns_the_buffer(self):
        got = self._fmt("rust", "fn main(){}")
        self.assertFalse(got["ok"])
        self.assertEqual(got["source"], "fn main(){}")

    def test_the_node_reports_which_engines_it_actually_has(self):
        """A Format button that silently does nothing is worse than one that is not offered."""
        eng = C._engines()
        self.assertIn("python", eng)
        self.assertIn("bash", eng)


if __name__ == "__main__":
    unittest.main()
