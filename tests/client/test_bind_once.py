"""startApp() must not register global listeners/timers — it runs more than once per page load.

Run: venv-unified/bin/python -m unittest tests.client.test_bind_once

A first visit lands in read-only GUEST mode and logs in WITHOUT a page reload, so startApp() runs a
second time in the same document. Anything it registers on `document`, `window`, `#feed` or a timer is
then live TWICE for the rest of the session — the DOM only dedupes a repeated *named* handler, and
every one of these is a fresh arrow function.

That shipped as "opening an image on the timeline has to be closed twice": two #feed click delegates
turned one tap into two openLightbox() calls and two stacked .lightbox overlays. The same doubling hit
the rightbar (two reactions per tap), popstate (Back skipped a view) and the refresh intervals.

So the rule is structural, not per-symptom: registrations belong in bindGlobalsOnce(), which returns
early on its second call. The one sanctioned exception is the `if(!window.__pcNativeBound){ ... }`
latch, which cannot move — it closes over locals that startApp() must still re-run per login.
"""
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js"

# Anything that outlives the call: a second copy is a second handler, timer or observer. Not just
# document/window — the rightbar's listener was bound through a local `const rb`, and any node that
# survives the login (everything outside #feed does) doubles just the same.
BANNED = re.compile(r"\.addEventListener\(|\bsetInterval\(|\bnew \w*Observer\(")


def _close(src, open_idx):
    """Index of the `}` matching the `{` at open_idx (comments/strings are close enough here)."""
    depth = 0
    for j in range(open_idx, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    raise AssertionError("unbalanced braces from offset %d" % open_idx)


def _body(src, header):
    """The {...} body of `header` (which must end in its opening brace)."""
    i = src.index(header) + len(header) - 1
    return src[i + 1:_close(src, i)]


def _strip_native_latch(body):
    """Drop the one sanctioned `if(!window.__pcNativeBound){ ... }` block."""
    m = re.search(r"if\(!window\.__pcNativeBound\)\s*\{", body)
    if not m:
        return body
    return body[:m.start()] + body[_close(body, m.end() - 1) + 1:]


class TestStartAppBindsNothingGlobal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text(encoding="utf-8")

    def test_start_app_registers_no_global_listeners_or_timers(self):
        left = _strip_native_latch(_body(self.src, "function startApp(){"))
        hits = [ln.strip() for ln in left.split("\n") if BANNED.search(ln)]
        self.assertEqual(
            hits, [],
            "startApp() runs again on guest->login without a reload, so these are bound twice.\n"
            "Move them into bindGlobalsOnce():\n  " + "\n  ".join(hits))

    def test_bind_globals_once_is_latched_and_owns_the_binders(self):
        self.assertIn("bindGlobalsOnce();", self.src, "startApp() must still call bindGlobalsOnce()")
        once = _body(self.src, "function bindGlobalsOnce(){")
        self.assertIn("if(window.__pcGlobalsBound) return;", once,
                      "bindGlobalsOnce() must return early on its second call")
        # A binder called from anywhere else is the same bug one level down, where the test above
        # cannot see it — startApp()'s own body would stay clean while the listener still doubles.
        for fn in ("bindSearch", "bindFeedActions", "bindMobileGestures"):
            calls = self.src.count(fn + "();")
            self.assertEqual(calls, 1, f"{fn}() is called {calls}x — it must be called only by bindGlobalsOnce()")
            self.assertIn(fn + "();", once)


if __name__ == "__main__":
    unittest.main()
