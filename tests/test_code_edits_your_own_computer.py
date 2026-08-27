"""PosterChan Code edits files on YOUR computer.

Run: venv-unified/bin/python -m pytest tests/test_code_edits_your_own_computer.py

Asked for in one line: "i want users to edit code on their computer with posterchan code, that's
all." That is not the node's tree and not a server-side workspace — it is the machine the person is
sitting at, which needs no permission from this instance at all.

The host bridge could browse, move, rename and trash, and hand a file to the OS. It could not read
or write one file's CONTENTS, so there was nothing for an editor to open.

Everything risky about that lives in desktop/hostfs.js, where a bridge cannot be talked out of it by
whoever is calling: a size ceiling, a NUL-byte refusal, an ATOMIC write, and an mtime
compare-and-swap.
"""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


def _decomment(js):
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


def _fn(src, head):
    i = src.index(head)
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"{head} never closes")


class TheBridgeCanReadAndWriteOneFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hostfs = _read("desktop", "hostfs.js")
        cls.main = _read("desktop", "main.js")
        cls.preload = _read("desktop", "preload.js")

    def test_it_exports_them(self):
        self.assertIn("readText", self.hostfs)
        self.assertIn("writeText", self.hostfs)
        exports = self.hostfs[self.hostfs.index("module.exports = {"):]
        self.assertIn("readText", exports, "readText exists but is not exported")
        self.assertIn("writeText", exports, "writeText exists but is not exported")

    def test_a_text_editor_only_ever_sees_text(self):
        """A 4 GB disk image or a JPEG must not reach a buffer and be saved back mangled."""
        body = _decomment(_fn(self.hostfs, "function readText("))
        self.assertIn("TEXT_MAX", body, "no size ceiling")
        self.assertIn("buf.includes(0)", body, "a binary file would load as mojibake")

    def test_the_write_is_atomic(self):
        """Writing in place is how an editor destroys the thing it was editing: a crash or a full
        disk leaves a truncated file where the original was."""
        body = _decomment(_fn(self.hostfs, "function writeText("))
        self.assertIn("renameSync", body, "the file is written in place")
        self.assertIn("path.dirname(abs)", body,
                      "the temp file is not in the same directory, so the rename crosses "
                      "filesystems and is no longer atomic")

    def test_it_refuses_to_overwrite_a_file_that_changed(self):
        """A terminal sitting beside the editor is the likeliest thing to have changed it."""
        body = _decomment(_fn(self.hostfs, "function writeText("))
        self.assertIn("changed-on-disk", body)
        self.assertIn("mtimeMs", body)

    def test_the_ipc_and_preload_both_carry_them(self):
        """A bridge function nothing exposes is a function the renderer cannot call."""
        self.assertIn("pc:host:readText", self.main)
        self.assertIn("pc:host:writeText", self.main)
        self.assertIn("pc:host:readText", self.preload)
        self.assertIn("pc:host:writeText", self.preload)
        self.assertIn("fsGuard(e)", self.main[self.main.index("pc:host:readText"):
                                             self.main.index("pc:host:readText") + 200])


class TheEditorOpensAndSavesThem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = _read("static", "js", "client", "code.js")
        cls.app = _read("static", "js", "client", "app.js")
        cls.host = _read("static", "js", "client", "hostfiles.js")

    def test_the_editor_can_open_one(self):
        self.assertIn("openHostFile", self.code)
        i = self.code.index("window.PCCode = {")
        self.assertIn("openHostFile", self.code[i:i + 400], "it exists but is not exported")

    def test_saving_goes_back_to_the_disk_it_came_from(self):
        body = _decomment(_fn(self.code, "async function saveDoc("))
        self.assertIn("d.host", body)
        self.assertIn("H.writeText", body)
        # …and before the drive/folder branches, or a local file would be uploaded to Blossom.
        self.assertLess(body.index("d.host"), body.index("d.blob"))

    def test_a_stale_buffer_is_re_read_from_the_disk(self):
        """`/api/code/file?path=` resolves against the SERVER's workspace and would answer about a
        different file, or 400."""
        body = _decomment(_fn(self.code, "async function hydrate("))
        self.assertIn("d.host", body)
        self.assertLess(body.index("d.host"), body.index("api("))

    def test_there_is_a_door_in_this_computer(self):
        """The door is the FILE, not a button beside it \u2014 "just click on the icon or double click".
        A local row already had a click (it handed the file to the machine), so the editor joins
        that click as a choice rather than replacing it; `openHere` is what keeps the old answer on
        the list, and without it this feature is a removal."""
        self.assertTrue("openhost" not in self.host, "an Open button came back on a local row")
        self.assertTrue("u.openFile(p, nm, openHere)" in self.host,
                        "clicking a local file does not offer the editor")
        self.assertTrue("openFile: (path, name, openHere) =>" in self.app,
                        "Files never passes an opener to the host view")

    def test_the_host_view_does_not_decide_what_text_is(self):
        """One answer for every source in Files, or the drive and this computer disagree about the
        same filename."""
        self.assertIn("u.openable", self.host)
        self.assertIn("openable: () => true", self.app)
        self.assertNotIn("_CODE_EXT", self.host, "hostfiles.js grew its own opinion")

    def test_openable_is_in_the_file_click_handlers_scope(self):
        """The grid builder and click binder are sibling functions. Declaring this in rowsHTML
        paints a convincing file list whose every regular-file click crashes at runtime."""
        render = _fn(self.host, "async function render(")
        rows = _fn(self.host, "function rowsHTML(")
        self.assertIn("const openable = u.openable", render)
        self.assertIn("openable(nm", render)
        self.assertNotIn("const openable", rows)

    def test_clicking_a_this_computer_video_executes_the_real_handler(self):
        """Paint and click the shipped row. This fails with the production `openable is not
        defined` exception; merely checking that the word exists in the file does not."""
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            self.skipTest("node is unavailable")
        sim = os.path.join(ROOT, "tests", "client", "hostfiles_click_sim.js")
        result = subprocess.run([node, sim], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("This Computer video click holds", result.stdout)


if __name__ == "__main__":
    unittest.main()
