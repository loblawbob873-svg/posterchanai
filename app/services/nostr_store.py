"""Repository layer for the Nostr-backed datastore (see docs/NOSTR_DATASTORE.md).

The system of record is the built-in WoT relay's event store. This module is the app's interface
to it: it reads/writes records as **NIP-78 app-data events (kind 30078)** over the relay's local
WebSocket — the same `ws://127.0.0.1:<port>/relay` path `client.py:signup-follow` already uses, so
it works whether the relay runs in-thread or out-of-process and never touches `RelayStore` across
threads.

Each record is a *document* identified by a `d` tag in the `pcai:` namespace. kind 30078 is
parameterized-replaceable, so re-`put`ting the same `d` replaces it in place (the store keeps only
the latest per pubkey+kind+d). Content is **NIP-44-encrypted to the signing key** by default, so:
  - operator-signed docs (settings/bots/maps) are readable only with the operator key (server-held),
  - user-signed docs (chats/profile) are readable only with that user's storage key (server-held).
These are app-specific kinds, encrypted, and never fanned to upstream relays — so they never appear
in any Nostr client timeline.
"""

import os
import json
import asyncio
import logging

from app.services.nostr import bip340, nip44
from app.services.nostr.event import build_event

logger = logging.getLogger(__name__)

APP_KIND = 30078        # NIP-78 application-specific data (parameterized-replaceable)

# `d`-tag namespaces — one document per record. Keep these stable; they ARE the schema.
NS_SETTING = "pcai:setting:"     # global settings, one doc per key       (operator-signed)
NS_USER    = "pcai:user:"        # per-user account record (admin/can_ai)  (operator-signed)
NS_USERCFG = "pcai:usercfg:"     # per-user UserSetting kv (mail/nitter/etc.) (operator-signed)
NS_BOT     = "pcai:bot:"         # bot config                              (operator-signed)
NS_CONV    = "pcai:conv:"        # a user's conversation doc               (user-signed)
NS_KV      = "pcai:kv:"          # misc operational key/value              (operator-signed)
NS_MSG     = "pcai:msg:"         # a single chat message (user-signed, deletable via NIP-09)
NS_UPLOAD  = "pcai:upload:"      # encrypted upload ref → ciphertext blob in Blossom (user-signed)
NS_AIREQ   = "pcai:ai-request:"  # pending AI-access request               (user-signed)


# ---- per-user server-held storage key ----
# Identity = the user's login npub (NIP-07/Amber/nsec, key never on the server). The STORAGE key is
# a separate keypair the server generates + holds, used to encrypt-at-rest the user's chats/uploads
# to the relay and decrypt them to run the AI. Kept in UserSetting (no schema migration). Stored as
# hex; decode_seckey accepts hex or nsec.
def user_storage_seckey(db, user) -> bytes:
    from app.models import UserSetting
    from app.services import keystore
    npub = getattr(user, "nostr_npub", None)
    # 1) keyfile (authoritative — survives the app DB being in-memory/eliminated), keyed by npub
    if npub:
        sk = keystore.get_storage_seckey(npub)
        if sk:
            return sk
    # 2) legacy app.db location (UserSetting) → migrate into the keyfile on first touch
    row = db.query(UserSetting).filter(UserSetting.user_id == user.id,
                                       UserSetting.key == "storage_nsec").first()
    if row and row.value:
        try:
            sk = bytes.fromhex(row.value)
            if npub:
                keystore.set_storage_seckey(npub, sk)
            return sk
        except ValueError:
            pass
    # 3) generate a fresh key → keyfile (npub users) or legacy UserSetting (no-npub legacy users)
    sk = os.urandom(32)   # valid secp256k1 scalar w/ overwhelming probability
    if npub:
        keystore.set_storage_seckey(npub, sk)
    else:
        if row:
            row.value = sk.hex()
        else:
            db.add(UserSetting(user_id=user.id, key="storage_nsec", value=sk.hex()))
        db.commit()
    # New storage key → tell the relay to accept it as a writer (operator) without a restart.
    # Debounced: a burst of new users (e.g. a busy bot) would otherwise trigger a reload storm; at
    # most one reload per _RELOAD_DEBOUNCE. A key not yet picked up just mirrors on the next reload.
    import time as _t
    global _last_op_reload
    if _t.time() - _last_op_reload > _RELOAD_DEBOUNCE:
        _last_op_reload = _t.time()
        try:
            from app.services.nostr_relay.thread import trigger_block_reload
            trigger_block_reload()
        except Exception as e:
            logger.debug("[nostr-store] operator reload after key provision failed: %s", e)
    return sk


_last_op_reload = 0.0
_RELOAD_DEBOUNCE = 20.0


# ---- local-relay WebSocket I/O (mirrors client.py's proven signup-follow path) ----
# ONE connection, reused. Opening a WebSocket per document — TCP handshake, HTTP upgrade,
# per-connection state on the relay, teardown — was the dominant cost of a bulk write: mirroring a
# mail folder at ~550 documents a minute meant 550 connect/close cycles a minute, on both sides, to
# deliver 550 small frames. The relay is on loopback and the writes are strictly sequential (each
# waits for its own OK), so a single long-lived socket behind a lock is both simpler and faster.
#
# It is a CACHE, never a requirement: any failure drops the socket and the call retries once on a
# fresh one, so a relay restart costs one retry instead of an error.
_pub_ws = None
_pub_lock: "asyncio.Lock | None" = None


async def _publish_once(port: int, event: dict, timeout: float, reuse: bool) -> tuple[bool, str]:
    global _pub_ws
    import websockets
    uri = f"ws://127.0.0.1:{port}/relay"
    ws = _pub_ws if reuse else None
    if ws is None:
        ws = await websockets.connect(uri, open_timeout=timeout, close_timeout=2, max_size=None)
        _pub_ws = ws
    await ws.send(json.dumps(["EVENT", event]))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        # Anything that is not our OK is another client's traffic on this shared socket (the relay
        # sends nothing unsolicited to a subscription-less connection, but be explicit rather than
        # assume it): skip it and keep waiting for the id we published.
        if msg and msg[0] == "OK" and msg[1] == event["id"]:
            return bool(msg[2]), (msg[3] if len(msg) > 3 else "")


async def _ws_publish(port: int, event: dict, timeout: float = 8.0) -> tuple[bool, str]:
    global _pub_ws, _pub_lock
    if _pub_lock is None:
        _pub_lock = asyncio.Lock()
    async with _pub_lock:
        for attempt in (True, False):        # reuse the pooled socket, then a fresh one
            try:
                return await _publish_once(port, event, timeout, reuse=attempt)
            except Exception as e:
                try:
                    if _pub_ws is not None:
                        await _pub_ws.close()
                except Exception:
                    pass
                _pub_ws = None
                if not attempt:
                    return False, str(e)
    return False, "unreachable"


async def publish_event(port: int, event: dict, timeout: float = 8.0) -> tuple[bool, str]:
    """Broadcast an already-signed event to the local relay. Returns (accepted, message). Public
    entry point for callers that hold a fully-formed signed event (e.g. scheduled posts)."""
    return await _ws_publish(port, event, timeout=timeout)


async def _ws_query(port: int, filters: list, timeout: float = 6.0, *, strict: bool = False) -> list:
    """Query the local relay. Returns the matching events.

    `strict` decides what a FAILURE looks like. By default a dead socket or a timeout is swallowed and
    reported as "no events" — which is what every hydrate-style caller wants (a node whose relay isn't
    up yet must not crash on startup). But that makes "the document doesn't exist" and "I couldn't
    ask" the same answer, and a caller that then WRITES on the strength of an empty read will replace
    real data with nothing. Pass strict=True to get the exception instead, and treat it as "unknown".
    """
    import websockets
    uri = f"ws://127.0.0.1:{port}/relay"
    sub = "repo" + os.urandom(4).hex()
    out: list = []
    try:
        async with websockets.connect(uri, open_timeout=timeout, close_timeout=2) as ws:
            await ws.send(json.dumps(["REQ", sub, *filters]))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if msg[0] == "EVENT" and msg[1] == sub:
                    out.append(msg[2])
                elif msg[0] in ("EOSE", "CLOSED") and msg[1] == sub:
                    break
    except Exception as e:
        logger.debug("[nostr-store] query failed: %s", e)
        if strict:
            raise
    return out


def _decode(content: str, seckey: bytes | None, encrypt: bool):
    """Decrypt (if needed) + JSON-parse a doc's content; fall back to the raw string."""
    raw = content
    if encrypt and seckey is not None and content:
        try:
            raw = nip44.decrypt_self(seckey, content)
        except Exception:
            return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


# ---- document CRUD (the repository API the rest of the app uses) ----
async def put_doc(port: int, seckey: bytes, d_tag: str, data,
                  *, encrypt: bool = True, kind: int = APP_KIND, tags: list | None = None) -> bool:
    """Create/replace the document `d_tag`, signed (and by default NIP-44-encrypted) with `seckey`.

    PRUNE-SAFETY INVARIANT: these app-data docs are the system of record. They survive the relay's
    prune ONLY because (1) APP_KIND (30078) is not in RelayStore._PRUNABLE_KINDS, (2) the relay
    stores client-published events as origin='direct' (excluded by _preserve_clause), and (3) the
    signer pubkey is in preserve_pubkeys. NEVER add a NIP-40 `expiration` tag here — the expiration
    sweep ignores kind AND preserve, so it WOULD delete settings/accounts/chats. (See store.py.)"""
    payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))

    # OFF THE EVENT LOOP. Encrypting (NIP-44) and signing (pure-Python secp256k1) are CPU-bound and
    # were running inline in an async function, so a caller that writes many documents in a row —
    # a mailbox sync, a calendar import — held the loop for the whole run. This app serves on a
    # SINGLE uvicorn worker, so that is not "a bit slower": measured during a mail sync, /status
    # took 2s instead of ~10ms and a framed web page took 4.6s, which reads as the app hanging and
    # renders a search result as a white screen. A thread still contends for the GIL, but the
    # interpreter switches every few milliseconds, so the loop gets to answer requests in between.
    def _seal():
        body = nip44.encrypt_self(seckey, payload) if encrypt else payload
        return build_event(seckey, kind, body, tags=[["d", d_tag]] + (tags or []))

    ev = await asyncio.to_thread(_seal)
    ok, msg = await _ws_publish(port, ev)
    if not ok:
        logger.warning("[nostr-store] put %s rejected: %s", d_tag, msg)
    return ok


async def get_doc(port: int, d_tag: str, *, seckey: bytes | None = None, pubkey: str | None = None,
                  encrypt: bool = True, kind: int = APP_KIND, strict: bool = False):
    """Read document `d_tag`. Supply `seckey` (owner) to decrypt, or `pubkey` for a plaintext doc.

    Returns None when the document doesn't exist. With `strict=True` an unreachable relay RAISES
    instead of also returning None — see _ws_query. Any caller that writes back what it read should
    use it, so a failed read can't be mistaken for an empty document."""
    pk = pubkey or (bip340.pubkey_from_seckey(seckey).hex() if seckey else None)
    if not pk:
        raise ValueError("get_doc needs seckey or pubkey")
    evs = await _ws_query(port, [{"authors": [pk], "kinds": [kind], "#d": [d_tag], "limit": 1}],
                          strict=strict)
    if not evs:
        return None
    evs.sort(key=lambda e: e.get("created_at", 0), reverse=True)   # newest wins (defensive)
    return _decode(evs[0].get("content", ""), seckey, encrypt)


async def list_docs(port: int, prefix: str, *, seckey: bytes | None = None, pubkey: str | None = None,
                    encrypt: bool = True, kind: int = APP_KIND, strict: bool = False,
                    limit: int = 5000, until: int | None = None,
                    with_meta: bool = False) -> dict:
    """Return {d_tag: data} for every doc whose `d` tag starts with `prefix`, newest per d_tag.

    `strict=True` RAISES when the relay is unreachable rather than answering {} — use it in any
    caller that DECIDES something from an absence (deleting what is "no longer there", checking that
    an id is free), because {} otherwise means both "nothing matches" and "I could not ask".

    `limit` is a real constraint, not a formality: a Nostr filter cannot match a `d` PREFIX, so this
    pulls the author's documents of that kind and filters here. The keyspace is shared — chat_store
    writes one document per chat MESSAGE with the same key and kind — so a heavy chat user can fill
    the window before another namespace's documents are reached. Callers reading a small namespace
    that must be COMPLETE should raise it.
    """
    pk = pubkey or (bip340.pubkey_from_seckey(seckey).hex() if seckey else None)
    if not pk:
        raise ValueError("list_docs needs seckey or pubkey")
    # `#d~` is this relay's PREFIX tag filter, so the socket carries the documents asked for instead
    # of everything the author owns. Measured before it existed: opening one mail folder moved 5000
    # events and 91.9 MB to display 35 messages — and hit `limit`, so it truncated too. `limit` is
    # still sent as the backstop it always was.
    flt = {"authors": [pk], "kinds": [kind], "limit": limit}
    if prefix:
        flt["#d~"] = [prefix]
    # `until` is NIP-01's cursor: results are ORDER BY created_at DESC, so paging is "give me the
    # next page older than the oldest I have". It is what makes a long folder readable without
    # re-reading (and re-decrypting) everything already on screen.
    if until:
        flt["until"] = int(until)
    evs = await _ws_query(port, [flt], strict=strict)
    best: dict = {}
    for ev in evs:
        d = next((t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "d"), None)
        if not d or not d.startswith(prefix):
            continue
        if d not in best or ev.get("created_at", 0) > best[d].get("created_at", 0):
            best[d] = ev
    if with_meta:
        # (value, created_at) — the caller needs the event's own timestamp to build the next cursor;
        # the document's contents cannot supply it (a message's date is not when it was stored).
        return {d: (_decode(ev.get("content", ""), seckey, encrypt), int(ev.get("created_at", 0)))
                for d, ev in best.items()}
    return {d: _decode(ev.get("content", ""), seckey, encrypt) for d, ev in best.items()}


async def list_dtags(port: int, prefix: str, *, seckey: bytes | None = None,
                     pubkey: str | None = None, kind: int = APP_KIND,
                     limit: int = 5000) -> set:
    """Just the d-tags under `prefix` — NO content decryption.

    For existence/UID checks where the key is encoded in the d-tag itself (mailbox dedup), so a sync
    pass doesn't NIP-44-decrypt the whole folder to find out what it already has. Doing that pegged
    the event loop on every pass.

    `limit` IS THE DEDUP'S CORRECTNESS, not a performance knob, and the caller must size it. A `d`
    prefix is not something a Nostr filter can match, so this pulls the author's documents of that
    kind and filters here — and the keyspace is SHARED (chat messages, calendars, contacts and mail
    all live in kind 30078 under one key). Truncate the window and the set comes back INCOMPLETE,
    which a dedup reads as "I have never seen this message": the sync re-downloads and re-writes the
    whole mailbox, which is precisely the write-storm that took this feature down once already.
    """
    pk = pubkey or (bip340.pubkey_from_seckey(seckey).hex() if seckey else None)
    if not pk:
        raise ValueError("list_dtags needs seckey or pubkey")
    flt = {"authors": [pk], "kinds": [kind], "limit": limit}
    if prefix:
        flt["#d~"] = [prefix]      # see list_docs: ask for the namespace, not the whole key
    evs = await _ws_query(port, [flt])
    out = set()
    for ev in evs:
        d = next((t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "d"), None)
        if d and d.startswith(prefix):
            out.add(d)
    return out


async def get_docs(port: int, d_tags, *, seckey: bytes | None = None, pubkey: str | None = None,
                   encrypt: bool = True, kind: int = APP_KIND, chunk: int = 200,
                   strict: bool = False) -> dict:
    """Return {d_tag: data} for the NAMED docs — a bulk read by exact `d` tag.

    Prefer this over `list_docs` whenever the caller knows which documents it wants. `list_docs`
    fetches every doc this author has (capped at 5000) and filters client-side, so on a busy operator
    key it silently stops returning the ones asked for once the total crosses that cap: this key is
    already at 4028, of which 2972 are bookmarks, a set that only grows. The failure would be a quiet
    partial answer, which is the worst shape for a caller deciding what to write.

    Chunked because a single REQ filter carrying thousands of tag values is a large frame for no
    benefit. A missing d_tag is simply absent from the result, exactly as with get_doc.
    """
    pk = pubkey or (bip340.pubkey_from_seckey(seckey).hex() if seckey else None)
    if not pk:
        raise ValueError("get_docs needs seckey or pubkey")
    tags = [d for d in dict.fromkeys(d_tags or []) if d]
    best: dict = {}
    for i in range(0, len(tags), max(1, chunk)):
        part = tags[i:i + max(1, chunk)]
        evs = await _ws_query(port, [{"authors": [pk], "kinds": [kind], "#d": part,
                                      "limit": len(part) * 2}], strict=strict)
        for ev in evs:
            d = next((t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "d"), None)
            if not d:
                continue
            if d not in best or ev.get("created_at", 0) > best[d].get("created_at", 0):
                best[d] = ev
    return {d: _decode(ev.get("content", ""), seckey, encrypt) for d, ev in best.items()}


async def delete_doc(port: int, seckey: bytes, d_tag: str, *, kind: int = APP_KIND) -> bool:
    """Delete document `d_tag` (NIP-09 kind-5 referencing the current event + its addressable coord)."""
    pk = bip340.pubkey_from_seckey(seckey).hex()
    evs = await _ws_query(port, [{"authors": [pk], "kinds": [kind], "#d": [d_tag], "limit": 1}])
    if not evs:
        return True   # nothing to delete
    eid = evs[0].get("id")
    tags = [["e", eid], ["a", f"{kind}:{pk}:{d_tag}"]]
    ev = build_event(seckey, 5, "", tags=tags)
    ok, _ = await _ws_publish(port, ev)
    return ok
