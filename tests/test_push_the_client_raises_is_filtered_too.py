"""A DEVICE'S ANSWER MUST GOVERN EVERY DOOR, and two of them were standing open.

The complaint is one sentence and it has been made repeatedly: "I am still getting Android push
notifications for stuff I said not to push." Measured on production, the reporting phone's row was
NOT the documented failure — `PushSubscription.prefs` was populated, with eight of the twelve types
explicitly `false`. The client had told the server. So the mirror was working and something else was
buzzing, which is what this file pins.

TWO senders never asked the question.

1. **The DM push.** `nostr_push_service._dm_handler` is the only sender in the subsystem that never
   called `push_prefs.allows_row` — and it could not have, because `_subs_for` hands it
   `direct_push_service.subscription_dict`, which dropped the `prefs` column on the floor.
   `getattr({...}, "prefs", None)` is None, which the gate reads as "never configured" and answers
   SEND to. Every device that switched Direct messages off went on being pushed for them, on every
   node, with its own row in the database saying otherwise.

2. **The notification the RUNNING CLIENT raises natively.** `PushPlugin.notify` draws through
   `PushEventService.show` — the very same builder, channel and tag scheme a server push uses — and
   never consulted `DirectPushStore`. Only `PushEventService.deliver` (the server path) did. On the
   packaged app the WebView is alive most of the time (a foreground direct-push service, "stay
   connected", and the launcher build IS the home app), so the phone kept drawing an OS notification
   for every like the user had just silenced, visually indistinguishable from the push. Two filters
   guarding the server path and a third door beside it with nothing on it.

Each test below was verified red against the code as it stood.
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.services import direct_push_service, nostr_push_service as nps, push_prefs
from tests.client_source import client_source

ROOT = Path(__file__).resolve().parents[1]
PUSH = ROOT / "mobile/android/app/src/main/java/place/poster/app/push"

PK = "a" * 64
WRAP = "c" * 64


class _Sub:
    """Just enough PushSubscription for `_subs_for` to convert and the handler to send to."""
    def __init__(self, endpoint, prefs=None):
        self.id = endpoint
        self.pubkey = PK
        self.endpoint = endpoint
        self.transport = "webpush"
        self.device_id = None
        self.token_hash = None
        self.p256dh = "p"
        self.auth = "a"
        self.prefs = prefs


class _DB:
    def __init__(self, subs):
        self._subs = subs
    def query(self, _model):
        return self
    def filter(self, *_a, **_k):
        return self
    def all(self):
        return list(self._subs)
    def close(self):
        pass


def _async(value):
    async def go():
        return value
    return go()


def _run_dm(monkeypatch, subs):
    """Drive the REAL `_dm_handler` over a gift wrap, through the REAL `subscription_dict`."""
    sent = []
    monkeypatch.setattr("app.database.SessionLocal", lambda: _DB(subs))
    monkeypatch.setattr(nps, "_subscriber_pks", lambda: _async({PK}))
    monkeypatch.setattr(nps, "_sent_by_own_device", lambda pks, wid: set())
    monkeypatch.setattr(nps.push_service, "send",
                        lambda sub, payload: sent.append((sub.get("endpoint"), payload)) or True)
    nps._dm_recent.clear()
    # kind 1059: a gift wrap, signed by a throwaway key and p-tagged to the real recipient.
    asyncio.run(nps._dm_handler({"id": WRAP, "kind": 1059, "pubkey": "f" * 64,
                                 "created_at": 100, "tags": [["p", PK]], "content": "", "sig": ""}))
    return sent


# ---- 1. the DM push ------------------------------------------------------------------------------

def test_the_prefs_column_survives_the_trip_to_the_handler():
    """`subscription_dict` is where the answer was lost, so it is where the first assertion goes.

    A gate that is called on a shape it cannot read is worse than no gate: it reports success and
    decides nothing, which is exactly how this survived a feature explicitly built to stop it.
    """
    wire = direct_push_service.subscription_dict(_Sub("https://x", '{"dm": false}'))
    assert wire["prefs"] == '{"dm": false}'
    assert push_prefs.allows_row(wire, "dm") is False, \
        "the gate cannot read a subscription dict, so it fails open on every device"


def test_a_phone_that_switched_direct_messages_off_is_not_pushed_for_one(monkeypatch):
    quiet = _Sub("https://phone", json.dumps({"dm": False, "likes": True}))
    loud = _Sub("https://desktop", json.dumps({"dm": True}))
    sent = _run_dm(monkeypatch, [quiet, loud])
    assert [e for e, _ in sent] == ["https://desktop"], \
        "the DM handler ignored the preference the row carries"


def test_an_unconfigured_device_still_gets_its_dm(monkeypatch):
    """FAIL OPEN, the rule the whole subsystem is built on: a missing preference means SEND.

    A silenced alert is indistinguishable from a lost one, and the expensive mistake here is a
    direct message that never arrived. Every row this node has ever written predates the mirror.
    """
    for prefs in (None, "", "{ not json", "{}", json.dumps({"likes": False})):
        sent = _run_dm(monkeypatch, [_Sub("https://phone", prefs)])
        assert [e for e, _ in sent] == ["https://phone"], "silenced a DM over %r" % (prefs,)


def test_the_payload_is_still_the_one_the_phone_knows_how_to_route(monkeypatch):
    """The filter must not disturb the contract the notification depends on — `view` is the tap
    target and `tag` is what makes this and the client's decrypted card ONE notification."""
    sent = _run_dm(monkeypatch, [_Sub("https://phone", json.dumps({"dm": True}))])
    assert sent and sent[0][1]["type"] == "dm"
    assert sent[0][1]["view"] == "messages" and sent[0][1]["tag"] == "pc-dm"


# ---- 2. the notification the client raises --------------------------------------------------------

def _method(src, signature, end):
    body = src[src.index(signature):]
    return body[:body.index(end)]


def test_a_notification_raised_by_the_web_layer_asks_the_device_first():
    """`PushPlugin.notify` and `PushEventService.deliver` draw with the SAME builder. Only one of
    them used to ask, and the one that did not is the one that runs while the app is alive."""
    plugin = (PUSH / "PushPlugin.java").read_text()
    notify = _method(plugin, "public void notify(PluginCall call)", "public void setStayConnected")
    assert "DirectPushStore.allowsType(getContext(), type)" in notify, \
        "the client's own native notification bypasses the device's preferences entirely"
    assert notify.index("DirectPushStore.allowsType") < notify.index("PushEventService.show("), \
        "the check must come BEFORE the notification is drawn"


def test_a_silenced_client_notification_resolves_rather_than_rejecting():
    """Not drawing because the user said not to is a SUCCESS. A rejection lands in the client's
    catch, where it is indistinguishable from "notifications are broken on this device"."""
    plugin = (PUSH / "PushPlugin.java").read_text()
    notify = _method(plugin, "public void notify(PluginCall call)", "public void setStayConnected")
    gate = notify[notify.index("DirectPushStore.allowsType(getContext(), type)"):]
    gate = gate[:gate.index("ClientNotified")]
    assert "call.resolve(" in gate and "call.reject(" not in gate, gate


def test_the_silenced_notification_is_not_recorded_as_one_the_client_spoke_for():
    """`ClientNotified.dm` means "the client drew a better card, do not draw the blind one". A
    notification that was never drawn must not claim that, or the record would suppress on behalf of
    nothing. (It is moot while both ends read the same prefs — and it stops being moot the moment
    they disagree, which is the entire reason the device keeps its own copy.)"""
    plugin = (PUSH / "PushPlugin.java").read_text()
    notify = _method(plugin, "public void notify(PluginCall call)", "public void setStayConnected")
    assert notify.index("DirectPushStore.allowsType") < notify.index("ClientNotified.dm(")


# ---- 3. the half this worker does not own ---------------------------------------------------------

def test_the_two_preference_sets_stay_separate():
    """The device PUSH answers must not veto an APP ALERT while the app is on screen.

    DirectPushStore holds the push answer, and its pane says verbatim that it is "a separate answer
    from the app alerts above". A notification raised by a RUNNING client is an app alert, already
    gated client-side by notificationAllowed(). Applied unconditionally, the closed-app answer
    silences the in-app one on the only platform where this is the sole OS-notification path — so
    somebody who turns Likes off for push and leaves it on for app alerts would get nothing, ever,
    with the App-alerts switch left decorative and nothing on screen to say so.

    Backgrounded it IS standing in for a push (same builder, channel and tag), so there the push
    answer is the right one to ask.
    """
    java = (ROOT / "mobile/android/app/src/main/java/place/poster/app/push/PushPlugin.java").read_text()
    assert "boolean foreground = call.getBoolean(\"foreground\"" in java, \
        "the plugin cannot tell an app alert from a stand-in push"
    assert "!foreground && !DirectPushStore.allowsType(" in java, \
        "the per-device push filter is applied to on-screen app alerts too"
    # …and the client has to actually send it, or the default (false) filters everything.
    app = client_source()
    notify = app[app.index("_capPlugin('PosterChanPush', 'notify')"):]
    notify = notify[:notify.index("return null;")]
    assert "foreground:" in notify, "osNotify does not tell the plugin whether the app is on screen"
    assert "document.hidden" in notify, "the foreground flag is not derived from page visibility"


def test_the_client_tells_the_plugin_which_kind_of_notification_this_is():
    app = client_source()
    fn = app[app.index("  function osNotify(title, body, opts){"):]
    # Bounded by its own closing brace: reminderAlert, which used to follow it, moved to ai.js.
    fn = fn[:fn.index("\n  }\n") + 4]
    call = fn[fn.index("const P = _capPlugin('PosterChanPush', 'notify');"):]
    assert "_notificationType(opts)" in call, \
        "the resolved type never reaches the native gate, so it can only ever govern 'dm' and 'reminders'"
