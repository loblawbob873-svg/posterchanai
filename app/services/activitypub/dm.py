"""Direct messages across the bridge: NIP-17 ⇄ ActivityPub direct notes. On with `activitypub_dms`.

  * FEDIVERSE → NOSTR. A note addressed to one of our accounts and NOT to the public (a "direct" or
    "mentioned people only" post) becomes a NIP-17 gift-wrapped DM from the sender's PUPPET to that
    account -- delivered to this relay for a local user, and to the person's own DM relays (kind
    10050) otherwise. Never stored as a public note.
  * NOSTR → FEDIVERSE. A gift wrap addressed to a puppet reaches this relay (it accepts DMs for
    puppets). The puppet's key is DERIVED by this server, so the wrap can be opened here and sent on
    as a direct note to the real account.

THE SERVER READS THESE MESSAGES IN TRANSIT. That is what any bridge between two encryption schemes
is; the admin setting says so in as many words.

WHO MAY MESSAGE WHOM. Anybody who is not blocked: a fediverse account may message one of ours unless
its instance or account is blocked, or the recipient's public mute list (kind 10000) names the
sender's puppet -- muting somebody on Nostr is how a Nostr user says "not from them". (It used to be
"only if that person follows it or wrote first", which silently ate every first message.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from app.services import settings_store
from app.services.activitypub import actors, config, convert, nostrside, remote, state

from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

_PLATFORM = "activitypub-dm"
_LOOKBACK = 3 * 86400            # NIP-59 backdates a wrap by up to two days
_puppets = {"at": 0.0, "set": frozenset()}


# ------------------------------------------------------------------------------------ bookkeeping

async def _done(key: str) -> bool:
    """Whether this message was already handled. Raises relay_reader.Unavailable when the ledger cannot
    be read: a DM is then neither sent (it may be a duplicate) nor recorded -- it is retried."""
    from app.services import fedi_tables
    return await fedi_tables.adelivered(_PLATFORM, "", key[:255])


async def _mark(key: str, event_id: str, pubkey: str) -> None:
    """A row per handled message. `instance_url` is blank ON PURPOSE: the Pleroma bridge's sweeps
    selected rows by instance, and these are not statuses on any instance."""
    from app.services import fedi_tables
    try:
        await fedi_tables.arecord(platform=_PLATFORM, instance_url="", note_id=key[:255],
                                  nostr_event_id=event_id, nostr_pubkey=pubkey)
    except Exception as e:      # noqa: BLE001 -- the message went; only its bookkeeping did not
        logger.info("[activitypub] DM bookkeeping not saved: %s", type(e).__name__)


async def may_message(recipient: str, sender_actor: str, sender_puppet: str) -> bool:
    """Anyone may DM, as on Nostr and Mastodon -- blocked instances, accounts and keys never get this
    far (inbox.process refuses them first). The one exception is somebody the recipient has MUTED:
    their public mute list (kind 10000) naming the sender's key. A mute list that cannot be read does
    not drop the message -- a DM lost to a slow relay is worse than one from somebody unwanted.

    (It used to require the recipient to FOLLOW the sender or have written first, which silently
    dropped the first DM anybody on the fediverse sent -- stricter than Nostr itself.)"""
    from app.services.fedi_bridge_identity import query_one
    ok, mutes = await query_one(settings_store._port(), {"kinds": [10000], "authors": [recipient], "limit": 1})
    return not (ok and mutes and any(len(t) > 1 and t[0] == "p" and t[1] == sender_puppet
                                     for t in mutes.get("tags", [])))


# ------------------------------------------------------------------------------------ fediverse → nostr

async def receive_direct(note: dict, signer: str) -> str:
    """A non-public note from `signer`: DM each of our addressees (unless they muted the sender)."""
    from app.services.activitypub import inbox
    from app.services.fedi_bridge_identity import publish
    from app.services.nostr import nip17
    if not config.dms():
        return "ignored: direct messages are off"
    uri = convert.id_of(note)
    if not uri or remote.host_of(uri) != remote.host_of(signer):
        return "ignored: message is not on its sender's server"
    addressed = [convert.id_of(a) for a in convert._as_list(note.get("to")) + convert._as_list(note.get("cc"))]
    recipients = []
    for a in addressed:
        pk = actors.pubkey_of_actor_url(a)
        if pk and pk not in recipients and await actors.exposed(pk):
            recipients.append(pk)
    if not recipients:
        return "ignored: addressed to nobody here"
    text, _tags = convert.note_content(note, local_actors=actors.local_actor_map())
    if not text.strip():
        return "ignored: empty"
    puppet = await inbox._puppet(await remote.actor(signer))
    if not puppet:
        return "ignored: no puppet"
    sent = done = 0
    for pk in recipients[:10]:
        key = f"{uri}|{pk[:16]}"                 # per recipient: a partial failure resends to nobody twice
        if await _done(key):
            done += 1
            continue
        if not await may_message(pk, signer, puppet["pubkey_hex"]):
            continue
        wrap = await asyncio.to_thread(nip17.wrap, puppet["seckey"], pk, text, [["proxy", uri, "activitypub"]])
        ok, _msg = await publish(settings_store._port(), wrap)
        if not ok or not actors.is_actor(pk):
            # Not a local user (or this relay would not take it): deliver where THEY read DMs, with
            # the sender's profile so the conversation has a name and a way back.
            extra = await nostrside.puppet_identity_events(puppet["pubkey_hex"])
            await nostrside.deliver(pk, extra + [wrap], dm=True, force=True)
        await _mark(key, wrap["id"], puppet["pubkey_hex"])
        sent += 1
    if not sent:
        return "already delivered" if done else "ignored: the recipient muted the sender"
    return f"delivered to {sent}"


# ------------------------------------------------------------------------------------ nostr → fediverse

def _puppet_pubkeys() -> frozenset:
    """Every puppet's pubkey, refreshed every five minutes -- the cheap first test for a wrap. Raises
    relay_reader.Unavailable when the registry cannot be read (never "no puppets")."""
    if time.monotonic() - _puppets["at"] < 300 and _puppets["at"]:
        return _puppets["set"]
    from app.services import fedi_tables
    _puppets["set"] = frozenset(fedi_tables.puppet_pubkeys())
    _puppets["at"] = time.monotonic()
    return _puppets["set"]


async def _apuppet_pubkeys() -> frozenset:
    if time.monotonic() - _puppets["at"] < 300 and _puppets["at"]:
        return _puppets["set"]
    from app.services import fedi_tables
    _puppets["set"] = frozenset(await fedi_tables.apuppet_pubkeys())
    _puppets["at"] = time.monotonic()
    return _puppets["set"]


def remember_puppet(pubkey: str) -> None:
    """Add a just-created puppet to the cached set, so a message from it is recognised as the
    fediverse talking at once, not five minutes later."""
    if pubkey and pubkey not in _puppets["set"]:
        _puppets["set"] = _puppets["set"] | {pubkey}


async def _puppet_actor(pubkey: str) -> tuple[str, str]:
    """(actor URI the puppet was derived from, its handle)."""
    from app.services import fedi_tables
    row = await fedi_tables.apuppet_by_pubkey(pubkey)
    return (row.actor_uri, row.acct or "") if row else ("", "")


async def handle_wrap(wrap: dict) -> str:
    """A gift wrap on this relay: if it is addressed to a puppet, open it and send it on."""
    from app.services import fedi_bridge_identity as ident
    from app.services.activitypub import outbox
    from app.services.nostr import bridge_keys, nip17
    if not (config.enabled() and config.dms() and config.base_url()):
        return "off"
    from app.services import fedi_tables
    await fedi_tables.aready()      # Unavailable: the listener reconnects and the wrap is replayed
    puppets = await _apuppet_pubkeys()
    targets = [t[1] for t in wrap.get("tags", []) if len(t) > 1 and t[0] == "p" and t[1] in puppets]
    if not targets:
        return "not for the fediverse"
    results = []
    for puppet_pk in targets[:5]:
        key = f"{wrap.get('id', '')}:{puppet_pk[:16]}"
        if await _done(key):
            results.append("already sent")
            continue
        if key in _retrying:
            results.append("waiting to be retried")
            continue
        actor_uri, acct = await _puppet_actor(puppet_pk)
        if not actor_uri:
            continue
        if acct and config.account_blocked(acct):
            results.append("recipient blocked")
            continue
        sk = bridge_keys.derive_seckey(ident._secret(), actor_uri)
        try:
            sender, text, rumor = await asyncio.to_thread(nip17.unwrap, sk, wrap)
        except Exception:
            results.append("not readable with that puppet's key")
            continue
        if rumor.get("kind") != 14 or not (text or "").strip():
            continue
        if sender in puppets:
            results.append("between two puppets")          # never bridge the bridge to itself
            continue
        if not await actors.exposed(sender):
            results.append("sender is not reachable on the fediverse")
            continue
        canonical, inbox_url = await outbox._canonical(actor_uri)
        if not canonical or not inbox_url or config.host_blocked(remote.host_of(canonical)):
            results.append("recipient unreachable or blocked")
            continue
        base = config.base_url()
        me = await actors.actor_id(sender)
        if not me:
            results.append("sender is not reachable on the fediverse")
            continue
        # The id is OURS (from the wrap, whose id the relay verified), never the sender-supplied
        # rumor id, which nothing checks.
        note = {"id": f"{base}/ap/dm/{wrap.get('id', '')}/{puppet_pk[:16]}", "type": "Note", "attributedTo": me,
                "content": convert.text_to_html(text, base=base, mentions={}),
                "published": convert.iso(rumor.get("created_at") or int(time.time())),
                "to": [canonical], "cc": [],
                "tag": [{"type": "Mention", "href": canonical,
                         # The HANDLE, as every server writes it -- a URL here showed as the name.
                         "name": f"@{acct}" if acct and "@" in acct else canonical}]}
        act = convert.create(note, me)
        keys = await state.keypair(sender)          # a real signed message is behind it
        key_id, priv = await actors.signing(sender, keys)
        status = await remote.deliver(inbox_url, act, key_id=key_id, private_pem=priv)
        if 200 <= status < 300:
            await _mark(key, wrap.get("id", ""), sender)   # first: a cancel after this must not resend
            await state.open_conversation(sender, canonical)
            results.append("sent")
        elif status == 0 or status >= 500 or status in (408, 429):
            # The recipient's server is briefly down. Sent once and forgotten, the message was lost
            # with nothing on either side to say so -- a wrap is re-read only on a reconnect.
            _retry_later(key, inbox_url, act, sender, canonical, wrap.get("id", ""))
            results.append(f"HTTP {status}, will retry")
        else:
            logger.info("[activitypub] DM to %s refused: HTTP %s", remote.host_of(inbox_url), status)
            results.append(f"HTTP {status}")
    return ", ".join(results) or "nothing to do"


_RETRY_DELAYS = (60, 300, 1800, 7200)
_retrying: set = set()


def _retry_later(key: str, inbox_url: str, act: dict, sender: str, canonical: str, wrap_id: str) -> None:
    if key in _retrying or len(_retrying) >= 1000:
        return
    _retrying.add(key)

    async def run():
        try:
            for delay in _RETRY_DELAYS:
                await asyncio.sleep(delay)
                keys = await state.keypair(sender)
                key_id, priv = await actors.signing(sender, keys)
                status = await remote.deliver(inbox_url, act, key_id=key_id, private_pem=priv)
                if 200 <= status < 300:
                    await _mark(key, wrap_id, sender)
                    await state.open_conversation(sender, canonical)
                    return
                if not (status == 0 or status >= 500 or status in (408, 429)):
                    break
            logger.info("[activitypub] DM to %s given up after retries", remote.host_of(inbox_url))
        except Exception as e:
            logger.info("[activitypub] DM retry failed: %s: %s", type(e).__name__, e)
        finally:
            _retrying.discard(key)
    t = asyncio.get_running_loop().create_task(run())
    _retry_tasks.add(t)
    t.add_done_callback(_retry_tasks.discard)


_retry_tasks: set = set()


# ------------------------------------------------------------------------------------ the listener

async def _listen_once() -> None:
    """One live subscription to this relay's gift wraps (a NIP-59 wrap is backdated, so the first
    REQ looks back three days; `_done` makes a replay harmless)."""
    import os
    import websockets
    sub = "apdm" + os.urandom(4).hex()
    uri = f"ws://127.0.0.1:{settings_store._port()}/relay"
    async with websockets.connect(uri, open_timeout=10, close_timeout=2, ping_interval=30) as ws:
        await ws.send(json.dumps(["REQ", sub, {"kinds": [1059], "since": int(time.time()) - _LOOKBACK}]))
        while True:
            msg = json.loads(await ws.recv())
            if msg[0] == "EVENT" and msg[1] == sub and isinstance(msg[2], dict):
                try:
                    await asyncio.wait_for(handle_wrap(msg[2]), timeout=60)
                except Unavailable:
                    # The ledger or the puppet registry could not be read: neither sent nor marked.
                    # Dropping the subscription is the retry -- the reconnect re-reads the last three
                    # days of wraps and `_done` skips everything that did go.
                    raise
                except Exception as e:
                    logger.info("[activitypub] DM not sent: %s: %s", type(e).__name__, e)


_task = None


def start_dm_listener() -> None:
    """Keep the listener running (idempotent). It idles while ActivityPub or DMs are off."""
    global _task
    if _task is not None:
        return

    async def run():
        while True:
            if config.enabled() and config.dms() and config.base_url():
                try:
                    await _listen_once()
                except Exception as e:
                    logger.info("[activitypub] DM listener reconnecting: %s", type(e).__name__)
            await asyncio.sleep(15)

    _task = asyncio.get_running_loop().create_task(run())
