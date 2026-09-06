"""THE TASKBAR IS ONE FACT, AND IT WAS DECIDED IN THREE PLACES THAT DISAGREED.

Only the shell renderer can measure the taskbar: it is a flex child whose height moves with the UI
scale, the font size and the safe-area inset. It measures it and publishes it, and main.js keeps it
for the arithmetic that lives there. But the KEY BINDINGS are separate processes -- Super+Left /
Right / Up run `pc-window-snap`, which cannot read that object and carried a hardcoded
`height - 72`.

That constant was never once correct. The bar is 48 css px: 48 device px at ui scale 1 and 60 at
1.25. MEASURED on the real desk after an upgrade, a snapped window was 2503 tall on a 2560 output --
its bottom edge three pixels inside the taskbar. Reported as "window snapping covers taskbar".

So main writes the measurement where another process can read it, and the helper reads it. These
tests RUN the helper's reader against real files, because the failure is a number, not a shape.
"""
from pathlib import Path
import json
import os
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")


def _reserve_fn():
    """The shipped helper's reader, loaded on its own (the file talks to a compositor at import)."""
    src = SNAP.read_text(encoding="utf-8")
    start = src.index("def taskbar_reserve(box):")
    end = src.index("\ndef ", start + 1)
    ns = {"os": os, "json": json}
    exec(compile(src[start:end], str(SNAP), "exec"), ns)
    return ns["taskbar_reserve"]


class TestTheHelperReadsTheMeasurement(unittest.TestCase):
    def setUp(self):
        self.fn = _reserve_fn()
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._old

    def _publish(self, **area):
        Path(self.tmp, "posterchan-workarea.json").write_text(json.dumps(area))

    def test_it_uses_the_published_reserve(self):
        self._publish(x=0, y=0, w=3840, h=2500, reserve=60)
        self.assertEqual(self.fn({"width": 3840, "height": 2560}), 60,
                         "the snap helper ignored the taskbar the shell measured")

    def test_the_other_ui_scale_measures_differently_and_is_also_honoured(self):
        self._publish(x=0, y=0, w=3840, h=2512, reserve=48)
        self.assertEqual(self.fn({"width": 3840, "height": 2560}), 48)

    def test_no_file_falls_back_to_the_old_constant_not_to_zero(self):
        """A wrong reserve misplaces one window; a zero reserve puts EVERY snap over the bar."""
        self.assertEqual(self.fn({"width": 3840, "height": 2560}), 72)

    def test_an_unreadable_file_is_not_a_zero_reserve(self):
        Path(self.tmp, "posterchan-workarea.json").write_text("{not json")
        self.assertEqual(self.fn({"width": 3840, "height": 2560}), 72)

    def test_a_reserve_bigger_than_half_the_output_is_refused(self):
        """A number from another output, or a shell that measured itself mid-resize."""
        self._publish(x=0, y=0, w=3840, h=100, reserve=2000)
        self.assertEqual(self.fn({"width": 3840, "height": 2560}), 72)

    def test_a_zero_reserve_is_not_treated_as_a_measurement(self):
        self._publish(x=0, y=0, w=3840, h=2560, reserve=0)
        self.assertEqual(self.fn({"width": 3840, "height": 2560}), 72)


class TestNobodyStillHardcodesIt(unittest.TestCase):
    def test_the_snap_helper_has_no_bare_72_left_in_its_arithmetic(self):
        src = SNAP.read_text(encoding="utf-8")
        body = src[src.index("def taskbar_reserve"):]
        body = body[body.index("\ndef ", 1):]        # everything AFTER the reader
        self.assertNotIn("- 72", body,
                         "a second hardcoded taskbar height is still in the snap arithmetic")


class TestMainPublishesIt(unittest.TestCase):
    def test_the_work_area_handler_writes_the_file(self):
        handler = MAIN[MAIN.index("ipcMain.handle('pc:wm:workarea'"):]
        handler = handler[: handler.index("ipcMain.handle('pc:wm:move'")]
        self.assertIn("publishWorkAreaFile", handler,
                      "the measurement never leaves this process, so a key binding cannot read it")

    def test_it_writes_into_the_session_runtime_dir(self):
        fn = MAIN[MAIN.index("function publishWorkAreaFile"):]
        fn = fn[: fn.index("\nipcMain.handle")]
        self.assertIn("XDG_RUNTIME_DIR", fn, "the file would outlive the session")
        self.assertIn("posterchan-workarea.json", fn)

    def test_the_write_is_atomic(self):
        """The reader is a key binding that can run at any instant; half a JSON file is a fallback
        taken for no reason."""
        fn = MAIN[MAIN.index("function publishWorkAreaFile"):]
        fn = fn[: fn.index("\nipcMain.handle")]
        self.assertIn("renameSync", fn)

    def test_it_refuses_to_publish_a_rectangle_it_does_not_have(self):
        """A guard, found by what it TESTS rather than by how near the top it sits: this looked in
        the first 400 characters and started failing when a line was added above it, about code
        that had not changed."""
        fn = MAIN[MAIN.index("function publishWorkAreaFile"):]
        fn = fn[: fn.index("\nipcMain.handle")]
        guard = [l for l in fn.splitlines() if "return" in l and "area" in l]
        self.assertTrue(guard, "nothing refuses an absent or zero-sized rectangle:\n" + fn)
        self.assertRegex(guard[0], r"!area|area\.w|area\.h")


if __name__ == "__main__":
    unittest.main()


def test_the_snap_helper_can_find_its_own_socket():
    """A key binding always has WAYFIRE_SOCKET. Nothing else does.

    `os.environ["WAYFIRE_SOCKET"]` answered its absence with a KeyError traceback into a stderr
    nobody reads -- so from the person's side the key simply did nothing. Hit while verifying the
    snap geometry over ssh, which is exactly the "anything else" case. The socket is discoverable,
    so it is discovered, and a genuine absence says what it looked for.
    """
    import os
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    snap = (Path(__file__).resolve().parents[1]
            / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap")
    import re as _re
    body = snap.read_text(encoding="utf-8")
    # Strip docstrings and comments first: the explanation quotes the very expression being
    # forbidden, so a raw search matches the reason it was removed. (Third time tonight.)
    code = _re.sub(r'"""[\s\S]*?"""', "", body)
    code = _re.sub(r"(?m)^\s*#.*$", "", code)
    assert 'os.environ["WAYFIRE_SOCKET"]' not in code, "still fatal on a missing variable"
    assert "_wayfire_socket()" in code

    with tempfile.TemporaryDirectory() as run:
        # No variable and no socket: a clear message, not a KeyError.
        env = dict(os.environ, XDG_RUNTIME_DIR=run)
        env.pop("WAYFIRE_SOCKET", None)
        done = subprocess.run([sys.executable, str(snap), "left"], env=env,
                              text=True, capture_output=True, timeout=30)
        assert done.returncode != 0
        assert "no Wayfire IPC socket" in (done.stderr + done.stdout), done.stderr

        # A socket present in the runtime dir is found without the variable being set. It refuses
        # the connection (nothing is listening), which is a DIFFERENT failure -- and that is the
        # point: the lookup got far enough to try.
        Path(run, "wayfire-wayland-1-.socket").touch()
        done = subprocess.run([sys.executable, str(snap), "left"], env=env,
                              text=True, capture_output=True, timeout=30)
        assert "no Wayfire IPC socket" not in (done.stderr + done.stdout), (
            "the socket in XDG_RUNTIME_DIR was not discovered")
