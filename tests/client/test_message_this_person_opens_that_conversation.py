"""Choosing Message on a profile opens THAT conversation, in ONE window.

Reported: "clicking on hamburger menu on their profile and choose message and then on posterchanOS,
two messages windows appear and no DM window to the user".

Two independent bugs, and each produced one half of that sentence:

* **The second window.** `openApp`'s lookup for an already-open real toplevel compared identities
  literally (`r.view === view`) while the in-page lookup beside it asks `sameAppWindow`, which knows
  Direct Messages and Communities are two tabs of ONE application. With Communities open as a
  toplevel, asking for Messages matched nothing and opened another one.
* **The missing conversation.** The old action set `dmActive` in the page that was clicked and then
  called `switchView('messages')`. When Messages is a real toplevel that is a DIFFERENT RENDERER —
  same origin, no shared variables — so it opened on the conversation list. The one thing asked for
  did not happen.
"""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
OSWIN = (ROOT / "static/js/client/oswin.js").read_text(encoding="utf-8")


def test_the_native_lookup_knows_messages_and_communities_are_one_app():
    """Both halves of one lookup must not answer differently."""
    line = next(ln for ln in OS_JS.splitlines()
                if "nativeTasks.find(" in ln and "r.own" in ln)
    assert "sameAppWindow(" in line, line
    assert "r.view === view" not in line, line


def test_the_profile_action_names_one_route_instead_of_setting_a_variable():
    action = next(ln for ln in APP_JS.splitlines() if "a==='message'" in ln)
    assert "openDMWith(" in action, action
    assert "dmActive=" not in action and "dmActive =" not in action, (
        "setting a variable in THIS page is exactly what does not reach another renderer: " + action)


def test_the_route_carries_the_peer_and_the_receiver_validates_it():
    assert "function routeExisting(view, arg)" in OSWIN
    assert "openDMWith" in OSWIN, "the receiver must be able to act on the peer it was handed"
    body = APP_JS.split("function openDMWith(pk){", 1)[1][:900]
    assert re.search(r"\^\[0-9a-f\]\{64\}\$", body), (
        "this arrives over a BroadcastChannel shared by every window on the origin: " + body[:200])


def test_a_delivered_route_stops_the_caller_painting_here_as_well():
    """Painting after the route was delivered puts the conversation in the wrong window — the same
    failure one layer along."""
    body = OS_JS.split("function routeApp(view, arg){", 1)[1][:1200]
    assert "return true;" in body and "return false;" in body, body[:300]
    caller = APP_JS.split("function openDMWith(pk){", 1)[1][:900]
    assert "PCOS.routeApp('messages', peer)) return true;" in caller, caller[:400]


def test_every_piece_is_reachable_from_the_shared_surface():
    """A helper that exists only inside a factory argument list is how `PC._fmtBytes is not a
    function` happened; see tests/client/test_pc_surface_exists.py."""
    assert re.search(r"^\s*openDMWith,", APP_JS, re.M), "openDMWith must be on the PC surface"
    assert "routeView, routeApp," in OS_JS, "routeApp must be on the PCOS surface"


def test_the_shipped_files_parse():
    for name in ("os.js", "app.js", "oswin.js"):
        got = subprocess.run(["node", "--check", str(ROOT / "static/js/client" / name)],
                             capture_output=True, text=True)
        assert got.returncode == 0, got.stderr
