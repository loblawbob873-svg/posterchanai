"""THE SHIPPED SESSION CONFIG MUST BE LOADABLE, AND ITS BINDINGS MUST DO SOMETHING.

A bad `/etc/wayfire.ini` is not a cosmetic bug. Wayfire loads it at session start, and on a fresh
install that is the only thing between the machine and a desktop: the config fails, the compositor
comes up with defaults or not at all, and the person is looking at a screen with no shell, no
taskbar and no way to start anything -- on a machine nobody can log into to fix it.

This replaces `test_sway_config_is_valid.py`, which ran `sway --validate`. **Wayfire has no
validate mode**, so this checks what a static reading can actually establish, and deliberately
includes the one class of fault that a parser would NOT have caught:

    binding_super_used_left = <super> KEY_LEFT
    command_super_used_left = /usr/local/bin/pc-super used

That parses perfectly and is a DEAD KEY. `pc-super used` only suppresses the Start menu that the
Super release would otherwise open; it performs no window action. All four arrow bindings were like
this, so keyboard snapping silently did nothing on the Wayfire session for its whole life while the
same keys worked on the old one and the taskbar menu offering the same actions kept working. A
binding whose command is only bookkeeping is the shape to look for.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "os/overlay/app-misc/posterchanos-shell/files"
CONFIG = FILES / "wayfire.ini"
EBUILD = ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild"

#: `pc-super used` is bookkeeping: it marks the modifier consumed so the release does not open Start.
BOOKKEEPING = "/usr/local/bin/pc-super used"


def parse(text):
    """wayfire.ini into {section: {key: value}}, preserving duplicates so they can be reported."""
    sections, current, dupes = {}, None, []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line.strip("[]")
            sections.setdefault(current, {})
            continue
        if "=" not in line:
            raise AssertionError("line %d is neither a section, a comment nor key = value: %r"
                                 % (number, raw))
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        assert current is not None, "line %d sets %r before any [section]" % (number, key)
        if key in sections[current]:
            dupes.append((current, key))
        sections[current][key] = value
    return sections, dupes


class TheSessionConfigParses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CONFIG.read_text(encoding="utf-8")
        cls.sections, cls.dupes = parse(cls.text)

    def test_every_line_is_a_section_a_comment_or_a_setting(self):
        self.assertIn("core", self.sections)
        self.assertIn("command", self.sections)

    def test_no_setting_is_written_twice_in_one_section(self):
        """The later one silently wins, which is how a binding is 'set' and does something else."""
        self.assertEqual(self.dupes, [], "duplicated keys: %s" % self.dupes)

    def test_the_session_starts_our_shell_and_nothing_elses(self):
        autostart = self.sections.get("autostart", {})
        self.assertIn("shell", autostart)
        self.assertIn("pc-shell-start-wayfire", autostart["shell"])
        # wf-shell would add a second panel, dock and background over PosterChan's own.
        joined = " ".join(autostart.values())
        for unwanted in ("wf-panel", "wf-dock", "wf-background"):
            self.assertNotIn(unwanted, joined)


class EveryBindingDoesSomething(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.command = parse(CONFIG.read_text(encoding="utf-8"))[0]["command"]

    def test_every_binding_has_a_command_and_every_command_has_a_binding(self):
        """Either half alone is silent: a dead key, or a command that can never be reached."""
        for key in self.command:
            if key.startswith("binding_"):
                self.assertIn("command_" + key[len("binding_"):], self.command,
                              "%s is bound to nothing" % key)
            elif key.startswith("command_"):
                suffix = key[len("command_"):]
                self.assertTrue("binding_" + suffix in self.command
                                or "release_binding_" + suffix in self.command,
                                "%s can never be reached" % key)

    def test_no_binding_runs_only_the_start_menu_bookkeeping(self):
        """THE DEAD-KEY SHAPE. See the module docstring: this parses and does nothing."""
        dead = [k for k, v in self.command.items()
                if k.startswith("command_") and v.strip() == BOOKKEEPING]
        self.assertEqual(dead, [], "these bindings suppress the Start menu and perform no action: %s"
                                   % dead)

    def test_no_chord_is_registered_twice(self):
        """Only one of two bindings on the same chord can win, and nothing says which.

        `<super><shift>Enter` was bound both to the recovery terminal and to the Start-menu
        bookkeeping, so either the terminal never opened or Start opened on the release -- and both
        lines looked correct. This is the Wayfire spelling of the `Overwriting binding` check that
        `sway --validate` used to give us for free.
        """
        chords = {}
        for key, value in self.command.items():
            if not key.startswith(("binding_", "release_binding_")):
                continue
            chords.setdefault(value.strip(), []).append(key)
        clashes = {chord: keys for chord, keys in chords.items() if len(keys) > 1}
        self.assertEqual(clashes, {}, "these chords are bound more than once: %s" % clashes)

    def test_the_window_actions_are_all_bound(self):
        """Snap, close and the shell ticks -- the controls the taskbar menu also offers."""
        joined = " ".join(self.command.values())
        for action in ("pc-window-snap left", "pc-window-snap right", "pc-window-snap max",
                       "pc-window-snap minimise", "pc-window-close",
                       "pc-wayfire-action pc:terminal", "pc-wayfire-action pc:tasks",
                       # Closing is pc-window-close, never `pc-wayfire-action pc:close`: the tick
                       # reaches only the renderer's own focused frame, so with a popped-out window
                       # or a bare native toplevel focused it closed nothing at all.
                       "pc-window-cycle next", "pc-window-cycle previous",
                       "pc-screenshot region", "pc-screenshot screen",
                       "pc-shell-restart"):
            self.assertIn(action, joined, "%s is not bound to any key" % action)


class EveryHelperItRunsIsShipped(unittest.TestCase):
    def test_every_referenced_helper_is_installed_by_the_package(self):
        """A binding pointing at an absent executable is the exact failure `sway --validate` used to
        catch here, and it is the half that mattered: the key does nothing and says nothing."""
        installed = set(re.findall(r"[a-z0-9-]+", EBUILD.read_text(encoding="utf-8")
                                   .split("for helper in", 1)[1].split(";", 1)[0]))
        referenced = set(re.findall(r"/usr/local/bin/([a-z0-9-]+)",
                                    CONFIG.read_text(encoding="utf-8")))
        missing = sorted(referenced - installed)
        self.assertEqual(missing, [], "wayfire.ini runs helpers the package does not install: %s"
                                      % missing)
