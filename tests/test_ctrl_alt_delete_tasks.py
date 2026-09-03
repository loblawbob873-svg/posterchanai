from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CtrlAltDelete(unittest.TestCase):
    def test_bound_in_both_install_sources(self):
        needle = "bindsym Ctrl+Mod1+Delete exec swaymsg -t send_tick pc:tasks"
        self.assertIn(needle, (ROOT / "os" / "gentoo.sh").read_text())
        self.assertIn(needle, (ROOT / "os" / "overlay" / "app-misc" / "posterchanos-shell" / "files" / "sway.config").read_text())

    def test_tick_opens_the_real_task_manager(self):
        src = (ROOT / "static" / "js" / "client" / "os.js").read_text()
        i = src.index("else if(p === 'pc:tasks')")
        body = src[i:i+700]
        self.assertIn("openTaskManager()", body)
        self.assertIn("const taskFocusToken=_claimFocus()", body)
        self.assertIn("_focusCompositorCurrent(sh.id,taskFocusToken)", body)


if __name__ == "__main__":
    unittest.main()
