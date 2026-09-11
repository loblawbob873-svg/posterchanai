from pathlib import Path


JS = (Path(__file__).resolve().parents[2] / "static/js/client/concord.js").read_text()


def _refresh():
    """THE WHOLE LIVE PATH, which is two functions now.

    absorbChatWraps (merge, mention notifications, paint) sits immediately above
    refreshActiveChannel (decide, fetch) so that one merge serves both the periodic tick and a
    pushed subscription event. Slicing from the first of them keeps these assertions pointed at
    the behaviour rather than at a function name."""
    return JS.split("async function absorbChatWraps", 1)[1].split(
        "async function refreshRoomMetadata", 1
    )[0]


def test_active_concord_channel_keeps_polling_while_another_desktop_app_is_focused():
    body = _refresh()
    assert "state.community==null" in body
    assert "state.community==null||!document.body.classList.contains('concord-view')" not in body
    assert "notifyMentions(p,room,next,viewer,me,channel.name)" in body
    assert "PCOS.parkedSlot('concord')" in body


def test_background_poll_persists_without_repainting_the_foreground_app():
    body = _refresh()
    # THE RULE, WHICH HAS NOT MOVED: a merge always PERSISTS, and only PAINTS when this view is on
    # screen. It used to be written as a save inside the repaint plus an `else` branch that saved —
    # two copies of one statement, which is how they come to disagree. The save is unconditional
    # now and sits in front of the gate, so a background poll cannot lose it.
    assert body.count("saveTestMessages(storeId,next)") == 1, (
        "the save is written twice again — one copy will eventually not be reached")
    save_at = body.index("saveTestMessages(storeId,next)")
    gate = body.index("document.body.classList.contains('concord-view')")
    assert save_at < gate, (
        "the save moved behind the on-screen gate, so a poll while Concord is not showing drops the "
        "messages it just merged")
    assert "preserveChatScroll" in body, "an on-screen merge no longer repaints the room"
    # …AND THE PAINT WAITS FOR THE READER'S HAND. Replacing the rows kills a momentum scroll, so a
    # busy room repainting on every arriving message is a series of dead flings.
    assert "whenHandLeaves(" in body, (
        "a live message repaints the room mid-flick again — 'it constantly jerks me to different "
        "positions'")
    # …AND THE ON-SCREEN TEST IS MADE TWICE. Waiting for the hand separates the request from the
    # paint, so "Concord is showing" can stop being true in between; a held repaint that does not
    # re-ask would draw a room over whatever the reader navigated to.
    held = body[body.index("whenHandLeaves("):]
    held = held[:held.index("preserveChatScroll")]
    assert "concord-view" in held, (
        "a deferred repaint no longer re-checks that Concord is still on screen when it fires")
