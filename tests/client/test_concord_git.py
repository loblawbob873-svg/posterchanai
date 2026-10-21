"""Concord × git (Armada parity): a channel can follow NIP-34 repositories.

Wire half (cord-reader.js): Armada's `custom["armada.git"]` attachment intervals, written and read by
the SHIPPED builders — concord_git_attach_runtime.mjs. Activity half (concord-git.js): Armada's
timeline/trust/deletion/CI rules and its exact event templates against real signed events —
concord_git_core_runtime.mjs. The browser half (rows in a channel, the ticket panel, Projects, the
picker, phone and desktop layouts) is test_concord_git_full_app.py.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GIT = (ROOT / "static/js/client/concord-git.js").read_text(encoding="utf-8")
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def _node(script):
    r = subprocess.run(["node", str(ROOT / script)], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr


def test_attachments_are_written_and_read_the_way_armada_does():
    _node("tests/client/concord_git_attach_runtime.mjs")


def test_activity_rules_and_templates_match_armada():
    _node("tests/client/concord_git_core_runtime.mjs")


def test_git_relays_are_dialled_only_through_the_concord_door():
    """Concord talks only to this node: every relay it touches goes through `window.PCConcordRelay`
    (tests/test_concord_relay_calls_go_through_the_tunnel.py). A repository's relays are relays
    Concord touches, so this module must never reach `window.Relay` or a pool method directly."""
    assert "window.PCConcordRelay" in GIT
    assert not re.search(r"window\.Relay\b", GIT), "concord-git.js dials around the tunnel"
    for raw in ("relayQueryFrom", "relayPublishTo", "Relay.query(", "Relay.publish(", "new WebSocket"):
        assert raw not in GIT, f"concord-git.js reaches relays through {raw}"
    # Everything it reads or sends goes through these two door methods.
    assert "door.queryFrom(" in GIT and "door.publishTo(" in GIT


def test_concord_hands_the_attachments_through_and_draws_the_rows():
    # Both control folds carry the attachment list onto the saved channel …
    assert CONCORD.count("repositories:Array.isArray(c.repositories)?c.repositories:[]") == 2
    # … the message pane interleaves git rows (and never inside a thread view) …
    assert "if(!state.thread) return _gitHere?PCConcordGit.paneRows(" in CONCORD
    assert "if(m&&m.ccGit)return m.html;" in CONCORD
    # … and git activity counts toward a channel's unread dot.
    assert CONCORD.count("gitUnread(room,c)") >= 3


def test_the_new_script_ships_everywhere_concord_does():
    html = (ROOT / "templates/client.html").read_text(encoding="utf-8")
    sw = (ROOT / "static/js/client/sw.js").read_text(encoding="utf-8")
    # Loaded by concord.js on its first paint (loadConcordGit), not on the app's cold load.
    assert "concord-git.js" not in html, "concord-git.js is on the cold load again (boot payload budget)"
    concord = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
    assert "if(!window.PCConcordGit)loadConcordGit();" in concord
    assert "'/static/js/client/concord-git.js'," in sw
    assert "concord-git.js" in (ROOT / "scripts/bump_desktop_overlay.py").read_text(encoding="utf-8")


def test_a_gif_community_icon_keeps_its_animation():
    _node("tests/client/concord_gif_icon_runtime.mjs")
