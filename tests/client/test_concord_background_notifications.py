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
    assert "if(document.body.classList.contains('concord-view'))preserveChatScroll" in body
    assert "else saveTestMessages(storeId,next)" in body
