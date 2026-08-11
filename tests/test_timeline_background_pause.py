"""The timeline must stop streaming when a PHONE is put away — and must not when a desktop is.

Run: venv-unified/bin/python -m pytest tests/test_timeline_background_pause.py

Two requirements that pull in opposite directions, and both have been reported:

  * "for battery efficiency, we don't want the timeline to continue loading on the phone when not
    active" — a phone in a pocket receiving the firehose is radio time spent on nothing.
  * "not showing new posts when other window is focused" — Chromium reports `hidden` for a desktop
    window that is merely COVERED by another one (native occlusion), so the same rule applied there
    tore the timeline down twenty seconds after anything was put in front of it. A covered window is
    still an app someone is running, and the desktop app can sit in the tray precisely so it keeps up
    out of sight.

The pause became MORE important the day "stay connected" shipped, not less, and that is the part
worth pinning. It used to be armed only from `visibilitychange`, and a phone that never delivered
that event was harmless because the OS froze the process a moment later and nothing was streaming
anyway. A KEPT-ALIVE process is not frozen: a missed or coalesced event would leave the firehose
subscription open over a live socket, in a pocket, indefinitely — the exact cost the pause exists to
avoid, introduced by the thing that keeps the app running.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPJS = (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")


def test_a_backgrounded_phone_drops_the_timeline():
    assert "function _tlBackground(){" in APPJS
    i = APPJS.index("function _tlBackground(){")
    body = APPJS[i:i + 400]
    assert "_tlPause" in body and "_TL_HIDE_AFTER" in body


def test_the_pause_actually_closes_the_subscription():
    """A flag that stopped RENDERING would still be receiving every event on the wire."""
    i = APPJS.index("_tlPause = ()=>{")
    body = APPJS[i:i + 240]
    assert "Relay.close(cur)" in body, "the timeline sub is not closed, only ignored"


def test_the_desktop_app_is_exempt():
    """The occlusion trap. This is the assertion that keeps "battery on a phone" from becoming "the
    Social window stops updating whenever you look at something else"."""
    i = APPJS.index("function _tlBackground(){")
    body = APPJS[i:i + 400]
    assert "if(_isDesktopApp()) return;" in body, "a covered desktop window loses its timeline"
    assert body.index("_isDesktopApp") < body.index("_tlHideTimer"), (
        "the timer is armed before the desktop check, so it fires anyway")


def test_desktop_mode_in_a_browser_is_not_special_cased_by_accident():
    """PosterChan OS is a LAYOUT, not a platform: in a browser tab, hidden still means hidden, and
    parking a window inside the desktop does not touch document.hidden. Nothing here should be
    reaching for PCOS."""
    i = APPJS.index("function _tlBackground(){")
    body = APPJS[i:APPJS.index("function _tlForeground(){")]
    assert "PCOS" not in body and "isOn()" not in body


def test_both_background_signals_arm_it():
    """`visibilitychange` is the least reliable of them on a frozen or kept-alive Android process."""
    assert len(re.findall(r"_tlBackground\(\)", APPJS)) >= 3, (
        "only one signal arms the pause (definition + visibilitychange + appStateChange expected)")
    i = APPJS.index("addListener('appStateChange'")
    body = APPJS[i:i + 700]
    assert "_tlBackground()" in body, "the native background signal does not drop the timeline"
    assert "st.isActive" in body


def test_returning_resumes_it_and_cancels_a_pending_pause():
    i = APPJS.index("function _tlForeground(){")
    body = APPJS[i:i + 300]
    assert "clearTimeout(_tlHideTimer)" in body, (
        "a pause armed just before returning would fire after you are back")
    assert "_tlResume" in body
