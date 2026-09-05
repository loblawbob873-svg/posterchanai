"""A POPUP WINDOW MUST NEVER SHOW THE ORDINARY CLIENT, INCLUDING WHILE IT BOOTS.

The start menu, the notification centre, the tray flyout and the composer are each their own
compositor window running this same bundle, so each BOOTS THE WHOLE CLIENT. What makes such a
window a menu rather than a client is `os-popup-body`, added by `popupHost()` -- which runs from
`restore()`, which app.js calls from boot AFTER `_cfgCache(CFG)`, a network fetch. On a node that
is slow or unreachable the sidebar, timeline and search box are therefore painted at full size
inside a 780x920 menu for as long as that fetch takes.

Reported as: "start menu and other taskbar widgets looks like it's loading a classic webui because
it can't connect to the relay". It is exactly that, and nothing throws or logs -- the real menu
arrives later and replaces it, so it reads as a slow, broken-looking menu rather than as a bug.

The window's identity is in its URL and needs nothing from the network, so os.js decides it
synchronously at parse time and stamps `pc-popup-boot` on <html>. These tests pin both halves.
Each is verified to fail with the shield removed.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


class TestTheShieldIsDecidedBeforeAnythingRenders(unittest.TestCase):
    def test_os_js_stamps_the_marker_at_parse_time(self):
        head = OS_JS[: OS_JS.index("const MIN_WIDTH")]
        self.assertIn("pc-popup-boot", head,
                      "the popup marker is not set before os.js has defined anything")
        self.assertIn("pcpopup", head, "the marker is not keyed off the URL")

    def test_it_is_set_synchronously_and_not_from_a_callback(self):
        head = OS_JS[: OS_JS.index("const MIN_WIDTH")]
        for deferred in ("addEventListener", "DOMContentLoaded", "setTimeout",
                         "requestAnimationFrame", "await ", ".then("):
            self.assertNotIn(deferred, head,
                             f"the shield waits on {deferred!r}; the client paints in that gap")

    def test_it_marks_the_root_element_not_the_body(self):
        """os.js may be evaluated before <body> exists, and then a body write silently does nothing."""
        head = OS_JS[: OS_JS.index("const MIN_WIDTH")]
        self.assertIn("documentElement", head)

    def test_the_marker_is_ahead_of_the_network(self):
        """The bug is an ordering one: prove the class os.js used to rely on lands after a fetch."""
        boot = APP_JS.index("window.PCOS && window.PCOS.restore()")
        cfg = APP_JS.index("_cfgCache(CFG)")
        self.assertLess(cfg, boot,
                        "restore() no longer runs after the config fetch — re-read this test")


class TestTheShieldActuallyHides(unittest.TestCase):
    def _rules(self):
        return [m.group(0) for m in
                re.finditer(r"html\.pc-popup-boot[^{]*\{[^}]*\}", CSS)]

    def test_a_rule_exists_and_hides(self):
        rules = self._rules()
        self.assertTrue(rules, "nothing in the stylesheet acts on pc-popup-boot")
        self.assertTrue(any("display:none" in r for r in rules), rules)

    def test_both_popup_hosts_survive_it(self):
        """Hiding every child would blank the compose window, which draws into #modal-root."""
        rule = next(r for r in self._rules() if "display:none" in r)
        self.assertIn("#os-popup-host", rule)
        self.assertIn("#modal-root", rule)

    def test_it_only_hides(self):
        """A boot-time rule that also positions or colours could change what a popup finally looks
        like. It is allowed to do exactly one thing."""
        rule = next(r for r in self._rules() if "display:none" in r)
        body = rule[rule.index("{") + 1: rule.rindex("}")]
        decls = [d.strip() for d in body.split(";") if d.strip()]
        self.assertEqual(len(decls), 1, f"the shield does more than hide: {decls}")


class TestAFailedPopupIsNotSilent(unittest.TestCase):
    def test_the_dispatch_reports_instead_of_swallowing(self):
        body = OS_JS[OS_JS.index("function restore()"):]
        body = body[: body.index("A POPOUT IS NOT A DESKTOP")]
        # The DISPATCH's own catch -- not the guards inside it, which may legitimately swallow.
        dispatch = body[body.index("if(popupKind()){"):body.index("\n      return;")]
        # The dispatch's OWN catch, identified by its indentation -- the guards nested inside it are
        # deeper and may legitimately swallow.
        outer = dispatch[dispatch.index("\n      }catch"):]
        self.assertNotIn("catch(_)", outer.split("{", 1)[0],
                         "a popup that fails to render still does so silently")
        self.assertIn("console.warn", outer)
        self.assertIn("popupKind()", outer.split("console.warn", 1)[1][:120],
                      "the warning does not say WHICH popup failed")


if __name__ == "__main__":
    unittest.main()
