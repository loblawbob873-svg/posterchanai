"""Write-back: a local NIP-05 user's Nostr interactions → the fediverse (interact-only).

The Nostr counterpart of the Matrix bridge's /api/matrix/timeline-action — but the trigger isn't an
HTTP call, it's the app's OWN relay. This service keeps a live subscription to the local relay and,
when a **local NIP-05 user** replies to / likes / reposts a bridged "puppet" note, performs the
matching action on the fediverse through THAT user's own linked Pleroma account:

    Nostr kind-1 reply  → Pleroma reply        (post_status in_reply_to)
    Nostr kind-7 like   → Pleroma favourite
    Nostr kind-6 repost → Pleroma reblog

Gating is strict: the author must (a) be a NIP-05 user on this instance (their pubkey is in
nostr_relay_nip05_names) and (b) map to a PosterChan user with a linked Pleroma account. Anyone else
is ignored — so a random npub wandering into the relay can never drive fediverse actions. The result
post is recorded in FediBridgeDelivered so the global-timeline mirror won't echo the user's own reply
back as a puppet note.
"""
import asyncio
import json
import logging
import os
import time
from urllib.parse import urlparse

from app.models import FediBridgeDelivered, FediBridgeMap, FediPuppet
from app.services import pleroma_service, settings_store, keystore
from app.services.nostr import nip17, bridge_keys, nostr_service

logger = logging.getLogger(__name__)

_WRITEBACK_KINDS = [1, 6, 7, 1059]
_seen_events: set = set()       # event ids already actioned this process (bounded below)
_SEEN_CAP = 5000
_LOOKBACK_SEC = 6 * 3600        # on (re)connect, replay this far back so interactions made while the
                                # listener was down (e.g. during a restart) still federate — paired
                                # with persistent idempotency so a replay can't double-post.
_DM_LOOKBACK_SEC = 3 * 86400    # NIP-59 gift-wraps carry a RANDOMIZED created_at up to 2 days in the
                                # PAST, and the relay enforces `since` on live fanout — so a 6h window
                                # would silently drop ~most DM-reply wraps. Use >2d so they're matched.
_RESUBSCRIBE_SEC = 300          # re-establish the subscription this often so a newly NIP-05'd user
                                # (just enabled Bridge Access) is added to the author filter w/o a restart.


def _port() -> int:
    try:
        return int(settings_store.get("nostr_relay_port", 3052) or 3052)
    except (ValueError, TypeError):
        return 3052


_nip05_cache: dict = {"at": 0.0, "set": frozenset()}


def _local_nip05_pubkeys() -> frozenset:
    """Pubkeys (hex) that hold a NIP-05 name on this instance — the write-back allowlist. Cached for
    30s so the per-event filter on the live relay stream is a cheap set lookup, not a parse."""
    now = time.monotonic()
    if now - _nip05_cache["at"] > 30:
        from app.services.nostr_relay.thread import _parse_nip05
        names, _ = _parse_nip05(settings_store.get("nostr_relay_nip05_names", "") or "", "")
        _nip05_cache["set"] = frozenset(names.values())
        _nip05_cache["at"] = now
    return _nip05_cache["set"]


def _user_for_pubkey(db, pk: str):
    """The PosterChan user whose linked Nostr identity is `pk` AND who has a linked Pleroma account."""
    from app.models import User
    for u in db.query(User).filter(User.pleroma_enabled == True).all():   # noqa: E712
        npub = getattr(u, "nostr_npub", None)
        if npub and nostr_service.to_pubkey_hex(npub) == pk:
            if u.pleroma_instance_url and u.pleroma_access_token:
                return u
    return None


def _referenced_event_ids(ev: dict) -> list:
    """The e-tagged events this interaction targets (NIP-10/18/25). For a reply we prefer the tag
    marked 'reply', else the last e-tag; for repost/reaction the e-tag is the target."""
    etags = [t for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "e" and t[1]]
    if not etags:
        return []
    reply = [t[1] for t in etags if len(t) >= 4 and t[3] == "reply"]
    if reply:
        return reply + [t[1] for t in etags if t[1] not in reply]
    return [t[1] for t in reversed(etags)]   # last e-tag first


def _reply_parent_id(ev: dict) -> str | None:
    """The DIRECT reply target only (NIP-10): the 'reply'-marked e-tag; else the 'root' marker when
    that's the parent; else the last positional e-tag. NEVER the thread root when a distinct reply
    target exists — otherwise a reply to a NATIVE nostr user inside a thread whose ROOT happens to be
    bridged would be mis-resolved to that root and wrongly federated (the reported bug)."""
    etags = [t for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "e" and t[1]]
    if not etags:
        return None
    marked: dict = {}
    for t in etags:
        if len(t) >= 4 and t[3] in ("root", "reply", "mention"):
            marked.setdefault(t[3], t[1])
    if "reply" in marked:
        return marked["reply"]
    if "root" in marked:          # only a root marker → this IS a direct reply to the root
        return marked["root"]
    return etags[-1][1]           # deprecated positional NIP-10: the last e-tag is the reply target


def _target_row(db, ev: dict):
    """The bridged note this event DIRECTLY interacts with (its immediate reply parent), or None.
    Reactions/reposts e-tag exactly the target; replies use the direct parent (never the root)."""
    pid = _reply_parent_id(ev)
    if not pid:
        return None
    return db.query(FediBridgeDelivered).filter(FediBridgeDelivered.nostr_event_id == pid).first()


def _strip_nostr_refs(text: str) -> str:
    import re
    return re.sub(r"\bnostr:[a-z0-9]+\b", "", text or "").strip()


import re as _re
_URL_RE = _re.compile(r"https?://[^\s]+")
_MEDIA_EXT = _re.compile(r"\.(gif|jpe?g|png|webp|apng|bmp|mp4|webm|mov|m4v)(?:[?#]|$)", _re.I)
_MIME = {"gif": "image/gif", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "webp": "image/webp", "apng": "image/apng", "bmp": "image/bmp", "mp4": "video/mp4",
         "webm": "video/webm", "mov": "video/quicktime", "m4v": "video/mp4"}


async def _extract_media(text: str):
    """Pull direct image/gif/video URLs out of a note, download them, and return (text_without_those,
    [(bytes, mime), …]) so they post as real fediverse ATTACHMENTS instead of a bare URL (the reported
    'gif rendered as a URL' issue). Bounded in count/size; non-media and page URLs are left in text."""
    import httpx
    media, consumed = [], []
    for u in _URL_RE.findall(text or ""):
        if len(media) >= 4:
            break
        m = _MEDIA_EXT.search(u)
        if not m:
            continue
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
                r = await c.get(u, headers={"User-Agent": "posterchanai-bridge/1.0"})
            if r.status_code != 200 or len(r.content) > 40_000_000:
                continue
            mime = (r.headers.get("content-type", "").split(";")[0].strip()
                    or _MIME.get(m.group(1).lower(), "application/octet-stream"))
            media.append((r.content, mime))
            consumed.append(u)
        except Exception as e:
            logger.debug("[fedi-writeback] media fetch failed for %s: %s", u, e)
    for u in consumed:
        text = text.replace(u, "")
    return text.strip(), media


async def _resolve_target_id(user, row) -> str | None:
    """The status id to act on, on the USER's instance. Same instance → the stored note_id; otherwise
    resolve the canonical AP URI on the user's instance so they act on their own copy."""
    bridge_host = urlparse(row.instance_url).netloc.split(":")[0].lower()
    user_host = urlparse(user.pleroma_instance_url).netloc.split(":")[0].lower()
    if bridge_host == user_host:
        return row.note_id
    if not row.note_uri:
        return None
    st = await pleroma_service.resolve_status(user.pleroma_instance_url, user.pleroma_access_token, row.note_uri)
    return st.get("id") if st else None


async def _handle_dm_reply(db, ev: dict) -> None:
    """A NIP-17 gift wrap (kind 1059) addressed to a puppet — a local user replying to a bridged DM.
    Unwrap it with the puppet's derived key, confirm the sender is a local NIP-05 user with a linked
    Pleroma account, then post the reply back as a direct status in that conversation."""
    eid = ev.get("id")
    if not eid or eid in _seen_events:
        return
    recipient = next((t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "p"), None)
    if not recipient:
        return
    puppet = db.query(FediPuppet).filter(FediPuppet.pubkey_hex == recipient).first()
    if not puppet:
        return
    try:
        sk = bridge_keys.derive_seckey(keystore.get_bridge_secret(), puppet.actor_uri)
        sender_hex, text, _rumor = nip17.unwrap(sk, ev)
    except Exception as e:
        logger.debug("[fedi-writeback] DM unwrap failed: %s", e)
        return
    if sender_hex not in _local_nip05_pubkeys():
        return
    user = _user_for_pubkey(db, sender_hex)
    if not user:
        return
    # Idempotency across restart/replay: a DM is NOT server-deduped, so guard on a durable marker
    # (kind="dm-out") keyed on this wrap's id before sending.
    if db.query(FediBridgeMap).filter(FediBridgeMap.nostr_event_id == eid,
                                      FediBridgeMap.kind == "dm-out").first():
        return
    _seen_events.add(eid)
    row = db.query(FediBridgeMap).filter(
        FediBridgeMap.user_id == user.id, FediBridgeMap.kind == "dm",
        FediBridgeMap.peer_pubkey == recipient).order_by(FediBridgeMap.id.desc()).first()
    text = _strip_nostr_refs(text)
    if not text:
        return
    if puppet.acct and ("@" + puppet.acct) not in text:
        text = f"@{puppet.acct} {text}"
    try:
        await pleroma_service.post_status(user.pleroma_instance_url, user.pleroma_access_token, text,
                                          visibility="direct",
                                          in_reply_to_id=(row.target_id if row else None),
                                          idempotency_key=eid)
        db.add(FediBridgeMap(user_id=user.id, nostr_event_id=eid, kind="dm-out", platform="pleroma",
                             instance_url=user.pleroma_instance_url, peer_pubkey=recipient,
                             target_id=(row.target_id if row else None), visibility="direct"))
        db.commit()
        logger.info("[fedi-writeback] DM reply by %s → fediverse DM to %s", user.username, puppet.acct)
    except Exception as e:
        db.rollback()
        logger.warning("[fedi-writeback] DM reply failed (ev %s): %s", eid, e)


async def _crosspost(db, user, ev: dict) -> None:
    """Federate a user's top-level Nostr note to their linked Pleroma account as a new public post.
    Idempotent across restart/replay via a recorded FediBridgeDelivered row keyed on the note id."""
    eid = ev.get("id")
    if db.query(FediBridgeDelivered).filter(FediBridgeDelivered.nostr_event_id == eid).first():
        return
    text = _strip_nostr_refs(ev.get("content", ""))
    text, media = await _extract_media(text)
    if not text and not media:
        return
    try:
        status = await pleroma_service.post_status(user.pleroma_instance_url, user.pleroma_access_token,
                                                   text, visibility="public", media=media or None,
                                                   idempotency_key=eid)
        if isinstance(status, dict) and status.get("id"):
            db.add(FediBridgeDelivered(
                platform="pleroma", instance_url=user.pleroma_instance_url, note_id=status["id"],
                note_uri=status.get("uri") or status.get("url"), author_acct=None,
                nostr_event_id=eid, nostr_pubkey=ev.get("pubkey")))
            db.commit()
        logger.info("[fedi-writeback] cross-posted note by %s → fediverse", user.username)
    except Exception as e:
        db.rollback()
        logger.warning("[fedi-writeback] cross-post failed (ev %s): %s", eid, e)


async def _handle(db, ev: dict) -> None:
    eid = ev.get("id")
    if not eid or eid in _seen_events:
        return
    if int(ev.get("kind", 1)) == 1059:
        await _handle_dm_reply(db, ev)
        return
    pk = ev.get("pubkey", "")
    if pk not in _local_nip05_pubkeys():
        return
    user = _user_for_pubkey(db, pk)
    if not user:
        return                       # not a local user with a linked Pleroma account → ignore
    row = _target_row(db, ev)
    if not row:
        # Cross-post only a PURE top-level broadcast — kind-1 with NO e-tags (not a reply) AND NO
        # p-tags (not a message/mention directed at a specific Nostr user). A note aimed at a Nostr
        # user (reply or mention) must stay on Nostr, never get broadcast to the fediverse.
        tags = ev.get("tags", [])
        has_e = bool(_referenced_event_ids(ev))
        has_p = any(t and len(t) >= 1 and t[0] == "p" for t in tags)
        if (int(ev.get("kind", 1)) == 1 and not has_e and not has_p
                and getattr(user, "fedi_crosspost_enabled", False)):
            await _crosspost(db, user, ev)
        return

    inst, token = user.pleroma_instance_url, user.pleroma_access_token
    kind = int(ev.get("kind", 1))
    try:
        target_id = await _resolve_target_id(user, row)
        if not target_id:
            logger.debug("[fedi-writeback] could not resolve target for ev %s", eid)
            return
        if kind == 7:
            await pleroma_service.favourite_status(inst, token, target_id)   # server-idempotent
        elif kind == 6:
            await pleroma_service.reblog_status(inst, token, target_id)      # server-idempotent
        elif kind == 1:
            # Durable idempotency across restart/replay: skip if this reply already federated.
            if db.query(FediBridgeDelivered).filter(FediBridgeDelivered.nostr_event_id == eid).first():
                return
            text = _strip_nostr_refs(ev.get("content", ""))
            text, media = await _extract_media(text)
            if not text and not media:
                return
            if row.author_acct and ("@" + row.author_acct) not in text:
                text = f"@{row.author_acct} {text}".strip()
            # Idempotency-Key = the Nostr event id → even a crash/replay double-send can't create a
            # duplicate fediverse status (server dedups on the key).
            status = await pleroma_service.post_status(inst, token, text, in_reply_to_id=target_id,
                                                       media=media or None, idempotency_key=eid)
            # Record so the global mirror won't re-publish the user's own reply as a puppet note.
            if isinstance(status, dict) and status.get("id"):
                db.add(FediBridgeDelivered(
                    platform="pleroma", instance_url=inst, note_id=status["id"],
                    note_uri=status.get("uri") or status.get("url"), author_acct=None,
                    nostr_event_id=eid, nostr_pubkey=pk))
                db.commit()
        # Mark seen only AFTER success — a transient failure stays un-seen so the next replay retries.
        _seen_events.add(eid)
        if len(_seen_events) > _SEEN_CAP:
            _seen_events.clear()
        logger.info("[fedi-writeback] kind-%d by %s → fediverse (%s)", kind, user.username, target_id)
    except Exception as e:
        db.rollback()
        logger.warning("[fedi-writeback] action failed (kind %d, ev %s): %s", kind, eid, e)


async def _listen_once() -> None:
    """One connection lifetime: subscribe to live write-back-kind events and dispatch each."""
    import websockets
    from app.database import SessionLocal
    uri = f"ws://127.0.0.1:{_port()}/relay"
    sub = "fediwb" + os.urandom(4).hex()
    since = int(time.time()) - _LOOKBACK_SEC
    # Scope the subscription SERVER-SIDE: public interactions (1/6/7) only from LOCAL NIP-05 users
    # (so the relay never streams us the whole puppet firehose), plus gift-wraps (1059) addressed to a
    # puppet — those carry an ephemeral author, so they can't be author-filtered and are handled by
    # checking the p-tag. The lookback replays missed events; idempotency (below) prevents double-posts.
    locals_ = list(_local_nip05_pubkeys())
    filters = []
    if locals_:
        filters.append({"kinds": [1, 6, 7], "authors": locals_, "since": since})
    # DMs (1059) get the wider window for NIP-59's backdated created_at (see _DM_LOOKBACK_SEC).
    filters.append({"kinds": [1059], "since": int(time.time()) - _DM_LOOKBACK_SEC})
    async with websockets.connect(uri, open_timeout=10, close_timeout=2, ping_interval=30) as ws:
        await ws.send(json.dumps(["REQ", sub, *filters]))
        logger.info("[fedi-writeback] subscribed (authors=%d, lookback=%dh) for write-back events",
                    len(locals_), _LOOKBACK_SEC // 3600)
        # Re-subscribe periodically so a NEWLY NIP-05'd user (e.g. just enabled Bridge Access) is
        # picked up into the author-scoped filter without needing a restart. Closing the socket after
        # the interval makes _run reconnect with a fresh author list.
        async def _cycle():
            await asyncio.sleep(_RESUBSCRIBE_SEC)
            try:
                await ws.close()
            except Exception:
                pass
        cycle = asyncio.ensure_future(_cycle())
        try:
            while True:
                msg = json.loads(await ws.recv())
                if msg[0] != "EVENT" or msg[1] != sub or not isinstance(msg[2], dict):
                    continue
                db = SessionLocal()
                try:
                    await _handle(db, msg[2])
                except Exception as e:
                    logger.debug("[fedi-writeback] handle error: %s", e)
                finally:
                    db.close()
        finally:
            cycle.cancel()


async def _run() -> None:
    while True:
        if str(settings_store.get("fedi_bridge_enabled", "false")).lower() not in ("1", "true", "yes", "on"):
            await asyncio.sleep(30)
            continue
        try:
            await _listen_once()
        except Exception as e:
            logger.debug("[fedi-writeback] listener disconnected (%s); reconnecting", e)
        await asyncio.sleep(5)   # backoff before reconnecting


_task = None


def start_fedi_writeback_listener() -> None:
    """Start the write-back relay listener (idempotent). Call from a running event loop."""
    global _task
    if _task is not None:
        return
    _task = asyncio.ensure_future(_run())
    logger.info("[fedi-writeback] write-back listener started")
