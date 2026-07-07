"""Write-back: a bridge-whitelisted user's Nostr interactions → the fediverse.

The Nostr counterpart of the Matrix bridge's /api/matrix/timeline-action — but the trigger isn't an
HTTP call, it's the app's OWN relay. This service keeps a live subscription to the local relay and,
when a WHITELISTED user replies to / likes / reposts a bridged "puppet" note, performs the matching
action on the fediverse through THAT user's own linked Pleroma account:

    Nostr kind-1 reply  → Pleroma reply        (post_status in_reply_to)
    Nostr kind-7 like   → Pleroma favourite
    Nostr kind-6 repost → Pleroma reblog

Gating: the author must be on the bridge WHITELIST — a PosterChan user with a linked Pleroma account
AND bridge/cross-post enabled by the admin or themselves (NO local-NIP-05 requirement; see
_bridge_allowed_pubkeys). Anyone else is ignored, so a random npub can never drive fediverse actions.
Results are recorded in FediBridgeDelivered so the global mirror won't echo a user's own post back.
"""
import asyncio
import json
import logging
import os
import re as _re
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


_allowed_cache: dict = {"at": 0.0, "set": frozenset(), "uid": {}, "all_uid": {}}


def _refresh_allowed() -> None:
    """Refresh (≤30s) in ONE DB pass: the write-back WHITELIST (`set`) + its pubkey→uid map (`uid`,
    for action gating), and a BROADER pubkey→uid map of ALL linked-Pleroma users (`all_uid`, for
    mention translation — a mentioned user may have a fedi account without bridge/crosspost on). So
    every per-event lookup is O(1), no full-table scan."""
    now = time.monotonic()
    if now - _allowed_cache["at"] <= 30:
        return
    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    out, uid, all_uid = set(), {}, {}
    try:
        for u in db.query(User).filter(User.pleroma_enabled == True).all():   # noqa: E712
            npub = getattr(u, "nostr_npub", None)
            h = nostr_service.to_pubkey_hex(npub) if npub else None
            if not (h and u.pleroma_instance_url and u.pleroma_access_token):
                continue
            all_uid[h] = u.id
            if getattr(u, "fedi_bridge_enabled", False) or getattr(u, "fedi_crosspost_enabled", False):
                out.add(h)
                uid[h] = u.id
    except Exception as e:
        logger.debug("[fedi-writeback] allowed-pubkey refresh failed: %s", e)
        # Still advance the throttle so a transient DB error doesn't turn every incoming relay event
        # into a fresh full-table scan (keep the previous cached maps in place).
        _allowed_cache["at"] = now
        return
    finally:
        db.close()
    _allowed_cache["set"] = frozenset(out)
    _allowed_cache["uid"] = uid
    _allowed_cache["all_uid"] = all_uid
    _allowed_cache["at"] = now


def _bridge_allowed_pubkeys() -> frozenset:
    """The write-back WHITELIST: pubkeys of users opted into the bridge (linked Pleroma + bridge or
    cross-post enabled). NO local NIP-05 requirement. Cached so the per-event filter is a set lookup."""
    _refresh_allowed()
    return _allowed_cache["set"]


def _user_for_pubkey(db, pk: str):
    """A WHITELISTED PosterChan user (bridge/crosspost enabled) whose linked Nostr identity is `pk`.
    Used to gate write-back ACTIONS, so it intentionally uses the narrow whitelist map."""
    from app.models import User
    _refresh_allowed()
    uid = _allowed_cache["uid"].get(pk)
    if uid is None:
        return None
    u = db.get(User, uid)
    return u if (u and u.pleroma_instance_url and u.pleroma_access_token) else None


def _any_user_for_pubkey(db, pk: str):
    """ANY PosterChan user with a linked Pleroma account whose Nostr identity is `pk` (regardless of
    bridge/crosspost toggle) — for mention translation, where we want the fedi handle of anyone who
    has one, not only whitelisted users."""
    from app.models import User
    _refresh_allowed()
    uid = _allowed_cache["all_uid"].get(pk)
    if uid is None:
        return None
    u = db.get(User, uid)
    return u if (u and u.pleroma_instance_url and u.pleroma_access_token) else None


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
    # Deprecated positional NIP-10: the reply target is the last UNMARKED e-tag. A 'mention'-marked
    # e-tag is a QUOTE/embed reference, NOT a reply parent — so skip it. Otherwise quoting a bridged
    # note would resolve to that note as a reply target and mis-federate the quote as a fedi reply.
    unmarked = [t[1] for t in etags if not (len(t) >= 4 and t[3])]
    return unmarked[-1] if unmarked else None


def _is_reply(ev: dict) -> bool:
    """True if this note is a NIP-10 REPLY — so it must NOT cross-post as a standalone fediverse post
    when its parent isn't a bridged note (that leaks the Nostr-side conversation to the fediverse). A
    reply is an e-tag marked 'reply'/'root', OR — for clients using deprecated POSITIONAL NIP-10 — any
    UNMARKED e-tag. Quote-posts are NOT replies: a 'mention'-marked e-tag and a NIP-18 `q` tag are
    quote/embed references, so a note that only quotes still cross-posts. The earlier marker-only check
    missed unmarked positional replies (e.g. `["e", <id>]`), which is how a reply to a native Nostr user
    slipped through and federated out — this covers both markings."""
    tags = ev.get("tags", [])
    has_quote = any(len(t) >= 2 and t[0] == "q" and t[1] for t in tags)
    for t in tags:
        if len(t) >= 2 and t[0] == "e" and t[1]:
            marker = t[3] if len(t) >= 4 else ""
            if marker in ("reply", "root"):
                return True
            if marker == "mention":
                continue                          # quote/embed reference, not a reply
            if not marker and not has_quote:      # deprecated positional e-tag, not a quote-post → reply
                return True
    return False


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


_handle_cache: dict = {}        # pubkey_hex -> "@user@host" (positive; handles are stable → cached forever)
_handle_neg: dict = {}          # pubkey_hex -> monotonic time of last "no identity" miss (TTL-rechecked)
_HANDLE_NEG_TTL = 300           # re-resolve a "no fedi identity" pubkey after this (user may link later)
_HANDLE_RE = _re.compile(r"nostr:((?:npub1|nprofile1)[0-9a-z]{20,})", _re.I)


async def _fedi_handle_for_pubkey(db, pk_hex: str) -> str | None:
    """Map a Nostr pubkey to its fediverse `@user@host` handle when one exists: a bridge PUPPET
    (cheap DB lookup) or ANY local user with a linked account (resolved once via verify_credentials).
    Positives are cached forever (stable); negatives only for _HANDLE_NEG_TTL so a user who links a
    fedi account later still gets their mentions translated without a restart."""
    if pk_hex in _handle_cache:
        return _handle_cache[pk_hex]
    if pk_hex in _handle_neg and (time.monotonic() - _handle_neg[pk_hex]) < _HANDLE_NEG_TTL:
        return None
    pup = db.query(FediPuppet).filter(FediPuppet.pubkey_hex == pk_hex).first()
    if pup and pup.acct:
        _handle_cache[pk_hex] = "@" + pup.acct
        _handle_neg.pop(pk_hex, None)
        return _handle_cache[pk_hex]
    user = _any_user_for_pubkey(db, pk_hex)
    handle = None
    if user:
        try:
            me = await pleroma_service.verify_credentials(user.pleroma_instance_url, user.pleroma_access_token)
            acct = (me or {}).get("acct") or (me or {}).get("username")
        except Exception:
            acct = None
        if acct:
            host = urlparse(user.pleroma_instance_url).netloc.split(":")[0].lower()
            handle = "@" + (acct if "@" in acct else f"{acct}@{host}")
    if handle:
        _handle_cache[pk_hex] = handle
        _handle_neg.pop(pk_hex, None)
    else:
        _handle_neg[pk_hex] = time.monotonic()
    return handle


async def _translate_mentions(db, text: str) -> str:
    """Rewrite `nostr:npub…`/`nostr:nprofile…` references that point at a fediverse identity into the
    matching `@user@host` so the cross-posted note actually mentions/notifies them on the fediverse.
    Unresolvable refs are left for _strip_nostr_refs to remove."""
    if not text:
        return text
    out = text
    for m in set(_HANDLE_RE.findall(text)):
        try:
            from app.services.nostr import bech32
            raw = bech32.decode_any(m)
            pk = raw.hex() if raw else None
        except Exception:
            pk = None
        if not pk:
            continue
        handle = await _fedi_handle_for_pubkey(db, pk)
        if handle:
            out = out.replace("nostr:" + m, handle)
    return out


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
    if sender_hex not in _bridge_allowed_pubkeys():
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


async def _quote_link(db, ev: dict) -> str | None:
    """A NIP-18 quote-post's `q` reference is a bare `nostr:nevent…` that _strip_nostr_refs removes — so
    the fediverse would see only the comment, out of context ("talking nonsense"). Build a fedi-side
    quote block: a short BLOCKQUOTE of the quoted note + a link fedi users can open (the ORIGINAL
    fediverse status when the quoted note is bridged, else a njump.me Nostr link). Degrades to just the
    link (or nothing) if the quoted note can't be fetched."""
    qt = next((t for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "q" and t[1]), None)
    if not qt:
        return None
    q_eid = qt[1]
    # Link: the original fediverse status if the quoted note is bridged, else a njump.me web link.
    row = db.query(FediBridgeDelivered).filter(FediBridgeDelivered.nostr_event_id == q_eid).first()
    link = row.note_uri if (row and row.note_uri) else None
    if not link:
        try:
            from app.services.nostr import bech32
            note = bech32.encode("note", bytes.fromhex(q_eid))
            link = f"https://njump.me/{note}" if note else None
        except Exception:
            link = None
    # Short blockquote of the quoted note (best-effort; the local relay lookup is fast).
    snippet, author = "", ""
    try:
        relays = [f"ws://127.0.0.1:{_port()}"]
        qev = await nostr_service.fetch_event(relays, q_eid)
        if qev:
            raw = _strip_nostr_refs(qev.get("content", "") or "")
            raw = _re.sub(r"https?://\S+", "", raw)          # drop URLs/media from the snippet
            raw = " ".join(raw.split())
            if len(raw) > 220:
                raw = raw[:219].rstrip() + "…"
            snippet = raw
            author = (await _fedi_handle_for_pubkey(db, qev.get("pubkey", "")) or "").strip()
            if not author:
                try:
                    meta = await nostr_service.get_metadata(qev.get("pubkey", ""), relays)
                    author = (meta.get("display_name") or meta.get("name") or "").strip()
                except Exception:
                    author = ""
    except Exception:
        pass
    lines = []
    if snippet:
        lines.append("💬 quoting" + (f" {author}" if author else "") + ":")
        lines.append(f"“{snippet}”")
    if link:
        lines.append(link)
    return "\n".join(lines) if lines else None


async def _crosspost(db, user, ev: dict) -> None:
    """Federate a user's top-level Nostr note to their linked Pleroma account as a new public post.
    Idempotent across restart/replay via a recorded FediBridgeDelivered row keyed on the note id."""
    eid = ev.get("id")
    if db.query(FediBridgeDelivered).filter(FediBridgeDelivered.nostr_event_id == eid).first():
        return
    text = _strip_nostr_refs(await _translate_mentions(db, ev.get("content", "")))
    q = await _quote_link(db, ev)
    if q:
        text = (text + "\n\n" + q).strip()   # keep the quoted post's context (else it reads as nonsense)
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
    if pk not in _bridge_allowed_pubkeys():
        return
    user = _user_for_pubkey(db, pk)
    if not user:
        return                       # not a local user with a linked Pleroma account → ignore
    row = _target_row(db, ev)
    if not row:
        # No bridged parent to thread under. Cross-post ONLY a genuine TOP-LEVEL note — never a NIP-10
        # REPLY aimed at a native Nostr user, which would leak the Nostr-side conversation to the
        # fediverse as an out-of-context standalone post (the reported bug). Quote-posts (q-tag /
        # 'mention'-marked e-tag) and notes that only @mention a fedi user DO still cross-post (mentions
        # are translated to @handles). Replies whose parent IS bridged/cross-posted are threaded below.
        if (int(ev.get("kind", 1)) == 1 and getattr(user, "fedi_crosspost_enabled", False)
                and not _is_reply(ev)):
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
            text = _strip_nostr_refs(await _translate_mentions(db, ev.get("content", "")))
            q = await _quote_link(db, ev)
            if q:
                text = (text + "\n\n" + q).strip()   # a reply can also quote — keep the quoted context
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
    """One connection lifetime: subscribe to live write-back-kind events and dispatch each.

    Two SEPARATE subscriptions on the one socket so the periodic author refresh is cheap:
      - sub_dm (1059 gift-wraps): established ONCE with the wide backdated lookback. Gift-wraps carry
        an ephemeral author (can't be author-filtered), so this is a firehose — but we keep it alive
        and NEVER re-REQ it, so we don't replay days of DMs every few minutes (the old CPU spike).
      - sub_pub (1/6/7): author-scoped to whitelisted users. Re-REQ'd every _RESUBSCRIBE_SEC (CLOSE +
        REQ, NOT a socket teardown) so a newly-whitelisted user is picked up — replaying only a small
        recent window, not the full lookback. Idempotency below prevents any double-post."""
    import websockets
    from app.database import SessionLocal
    uri = f"ws://127.0.0.1:{_port()}/relay"
    sub_pub = "fediwb" + os.urandom(4).hex()
    sub_dm = "fediwd" + os.urandom(4).hex()

    async def _send_pub_req(ws, first: bool):
        authors = list(_bridge_allowed_pubkeys())
        if not authors:
            return 0
        since = int(time.time()) - (_LOOKBACK_SEC if first else (_RESUBSCRIBE_SEC + 60))
        await ws.send(json.dumps(["REQ", sub_pub, {"kinds": [1, 6, 7], "authors": authors, "since": since}]))
        return len(authors)

    async with websockets.connect(uri, open_timeout=10, close_timeout=2, ping_interval=30) as ws:
        # DM firehose: one-time REQ with the wide window for NIP-59's backdated created_at.
        await ws.send(json.dumps(["REQ", sub_dm, {"kinds": [1059],
                                                  "since": int(time.time()) - _DM_LOOKBACK_SEC}]))
        n = await _send_pub_req(ws, first=True)
        logger.info("[fedi-writeback] subscribed (authors=%d, lookback=%dh) for write-back events",
                    n, _LOOKBACK_SEC // 3600)

        async def _cycle():
            # Refresh ONLY the author-scoped public sub; leave the DM sub untouched (no replay). Also
            # re-check the kill-switch here — the socket now stays alive across refreshes, so this is
            # the only place that can notice fedi_bridge_enabled flipping off and tear down (which
            # makes recv() raise → _listen_once returns → _run idles instead of federating).
            while True:
                await asyncio.sleep(_RESUBSCRIBE_SEC)
                if str(settings_store.get("fedi_bridge_enabled", "false")).lower() not in ("1", "true", "yes", "on"):
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                try:
                    await ws.send(json.dumps(["CLOSE", sub_pub]))
                    await _send_pub_req(ws, first=False)
                except Exception:
                    break

        cycle = asyncio.ensure_future(_cycle())
        try:
            while True:
                msg = json.loads(await ws.recv())
                if msg[0] != "EVENT" or msg[1] not in (sub_pub, sub_dm) or not isinstance(msg[2], dict):
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
