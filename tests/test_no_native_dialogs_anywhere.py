"""A native alert/confirm/prompt opens a REAL WINDOW in the desktop shell.

"Admin - Relay -> run auto-prune now button causes desktop to split in half."

That button called `window.confirm`. In Electron that is not an in-page dialog: it is a real window,
and sway — which tiles — puts it beside the shell and gives it half the screen. On the web it merely
blocks the renderer; in the APK's WebView it can be suppressed outright, so the branch behind it
never runs and the button silently does nothing. Three different wrong behaviours from one call.

The rule was already known and already enforced — for `static/js/client/*.js` only. The ADMIN panel
is a separate page (loaded in a full-height iframe) with its own scripts, and it had SIXTY-SEVEN of
them: 47 alerts, 16 confirms and prompts across admin.js, admin-bots.js, admin-emoji.js, the relay
tab and two includes. A rule scoped to one directory is not a rule; it is a coincidence that held
until somebody wrote code in another directory.

So this audit covers every script this app serves. `pcAlert`/`pcConfirm`/`pcPrompt` in
`static/js/admin-dialogs.js` are what admin uses instead; the client keeps `uiConfirm`/`uiPrompt`.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NATIVE = re.compile(r"(?<![.\w$])(alert|confirm|prompt)\s*\(")
# The file that DEFINES the replacements necessarily talks about them.
EXEMPT = {"admin-dialogs.js"}

# DEAD FILES, EXEMPTED WITH A TRIPWIRE — not ignored.
#
# `file-manager.js` and `apikeys.js` carry 156 native dialogs between them and are referenced by
# NOTHING: no template, no import, no service-worker precache; last touched 2026-05-31. Rewriting
# 156 call sites in code that cannot run is work with no user on the other end. But an exemption
# with no condition is how a rule quietly stops applying, so `test_the_dead_files_are_still_dead`
# below fails the moment anything loads one — at which point they must be fixed or deleted.
DEAD = {"file-manager.js", "apikeys.js"}


def _strip(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    return re.sub(r"(?<![:\w])//[^\n]*", " ", js)


def _scripts_in(html: str):
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)


class NothingServedByThisAppOpensANativeDialog(unittest.TestCase):
    def _hits(self, text, where, out):
        for m in NATIVE.finditer(_strip(text)):
            head = _strip(text)[max(0, m.start() - 40):m.start()]
            if re.search(r"(function|const|let|var)\s*$", head):
                continue        # a definition, not a call
            out.append("%s: %s(" % (where, m.group(1)))

    def test_no_native_dialog_in_any_served_javascript(self):
        bad = []
        for path in sorted((ROOT / "static" / "js").rglob("*.js")):
            if path.name in EXEMPT or path.name in DEAD or "node_modules" in str(path):
                continue
            self._hits(path.read_text(encoding="utf-8", errors="replace"),
                       str(path.relative_to(ROOT)), bad)
        self.assertEqual(bad, [], "these open a real window in the desktop shell:\n" + "\n".join(bad))

    def test_no_native_dialog_inside_a_template(self):
        """Where this one actually lived. Scanning only `static/js` missed every inline handler."""
        bad = []
        for path in sorted((ROOT / "templates").rglob("*.html")):
            for block in _scripts_in(path.read_text(encoding="utf-8", errors="replace")):
                self._hits(block, str(path.relative_to(ROOT)), bad)
        self.assertEqual(bad, [], "these open a real window in the desktop shell:\n" + "\n".join(bad))

    def test_the_admin_panel_has_a_replacement_and_loads_it_first(self):
        """Every other admin script calls these, so load order is the difference between a dialog
        and a ReferenceError inside a click handler."""
        page = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
        self.assertIn("admin-dialogs.js", page)
        self.assertLess(page.index("admin-dialogs.js"), page.index("admin.js?"),
                        "admin-dialogs.js must load before the scripts that call it")

    def test_the_replacements_answer_asynchronously_and_default_to_cancel(self):
        src = (ROOT / "static" / "js" / "admin-dialogs.js").read_text(encoding="utf-8")
        self.assertIn("new Promise", src)
        for name in ("pcAlert", "pcConfirm", "pcPrompt"):
            self.assertIn("root." + name, src)
        # Backdrop and Escape must CANCEL. Defaulting a destructive confirm to "yes" is the one
        # mistake worse than the native dialog it replaces.
        self.assertIn("Clicking the backdrop is a cancel, never a confirm", src)

    def test_every_converted_caller_awaits_it(self):
        """`pcConfirm` returns a promise; a caller that forgot to await gets a truthy object and
        proceeds unconditionally — which on 'delete every feed note' is worse than the split screen."""
        bad = []
        for path in list((ROOT / "static" / "js").glob("admin*.js")) + \
                    list((ROOT / "templates").rglob("*.html")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"(?<!await )pc(Confirm|Prompt)\(", text):
                bad.append("%s:%d" % (path.name, text.count("\n", 0, m.start()) + 1))
        self.assertEqual(bad, [], "pcConfirm/pcPrompt used without await:\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()


class TheExemptionsStayHonest(unittest.TestCase):
    def test_the_dead_files_are_still_dead(self):
        """The moment one of these is loaded it is live code full of native dialogs, and the
        exemption above becomes a lie. This is what makes skipping them defensible."""
        referenced = []
        for path in list((ROOT / "templates").rglob("*")) + list((ROOT / "static").rglob("*.js")):
            if not path.is_file() or path.name in DEAD:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for dead in DEAD:
                if dead in text:
                    referenced.append("%s references %s" % (path.name, dead))
        self.assertEqual(referenced, [],
                         "a file exempted for being unreachable is now reachable:\n"
                         + "\n".join(referenced))
