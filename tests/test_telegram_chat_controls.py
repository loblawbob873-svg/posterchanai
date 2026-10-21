"""Telegram chat controls: block, mute / notify without sound, auto-delete (2026-10-21: "no way to block, mute
notification sounds from user, set chat expiration").

Driven through the SHIPPED Manager with the suite's fake client, extended here with what these controls use: an
input entity and raw Telegram requests -- every request is recorded, so each test asserts the exact Telethon
request Telegram receives, not just a return value.
"""
import datetime
import time

import pytest

from tests.test_telegram_client import FakeClient, M, U, login, run, world  # noqa: F401  (fixture)


class _NS:
    def __init__(self, mute_until=None, silent=False):
        self.mute_until, self.silent = mute_until, silent


@pytest.fixture
def tg(world, monkeypatch):
    mgr, tgs, store, made = world
    sent = []
    state = {"ns": _NS(), "user": True, "ttl": 0, "blocked": False, "refuse": None}

    async def get_input_entity(self, chat):
        return ("peer", int(chat))

    async def call(self, req):
        name = type(req).__name__
        sent.append(req)
        if state["refuse"] and name == state["refuse"][0]:
            raise state["refuse"][1]
        if name == "GetNotifySettingsRequest":
            return state["ns"]
        if name == "GetFullUserRequest":
            if not state["user"]:
                raise ValueError("not a user")
            return type("F", (), {"full_user": type("U", (), {"blocked": state["blocked"], "ttl_period": state["ttl"]})()})()
        if name in ("GetFullChannelRequest", "GetFullChatRequest"):
            return type("F", (), {"full_chat": type("C", (), {"ttl_period": state["ttl"]})()})()
        return True
    monkeypatch.setattr(FakeClient, "get_input_entity", get_input_entity, raising=False)
    monkeypatch.setattr(FakeClient, "__call__", call, raising=False)
    login(mgr, U(1), tgs[1])
    return mgr, sent, state


def test_block_and_unblock_reach_telegram(tg):
    mgr, sent, _ = tg
    assert run(mgr.set_blocked(None, U(1), 42, True)) == {"blocked": True}
    assert type(sent[-1]).__name__ == "BlockRequest" and sent[-1].id == ("peer", 42)
    run(mgr.set_blocked(None, U(1), 42, False))
    assert type(sent[-1]).__name__ == "UnblockRequest"


def test_mute_for_a_while_forever_unmute_and_silent(tg):
    mgr, sent, _ = tg
    run(mgr.set_notify(None, U(1), 42, mute=3600))
    req = next(r for r in reversed(sent) if type(r).__name__ == "UpdateNotifySettingsRequest")
    left = req.settings.mute_until.timestamp() - time.time()
    assert 3500 < left <= 3600, left
    run(mgr.set_notify(None, U(1), 42, mute=-1))
    req = next(r for r in reversed(sent) if type(r).__name__ == "UpdateNotifySettingsRequest")
    assert req.settings.mute_until.year == 2038, "mute until turned back on"
    run(mgr.set_notify(None, U(1), 42, mute=0))
    req = next(r for r in reversed(sent) if type(r).__name__ == "UpdateNotifySettingsRequest")
    assert req.settings.mute_until.timestamp() <= time.time() + 2, "unmute = a mute that ends now"
    run(mgr.set_notify(None, U(1), 42, silent=True))
    req = next(r for r in reversed(sent) if type(r).__name__ == "UpdateNotifySettingsRequest")
    assert req.settings.silent is True and req.settings.mute_until is None, "sound off must not touch the mute"
    with pytest.raises(M.TGError):
        run(mgr.set_notify(None, U(1), 42))


def test_an_ended_mute_is_not_a_mute(tg):
    mgr, _, state = tg
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    state["ns"] = _NS(mute_until=past)
    s = run(mgr.peer_settings(None, U(1), 42))
    assert s["muted"] is False and s["muted_until"] == 0
    state["ns"] = _NS(mute_until=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1), silent=True)
    s = run(mgr.peer_settings(None, U(1), 42))
    assert s["muted"] is True and s["silent"] is True and s["kind"] == "user"


def test_auto_delete_periods_and_a_group_refusal(tg):
    mgr, sent, state = tg
    assert run(mgr.set_ttl(None, U(1), 42, 604800)) == {"ttl": 604800}
    assert type(sent[-1]).__name__ == "SetHistoryTTLRequest" and sent[-1].period == 604800
    with pytest.raises(M.TGError):
        run(mgr.set_ttl(None, U(1), 42, 12345))
    state["refuse"] = ("SetHistoryTTLRequest", Exception("CHAT_ADMIN_REQUIRED"))
    with pytest.raises(M.TGError) as e:
        run(mgr.set_ttl(None, U(1), -100, 86400))
    assert "admin" in str(e.value)


def test_a_group_reports_its_timer_and_is_not_blockable(tg):
    mgr, _, state = tg
    state["user"] = False; state["ttl"] = 86400
    s = run(mgr.peer_settings(None, U(1), -100))
    assert s["kind"] == "chat" and s["ttl"] == 86400 and s["blocked"] is False


def test_the_client_honours_mute_and_silent_and_retries():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "static/js/client/telegram.js").read_text()
    notify = js[js.index("  function notify(m, chat){"):][:1200]
    assert "if(d && d.muted) return;" in notify, "a muted chat must not ring"
    assert "silent: !!(d && d.silent)" in notify
    assert "setTimeout(() => { const e = new Error('Telegram did not answer'); e.offline = true; no(e); }, 15000)" in js
    assert js.count("scheduleRetry();") >= 3, "the error card, the chat list and the device copy all retry"
    app = (Path(__file__).resolve().parents[1] / "static/js/client/app.js").read_text()
    assert "silent: !!(opts && opts.silent)," in app
    java = (Path(__file__).resolve().parents[1] / "mobile/android/app/src/main/java/place/poster/app/push/PushEventService.java").read_text()
    assert 'silent ? place.poster.app.ringtone.RingtoneRules.messagesChannel("off")' in java


def test_the_terminal_clears_its_retry_note_when_a_retry_works():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "static/js/client/term.js").read_text()
    ref = js[js.index("    async function _hostsRefresh(){"):][:1200]
    assert ref.index("_paintHosts();") < ref.index("_clearHostRetry();") < ref.index("}catch(_){")
    load = js[js.index("    async function loadHosts(){"):]
    assert "_clearHostRetry();\n        return true;" in load
