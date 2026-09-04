from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CtrlAltDelete(unittest.TestCase):
    def test_bound_in_the_one_install_source(self):
        """THERE IS ONE SOURCE NOW, AND THAT IS THE POINT.

        This used to assert the binding in BOTH os/gentoo.sh and the packaged config, because the
        installer generated its own copy of the compositor config and the two drifted. The generated
        copy is gone: gentoo.sh installs the packaged `/etc/wayfire.ini` (with a copy-from-source
        fallback), so the binding exists once and cannot disagree with itself.
        """
        needle = "/usr/local/bin/pc-wayfire-action pc:tasks"
        config = ROOT / "os" / "overlay" / "app-misc" / "posterchanos-shell" / "files" / "wayfire.ini"
        self.assertIn(needle, config.read_text())
        installer = (ROOT / "os" / "gentoo.sh").read_text()
        self.assertNotIn("cat >/etc/wayfire.ini", installer,
                         "the installer generates a second copy of the session config again")
        self.assertIn("wayfire.ini", installer, "the installer no longer ships the session config")

    def test_tick_opens_the_real_task_manager(self):
        src = (ROOT / "static" / "js" / "client" / "os.js").read_text()
        i = src.index("else if(p === 'pc:tasks')")
        body = src[i:i+700]
        self.assertIn("openTaskManager()", body)
        self.assertIn("const taskFocusToken=_claimFocus()", body)
        self.assertIn("_focusCompositorCurrent(sh.id,taskFocusToken)", body)


if __name__ == "__main__":
    unittest.main()
