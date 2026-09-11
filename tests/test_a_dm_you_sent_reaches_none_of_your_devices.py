"""A MESSAGE YOU SENT IS NOT NEWS TO ANY OF YOUR DEVICES — NOT JUST THE ONE THAT SENT IT.

Reported: "if I send a DM, i do not want a push notification saying that somebdy sent a DM" —
"it was me!". The second time, after a device-local fix.

NIP-17 publishes a SELF-COPY of every message so the sender's other devices see what they sent. It
is p-tagged to the sender, and a gift wrap's author is an ephemeral throwaway key, so the watcher's
"don't notify the author" test cannot see that this recipient IS the sender.

THE FIRST FIX WAS CORRECT AND INCOMPLETE. `ClientNotified` has the publishing device record the
wrap ids and drop the push carrying one — exact, and needing nothing from the server. But a device
only knows what IT published:

  * send from the desktop and the phone still buzzes: it published nothing and knows nothing;
  * and Web Push has no equivalent map at all — `sw.js` never looks at `wid` — so every browser
    device was told regardless of which one sent it. Measured on this deployment: of ten registered
    subscriptions, five are `webpush`.

So the ACCOUNT is told, and the push is never sent rather than sent and then suppressed. Both
halves are kept: the device-local one still works with no instance at all and is faster when the
sender and the receiver are the same phone.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
ROUTER = (ROOT / "app/routers/push.py").read_text(encoding="utf-8")
WATCHER = (ROOT / "app/services/nostr_push_service.py").read_text(encoding="utf-8")
MODELS = (ROOT / "app/models.py").read_text(encoding="utf-8")


def test_the_watcher_asks_before_it_sends():
    """Not sending beats sending and hoping the device drops it — the device may be a browser."""
    body = WATCHER[WATCHER.index("async def _dm_handler("):WATCHER.index("def _dm_sub(")] \
        if "def _dm_sub(" in WATCHER else WATCHER[WATCHER.index("async def _dm_handler("):]
    assert "_sent_by_own_device" in body, (
        "the DM push no longer checks whether one of this account's own devices published the wrap")
    assert body.index("_sent_by_own_device") < body.index("_subs_for"), (
        "the check runs after the devices are gathered; it must come first so nothing is sent")


def test_the_lookup_fails_open():
    """Every guard in this subsystem fails open, for one reason: a duplicate notification is a
    nuisance, a suppressed one is a message somebody never learns about."""
    fn = WATCHER[WATCHER.index("def _sent_by_own_device("):WATCHER.index("def _subs_for(")]
    assert "return set()" in fn and "except Exception" in fn, (
        "an unreachable database must answer 'nobody sent this' and let the push through")
    assert "created_at >=" in fn.replace(" ", "") or "created_at >=" in fn, (
        "the lookup has no freshness bound, so a stale row could silence a real message for ever")


def test_the_endpoint_proves_who_is_asking():
    """Without it, knowing an npub would be enough to silence somebody's real messages — and a
    suppressed alert is invisible to the person it belonged to."""
    fn = ROUTER[ROUTER.index('@router.post("/sent")'):ROUTER.index('@router.post("/direct/register")')]
    assert "verify_self_auth" in fn, "the suppression endpoint takes a pubkey on the caller's word"
    assert '"push-sent"' in fn, "the proof is not bound to this purpose, so a prefs proof would do"
    assert "_HEX64" in fn or "64" in fn, "wrap ids are not validated as event ids"
    assert "_SENT_MAX_IDS" in fn, "an unbounded list of ids can be posted in one call"


def test_the_record_expires():
    """A send is followed by its push within seconds. Rows that outlive that are bookkeeping, and a
    table that only grows is its own outage."""
    fn = ROUTER[ROUTER.index('@router.post("/sent")'):ROUTER.index('@router.post("/direct/register")')]
    assert "delete(" in fn, "nothing ever removes old rows"
    assert "_SENT_TTL_SECONDS" in fn
    assert "push_sent_wraps" in MODELS, "the model is gone"


def test_the_client_tells_the_account_as_well_as_the_device():
    send = APP[APP.index("notePublished"):]
    send = send[:send.index("Store.saveEvent")]
    assert "_notePublishedWraps" in send, (
        "the client tells only the device that sent the message, so every OTHER device of the same "
        "person is still pushed about it")


def test_the_proof_is_cached_so_sending_a_dm_does_not_prompt_the_signer():
    """A self-auth proof is valid for five minutes either side. Signing one per message would mean a
    signer prompt PER DM on every NIP-07 and Amber setup — a worse annoyance than the notification
    this fixes."""
    fn = APP[APP.index("async function _notePublishedWraps("):]
    fn = fn[:fn.index("async function mirrorPushPrefs(")]
    assert "_sentAuth" in fn and "_SENT_AUTH_TTL" in fn, "a signature is taken for every DM sent"
    ttl = int(re.search(r"_SENT_AUTH_TTL\s*=\s*(\d+)", APP).group(1))
    assert ttl < 300000, (
        "the cached proof outlives the server's 300s window, so every send after it silently fails")


def test_it_never_blocks_or_breaks_the_send():
    """It runs after the message is published. A notification nicety must not be able to cost
    somebody their message."""
    fn = APP[APP.index("async function _notePublishedWraps("):]
    fn = fn[:fn.index("async function mirrorPushPrefs(")]
    assert "catch(_)" in fn, "a failure here can throw into the DM send path"
    assert "_standalone()" in fn, (
        "a build with no instance still posts to an endpoint that cannot exist")
    send = APP[APP.index("const _wrapIds="):]
    send = send[:send.index("Store.saveEvent")]
    assert "void _notePublishedWraps" in send, (
        "the send path awaits the notice; the message must be saved and shown regardless")
