"""The startup SCREEN pref (`startView`) must resolve safely at boot.

Run: venv-unified/bin/python -m unittest tests.client.test_start_view

Settings → Profile → "Screen the app opens on" lets the account land on Notes, Messages, Calendar…
instead of the timeline. The value is synced over Nostr (kind-30078 client-prefs), so at boot it can
name a view that ANOTHER device chose and THIS deployment gates away (instance gating, nostr-only,
standalone), a slug from a newer/older build, or plain garbage. `_startView()` is the single place
that decides, and its rule is: only a view the sidebar actually shows wins; everything else — guest
sessions, timeline tabs, hidden/absent rows, malformed values — falls back to `_startTimeline()`.

The shipped function is extracted from app.js and RUN under node against a stub DOM, so a regression
in the guard order (e.g. dropping the hidden-class check) fails here, not on a user's phone.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static" / "js" / "client" / "app.js"


def _close(src, open_idx):
    depth = 0
    for j in range(open_idx, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    raise AssertionError("unbalanced braces from offset %d" % open_idx)


def _fn(src, header):
    i = src.index(header) + len(header) - 1
    return src[src.index(header):_close(src, i) + 1]


def run_start_view(fn_src, *, guest, stored, visible, hidden=()):
    """Run the shipped _startView with a stub ClientSettings/document; returns its answer."""
    js = """
const GUEST = %s;
const ClientSettings = { get: (k, d) => (%s)[k] !== undefined ? (%s)[k] : d };
const VISIBLE = new Set(%s), HIDDEN = new Set(%s);
const document = { querySelector: (sel) => {
  const m = /\\.nav-item\\[data-view="([^"]*)"\\]/.exec(sel);
  if (!m) return null;
  if (VISIBLE.has(m[1])) return { classList: { contains: () => false } };
  if (HIDDEN.has(m[1]))  return { classList: { contains: (c) => c === 'hidden' } };
  return null;
} };
function _startTimeline(){ return 'TIMELINE'; }
%s
console.log(JSON.stringify(_startView()));
""" % (json.dumps(guest), json.dumps(stored), json.dumps(stored),
       json.dumps(list(visible)), json.dumps(list(hidden)), fn_src)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


class TestStartViewResolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text(encoding="utf-8")
        cls.fn = _fn(cls.src, "function _startView(){")

    def test_default_and_social_land_on_the_timeline(self):
        for stored in ({}, {"startView": "social"}):
            self.assertEqual(run_start_view(self.fn, guest=False, stored=stored,
                                            visible=["notes"]), "TIMELINE")

    def test_a_visible_view_wins(self):
        self.assertEqual(run_start_view(self.fn, guest=False, stored={"startView": "notes"},
                                        visible=["notes"]), "notes")

    def test_guest_always_gets_the_timeline(self):
        # Most screens need a key; a guest landing on Notes is a blank screen with no way in.
        self.assertEqual(run_start_view(self.fn, guest=True, stored={"startView": "notes"},
                                        visible=["notes"]), "TIMELINE")

    def test_a_gated_or_absent_view_falls_back(self):
        # Instance-gated (row present but .hidden — standalone/nostr-only) and a slug this build
        # simply doesn't have: both are another device's choice this deployment can't honour.
        self.assertEqual(run_start_view(self.fn, guest=False, stored={"startView": "terminal"},
                                        visible=[], hidden=["terminal"]), "TIMELINE")
        self.assertEqual(run_start_view(self.fn, guest=False, stored={"startView": "newthing"},
                                        visible=["notes"]), "TIMELINE")

    def test_timeline_tabs_and_garbage_go_through_start_timeline(self):
        # 'home'/'global' must respect the hidden-tab rules _startTimeline owns, and a synced value
        # is attacker-shaped input for a querySelector — anything outside the slug alphabet is out.
        for v in ("home", "global", 'x"]{}', "a" * 40, ""):
            self.assertEqual(run_start_view(self.fn, guest=False, stored={"startView": v},
                                            visible=[v]), "TIMELINE")

    def test_wiring(self):
        # The boot landing resolves through _startView (not _startTimeline), the pref is saved AND
        # restored under the same key, and the restore never calls switchView (adopt-only — a synced
        # value must not yank the screen mid-session).
        self.assertIn("switchView(_startView()); _onLandingView = true;", self.src)
        self.assertIn("saveClientPrefsNostr({ startView: v })", self.src)
        m = re.search(r"_prefTouched\.has\('startView'\)[^\n]*\n[^\n]*ClientSettings\.set\('startView'", self.src)
        self.assertIsNotNone(m, "restoreClientPrefsNostr must adopt a synced startView (guarded by _prefTouched)")


if __name__ == "__main__":
    unittest.main()
