"""The sandbox loader relays the app's JSON-RPC replies instead of eating them.

The loader and the app both talk JSON-RPC to the client over the same channel, and both
number their requests from 1. The loader used to treat every reply coming down as an
answer to one of its OWN requests and drop it when the id was not in its table — which is
every reply the app was waiting for.

Nothing about that is visible from outside. A promise that never settles throws nothing,
logs nothing and fails no check: `joinRealtimeChannel()` never resolved, so the bridge's
send() parked every packet behind a `.then()` that could not run. An app could send one a
frame for ever and not a byte left the sandbox — multiplayer dead in every mini app while
single-player looked perfect, because single-player asks the host for nothing.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOADER = (ROOT / "static/webxdc-sandbox/index.html").read_text()

REPLY_BRANCH = LOADER[LOADER.index("if(d.id !== undefined && d.method === undefined){"):
                      LOADER.index("// Anything else is for the app")]


def test_a_reply_the_loader_did_not_ask_for_goes_down_to_the_app():
    assert "hasOwnProperty.call(pending, d.id)" in REPLY_BRANCH
    # The unmatched case must FORWARD, and must do so before resolving anything of ours.
    assert "postMessage(d, location.origin)" in REPLY_BRANCH
    fwd = REPLY_BRANCH.index("postMessage(d, location.origin)")
    assert fwd < REPLY_BRANCH.index("delete pending[d.id]")
    assert "if(!p) return;" not in REPLY_BRANCH


def test_the_loaders_own_request_ids_cannot_collide_with_the_apps():
    # Two independent sequences on one channel: ours is marked, so a reply is never
    # ambiguous. Without this the loader can resolve its own file fetch with the answer
    # to the app's join, which is the same bug wearing the other hat.
    assert "var MINE = 'sbx:';" in LOADER
    assert "var id = MINE + (nextId++);" in LOADER
