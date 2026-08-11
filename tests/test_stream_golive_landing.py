"""Where you end up after pressing Go Live, and what plays when you get there.

Run: venv-unified/bin/python -m pytest tests/test_stream_golive_landing.py

Asked for as: "after you go live, it should bring you to your Stream in Discover-Stream. Maybe
automatically minimize the stream window and bring you there?" — and then, narrowing it:
"desktop/classic i mean, mobile stays same".

Two things are being pinned, and they pull in opposite directions:

  1. EVERY go-live route has to land you on your own stream. There are four of them (the OBS modal,
     the "your OBS feed is already ingesting — announce it" banner, the WHIP camera/screen path, and
     the native Android screen share) and they were each written at a different time. Two ended on
     `switchView('streams')` — the LIST — and two ended on a toast and a full-screen preview of your
     own face with the chat unreachable. A fifth path added later that forgets this is exactly how
     the feature comes apart, and it would look like nothing at all: you go live, it works, you are
     simply somewhere else.

  2. …and the page you land on must NOT play you back. You are broadcasting from this device: the
     audio returns through the speakers into the open mic about ten seconds later, and the browser
     downloads the same video it is uploading — on a tethered laptop that is the upload budget. So
     `selfLive` suppresses the player, and the way back in is one opt-in button that starts muted.

Source assertions, because the alternative is a headless browser with WebRTC, a signer and a relay to
drive a real broadcast. What regresses silently here is the WIRING.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def app():
    return (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")


def test_every_go_live_path_lands_you_on_your_stream(app):
    """All four call the ONE helper. Counting them is the point: a new path is a new call site."""
    assert len(re.findall(r"_afterGoLive\(", app)) >= 5, (
        "a go-live path no longer routes through _afterGoLive (4 call sites + the definition)")
    # The one that used to end at the list must no longer do so on its own.
    obs = app[app.index("$('#gl-go',root).onclick"):]
    obs = obs[:obs.index("};")]
    assert "_afterGoLive(ev" in obs and "switchView('streams')" not in obs, (
        "the OBS Go Live button still navigates to the streams LIST by hand")


def test_publish_hands_back_the_event_so_there_is_a_stream_to_open(app):
    """Opening "your stream" needs the event. `publish()` answers {ok, ev}; without returning it the
    landing could only ever be the list again."""
    body = app[app.index("async function _publishLive("):]
    body = body[:body.index("\n  /* WHERE YOU LAND")]
    assert re.search(r"return r\.ev;", body), "_publishLive does not return the published event"


def test_mobile_is_left_exactly_as_it_was(app):
    """On a phone the full-screen overlay IS the interface — Stop, Mute, Flip and Chat are its
    buttons. Shrinking it to a corner thumbnail to make room for a page nobody asked for takes the
    controls away at the moment they are most needed."""
    body = app[app.index("function _afterGoLive("):]
    body = body[:body.index("\n  }") + 4]
    assert "if(!isDesktop()){" in body, "_afterGoLive is not gated to desktop"
    # The mobile branch must return before minimising or opening anything.
    mob = body[body.index("if(!isDesktop()){"):]
    mob = mob[:mob.index("}")]
    assert "_setMiniLive" not in mob and "openStream" not in mob, (
        "the mobile branch minimises the overlay or opens a stream page — mobile stays the same")
    # …and the OBS-on-mobile behaviour (straight to the list) is preserved.
    assert "switchView('streams')" in mob


def test_the_overlay_is_minimised_rather_than_closed(app):
    """The broadcast lives in the PeerConnection, not the DOM — but the overlay is also the only
    Stop button. Minimising keeps it reachable; removing it would strand a live broadcast."""
    body = app[app.index("function _afterGoLive("):]
    body = body[:body.index("\n  }") + 4]
    assert "_setMiniLive(true)" in body
    assert "_endLive" not in body and "remove()" not in body


def test_your_own_live_stream_does_not_play_itself_back(app):
    """The echo and the wasted upload. `attachStream` must not run for a stream this device is
    broadcasting — not merely be muted, because hls.js downloads the whole thing regardless."""
    assert re.search(r"const selfLive = isMine && st==='live' && !!\(_liveStream \|\| _phoneStream\)",
                     app), "the self-broadcast condition is gone or changed shape"
    assert "} else if(url && !selfLive){ attachStream(url); }" in app, (
        "the live player attaches even when you are the one broadcasting")


def test_the_preview_is_opt_in_and_starts_muted(app):
    """"Is my encoder actually sending frames" is a real question, so there is a way in — but the
    echo is the reason the player is off, so one click must not be able to cause it."""
    i = app.index("const pv=$('#st-selfprev')")
    body = app[i:i + 600]
    assert "v.muted=true" in body and "v.volume=0" in body, (
        "the self-preview can start unmuted, which is the exact feedback loop this avoids")
    assert "attachStream(url)" in body, "the preview button does not actually attach a player"


def test_the_watch_link_survives_a_refused_clipboard(app):
    """This is the one string a streamer has to be able to get out of the app, and
    navigator.clipboard is refused outright on an insecure origin and in some WebViews."""
    i = app.index("const cl=$('#st-selflink')")
    body = app[i:i + 400]
    assert "_copyFallback(" in body, "a refused clipboard silently copies nothing"


def test_opening_a_stream_tells_the_desktop_what_the_window_holds(app):
    """openStream sets VIEW directly instead of going through renderView, which is where noteView
    normally runs — so the window went on believing it held the streams LIST, and any repaint (a
    drag, a focus) threw the stream away. Going live now lands here, so it is reached far more."""
    body = app[app.index("function openStream(e){"):]
    body = body[:body.index("feed.innerHTML=")]
    assert "PCOS.noteView('stream')" in body, (
        "the Streams window still thinks it is showing the list after opening a stream")
