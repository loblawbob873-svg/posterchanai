"""Write-back: a bridge-whitelisted user's Nostr interactions → the fediverse.

The Nostr counterpart of the old bridge's timeline-action endpoint — but the trigger isn't an
HTTP call, it's the app's OWN relay. This service keeps a live subscription to the local relay and,
when a WHITELISTED user replies to / likes / reposts a bridged "puppet" note, performs the matching
action on the fediverse through THAT user's own linked Pleroma account:

    Nostr kind-1 reply  → Pleroma reply        (post_status in_reply_to)
    Nostr kind-7 like   → Pleroma favourite ('+'), else an emoji reaction (unicode or custom) — _react
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
from datetime import datetime
from urllib.parse import urlparse

from app.models import FediBridgeAction, FediBridgeDelivered, FediBridgeMap, FediPuppet
from app.services import pleroma_service, settings_store, keystore
from app.services.nostr import nip17, bridge_keys, nostr_service

logger = logging.getLogger(__name__)

_WRITEBACK_KINDS = [1, 5, 6, 7, 1059]
# KNOWN GAP (deliberately not "fixed" with an age cap): a cross-post that keeps FAILING (the instance 422s,
# say) gets re-queued by every reconnect's _LOOKBACK_SEC replay, so a backlog can build and then federate all
# at once when the instance recovers — which reads as spam. Bounding it on the note's created_at was the
# obvious fix and is WRONG: created_at is the client's SIGNING time, so an offline/queued post, a clock-skewed
# client, or any outage longer than the cap would silently never federate at all. Losing posts is worse than
# a late burst. Doing this properly needs a durable per-event attempt counter (give up after N), not a clock.
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


# "at" starts negative for the same reason as _mod_cache in the bridge service: time.monotonic() is
# seconds since BOOT, so a 0.0 seed made the freshness test "has the host been up 30s?". Starting within
# 30s of a reboot, _refresh_allowed returned WITHOUT querying, the author set stayed empty, and
# _send_pub_req(first=True) returned 0 without sending any REQ — so the whole 6h replay window was
# skipped and interactions made during the reboot never federated.
_allowed_cache: dict = {"at": -3600.0, "set": frozenset(), "uid": {}, "all_uid": {}}


def _blocked_pubkeys() -> set:
    """The relay's hard denylist (Admin → Relay "Blocked accounts" / POST /client/block), as hex.

    Parsed exactly the way the relay thread parses it — npub OR hex, comma OR newline separated — so
    "blocked on the relay" and "blocked on the bridge" cannot drift apart.
    """
    out = set()
    for tok in (settings_store.get("nostr_relay_blocked_pubkeys", "") or "").replace(",", "\n").split():
        h = nostr_service.to_pubkey_hex(tok.strip())
        if h:
            out.add(h)
    return out


def _refresh_allowed() -> None:
    """Refresh (≤30s) in ONE DB pass: the write-back WHITELIST (`set`) + its pubkey→uid map (`uid`,
    for action gating), and a BROADER pubkey→uid map of ALL linked-Pleroma users (`all_uid`, for
    mention translation — a mentioned user may have a fedi account without bridge/crosspost on). So
    every per-event lookup is O(1), no full-table scan.

    Blocking a profile on the relay is the operator's abuse lever, and it has to work HERE too, not
    only at ingest. For kinds 1/6/7/5 the denylist already settles it: those are signed by the author,
    so the relay rejects and purges them and nothing ever reaches our subscription. A NIP-17 DM does
    NOT work that way — the kind-1059 gift wrap is signed by an EPHEMERAL key, so no pubkey denylist
    can match it, the relay accepts it, and _handle_dm_reply unwraps it and gates on THIS whitelist.
    A blocked account could therefore keep pushing direct messages onto the fediverse under its own
    linked handle. Dropping blocked keys from the whitelist closes that path and makes the one admin
    action complete. Also dropped from `all_uid`, so a blocked account stops resolving in mention
    translation as well.

    Not instant, unlike the relay's own gate+purge: this runs in the WORKER, which re-hydrates
    settings from the relay every 120s, so a fresh block reaches the bridge within ~2.5 min. Blocking
    is a moderation action, not a live filter — but don't read a short delay here as a failed block.
    """
    now = time.monotonic()
    if now - _allowed_cache["at"] <= 30:
        return
    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    out, uid, all_uid = set(), {}, {}
    try:
        blocked = _blocked_pubkeys()   # inside the try: a failure here keeps the last good maps, it
        # does not escape into the per-event dispatch that calls this
        for u in db.query(User).filter(User.pleroma_enabled == True).all():   # noqa: E712
            npub = getattr(u, "nostr_npub", None)
            h = nostr_service.to_pubkey_hex(npub) if npub else None
            if not (h and u.pleroma_instance_url and u.pleroma_access_token):
                continue
            if h in blocked:
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
    # The ids a `q` tag quotes — an unmarked e-tag naming one of THOSE is the quote reference, not a
    # reply. This used to be a single has_quote boolean, so ONE q tag disabled reply-detection for
    # EVERY unmarked e-tag in the note: a positional-NIP-10 reply that also quoted something was read
    # as top-level and cross-posted to the fediverse as a standalone PUBLIC status, leaking a
    # Nostr-side conversation. Now only the quoted ids are exempt.
    quoted_ids = {t[1] for t in tags if len(t) >= 2 and t[0] == "q" and t[1]}
    for t in tags:
        if len(t) >= 2 and t[0] == "e" and t[1]:
            marker = t[3] if len(t) >= 4 else ""
            if marker in ("reply", "root"):
                return True
            if marker == "mention":
                continue                          # quote/embed reference, not a reply
            if not marker and t[1] not in quoted_ids:   # deprecated positional e-tag → reply
                return True
    return False


def _persist_delivered(inst: str, note_id: str, note_uri, eid: str, pk: str) -> None:
    """Write the dedup row on a FRESH session. The post is already live on the fediverse by the time we
    get here, so losing this row to a poisoned/aborted transaction is not cosmetic: the global mirror
    then re-imports the user's OWN status as a puppet note (the echo the row exists to prevent), and the
    reconnect replay can re-run the cross-post once the server's Idempotency-Key cache expires. The
    mirror plane already has this fallback (_deliver); the write-back didn't."""
    try:
        from app.database import SessionLocal
        s2 = SessionLocal()
        try:
            if not s2.query(FediBridgeDelivered).filter(
                    FediBridgeDelivered.instance_url == inst,
                    FediBridgeDelivered.note_id == note_id).first():
                s2.add(FediBridgeDelivered(platform="pleroma", instance_url=inst, note_id=note_id,
                                           note_uri=note_uri, author_acct=None,
                                           nostr_event_id=eid, nostr_pubkey=pk))
                s2.commit()
        finally:
            s2.close()
    except Exception as e:
        logger.warning("[fedi-writeback] dedup row re-persist failed (%s): %s", note_id, e)


async def _parent_status(inst: str, token: str, status_id: str) -> dict | None:
    """Fetch the parent status once — both the audience and the thread's participants come from it."""
    if not status_id:
        return None
    try:
        return await pleroma_service.fetch_status(inst, token, status_id)
    except Exception as e:
        logger.debug("[fedi-writeback] parent lookup failed (%s): %s", status_id, e)
        return None


def _visibility_of(st: dict | None) -> str:
    """The audience to reply with: the parent status's own visibility. Unknown → "unlisted", which is
    the safe direction (never widens the audience beyond the post being answered)."""
    v = ((st or {}).get("visibility") or "").strip().lower()
    if v in ("public", "unlisted", "private", "direct"):
        return v
    if v == "home":              # Misskey naming for unlisted
        return "unlisted"
    return "unlisted"


def _thread_handles(st: dict | None, own_acct: str = "") -> list:
    """Everyone who should stay addressed in the reply: the parent's author PLUS everyone the parent
    itself mentioned, in that order, de-duplicated.

    The fediverse carries the whole participant list forward on every reply — that's how a multi-person
    thread stays readable and how the others keep getting notified. We were prepending ONLY the direct
    parent's author, so replying into a thread silently dropped everyone else from it (reported by a
    fediverse user: "your replies drop all previous mentions in the thread"). Our own handle is
    excluded — self-mentioning reads as noise."""
    out, seen = [], set()
    own = (own_acct or "").lstrip("@").lower()

    def add(acct):
        acct = (acct or "").strip().lstrip("@")
        if not acct or acct.lower() == own or acct.lower() in seen:
            return
        seen.add(acct.lower())
        out.append(acct)

    add(((st or {}).get("account") or {}).get("acct"))
    for m in ((st or {}).get("mentions") or []):
        if isinstance(m, dict):
            add(m.get("acct"))
    return out


def _target_row(db, ev: dict):
    """The bridged note this event DIRECTLY interacts with (its immediate reply parent), or None.
    Reactions/reposts e-tag exactly the target; replies use the direct parent (never the root)."""
    # Kind-aware, because the two cases genuinely differ:
    #  - reaction (7) / repost (6): NIP-25/18 put the target in the LAST e-tag. Clients (Amethyst,
    #    Damus) copy the target's OWN root/reply tags in first, so keying off the "reply"-marked tag
    #    picked the GRANDPARENT — the like/boost federated onto the wrong status and the real reaction
    #    never did.
    #  - reply (1): the direct parent IS the reply-marked tag (never the root).
    # _referenced_event_ids also prefers the reply marker, so it can't serve the reaction case either.
    try:
        kind = int(ev.get("kind") or 0)
    except (TypeError, ValueError):
        kind = 0
    if kind in (6, 7):
        etags = [t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "e" and t[1]]
        pid = etags[-1] if etags else None
    else:
        pid = _reply_parent_id(ev)
    if not pid:
        return None
    return db.query(FediBridgeDelivered).filter(FediBridgeDelivered.nostr_event_id == pid).first()


def _strip_nostr_refs(text: str) -> str:
    import re
    return re.sub(r"\bnostr:[a-z0-9]+\b", "", text or "").strip()


_handle_cache: dict = {}        # pubkey_hex -> "@user@host" (positive; handles are stable → cached forever)
_handle_neg: dict = {}          # pubkey_hex -> (monotonic time of the miss, seconds to honour it)
_HANDLE_NEG_TTL = 300           # re-resolve a "no fedi identity" pubkey after this (user may link later)
# A lookup that ERRORED is NOT the same answer as "this person has no fediverse account", and must not
# be cached like one. Conflating them cost a real mention: an unresolved handle means the `nostr:npub…`
# is deleted from the post by _strip_nostr_refs, so ONE failed call to a flaky instance silently
# vaporised every mention of that person for the next five minutes.
_HANDLE_FAIL_TTL = 30           # ...so retry soon, but not once per event against a dead instance
_HANDLE_RE = _re.compile(r"nostr:((?:npub1|nprofile1)[0-9a-z]{20,})", _re.I)


_own_acct_cache: dict = {}


async def _own_acct(user) -> str:
    """This user's own fediverse handle (`user@host`), cached per user.

    Needed so a reply doesn't @-mention the sender in their own post: the parent's mention list
    normally includes us, and echoing it back reads as noise."""
    key = getattr(user, "id", None)
    if key in _own_acct_cache:
        return _own_acct_cache[key]
    acct = ""
    try:
        me = await pleroma_service.verify_credentials(user.pleroma_instance_url, user.pleroma_access_token)
        a = (me or {}).get("acct") or (me or {}).get("username") or ""
        if a:
            host = urlparse(user.pleroma_instance_url).netloc.split(":")[0].lower()
            acct = a if "@" in a else f"{a}@{host}"
    except Exception as e:
        logger.debug("[fedi-writeback] own acct lookup failed: %s", e)
    if acct:
        _own_acct_cache[key] = acct
    return acct


async def _fedi_handle_for_pubkey(db, pk_hex: str) -> str | None:
    """Map a Nostr pubkey to its fediverse `@user@host` handle when one exists: a bridge PUPPET
    (cheap DB lookup) or ANY local user with a linked account (resolved once via verify_credentials).
    Positives are cached forever (stable); negatives only for _HANDLE_NEG_TTL so a user who links a
    fedi account later still gets their mentions translated without a restart."""
    if pk_hex in _handle_cache:
        return _handle_cache[pk_hex]
    neg = _handle_neg.get(pk_hex)
    if neg and (time.monotonic() - neg[0]) < neg[1]:
        return None
    pup = db.query(FediPuppet).filter(FediPuppet.pubkey_hex == pk_hex).first()
    if pup and pup.acct:
        _handle_cache[pk_hex] = "@" + pup.acct
        _handle_neg.pop(pk_hex, None)
        return _handle_cache[pk_hex]
    user = _any_user_for_pubkey(db, pk_hex)
    handle, failed = None, False
    if user:
        host = urlparse(user.pleroma_instance_url).netloc.split(":")[0].lower()
        acct = (user.pleroma_acct or "").strip().lstrip("@")
        if not acct:
            # The handle is only fetched over the network when it ISN'T already on the row, and the
            # answer is written back — so this costs one call per user EVER, not one per process
            # start. It used to re-ask on every restart, which is what put the whole translation at
            # the mercy of a single request to someone else's instance.
            try:
                me = await pleroma_service.verify_credentials(user.pleroma_instance_url,
                                                              user.pleroma_access_token)
                acct = ((me or {}).get("acct") or (me or {}).get("username") or "").strip().lstrip("@")
            except Exception as e:
                failed = True       # logged, not swallowed: this was invisible in the journal before
                logger.warning("[fedi-writeback] fedi handle lookup failed for %s at %s: %s",
                               pk_hex[:8], user.pleroma_instance_url, e)
            if acct:
                try:
                    user.pleroma_acct = acct if "@" in acct else f"{acct}@{host}"
                    db.commit()
                except Exception as e:
                    db.rollback()   # the handle still stands for THIS note; we just re-ask next time
                    logger.debug("[fedi-writeback] pleroma_acct backfill failed for %s: %s", pk_hex[:8], e)
        if acct:
            handle = "@" + (acct if "@" in acct else f"{acct}@{host}")
    if handle:
        _handle_cache[pk_hex] = handle
        _handle_neg.pop(pk_hex, None)
    else:
        _handle_neg[pk_hex] = (time.monotonic(), _HANDLE_FAIL_TTL if failed else _HANDLE_NEG_TTL)
    return handle


# A bare `@name` mention: start-of-line/after-whitespace, and NOT already a full `@user@host` (the
# trailing lookahead rejects a token followed by more handle characters, so `@alice@example.org`
# — either typed, or just produced by the nostr: pass below — is left alone rather than re-mangled).
_BARE_MENTION_RE = _re.compile(r"(?:(?<=^)|(?<=\s))@([a-z0-9_.\-]{2,40})(?![\w.@\-])", _re.I)


async def _nostr_names_for(pk_hex: str) -> set:
    """The bare `@names` a Nostr profile answers to: kind-0 name, display_name and the NIP-05
    local-part — the same three fields the client matches on when it resolves a typed `@name` to a
    p-tag (static/js/client/app.js mentionTags), so both ends agree on who `@choom` is."""
    try:
        meta = await nostr_service.get_metadata(pk_hex, [f"ws://127.0.0.1:{_port()}"])
    except Exception:
        return set()
    names = set()
    for v in ((meta or {}).get("name"), (meta or {}).get("display_name")):
        v = (v or "").strip().lower()
        if v:
            names.add(v)
    local = ((meta or {}).get("nip05") or "").strip().lstrip("@").lower().partition("@")[0]
    if local and local != "_":
        names.add(local)
    return names


async def _translate_mentions(db, ev: dict) -> str:
    """Rewrite Nostr mentions that point at a fediverse identity into the matching `@user@host` so the
    cross-posted note actually mentions/notifies them on the fediverse. Two forms, because a mention
    reaches us two ways:

      - `nostr:npub…`/`nostr:nprofile…` inline refs (what every client inserts when you PICK from the
        mention autocomplete). Unresolvable refs are left for _strip_nostr_refs to remove.
      - a bare `@name` whose identity lives ONLY in the note's `p` tags. Our client deliberately
        supports typing `@name` without picking the autocomplete — it p-tags the match but leaves the
        text as-is — so such a note arrived here with nothing for the pass above to rewrite and went
        out to Pleroma as the literal string "@choom", which resolves to nobody off-instance: no
        mention tag, no link, no notification (reported for a cross-post to detroitriotcity.com).

    A bare name is only translated when exactly ONE p-tagged profile answers to it, mirroring the
    client's own "1 unique hit" rule — an ambiguous @name is better left as plain text than aimed at
    the wrong person."""
    text = (ev or {}).get("content", "") or ""
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

    # Bare `@name` → p-tag. Gated on the text actually containing one, so the common case costs no
    # lookups at all; the kind-0 fetch then only runs for p-tagged users who HAVE a fedi handle.
    wanted = {m.group(1).lower().rstrip(".") for m in _BARE_MENTION_RE.finditer(out)}
    if not wanted:
        return out
    fedi = []
    for pk in {t[1] for t in (ev.get("tags") or []) if len(t) >= 2 and t[0] == "p" and t[1]}:
        handle = await _fedi_handle_for_pubkey(db, pk)   # cached; None for anyone with no fedi account
        if handle:
            fedi.append((pk, handle))
    # The kind-0 lookups run CONCURRENTLY: a thread reply carries a p-tag per participant, and
    # get_metadata blocks for its full timeout whenever the local relay is unreachable — sequentially
    # that would stall the writeback loop for timeout x N on every note with an @ in it.
    name_sets = await asyncio.gather(*[_nostr_names_for(pk) for pk, _ in fedi])
    name_map: dict = {}
    for (_pk, handle), names in zip(fedi, name_sets):
        for n in names & wanted:
            # None = two p-tagged people answer to this name → ambiguous, leave it as plain text.
            name_map[n] = handle if name_map.get(n, handle) == handle else None

    def _sub(m):
        raw = m.group(1)
        stem = raw.rstrip(".")                      # keep sentence punctuation outside the handle
        handle = name_map.get(stem.lower())
        return (handle + raw[len(stem):]) if handle else m.group(0)

    return _BARE_MENTION_RE.sub(_sub, out)


_URL_RE = _re.compile(r"https?://[^\s]+")
_MEDIA_EXT = _re.compile(r"\.(gif|jpe?g|png|webp|apng|bmp|mp4|webm|mov|m4v)(?:[?#]|$)", _re.I)
_MIME = {"gif": "image/gif", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "webp": "image/webp", "apng": "image/apng", "bmp": "image/bmp", "mp4": "video/mp4",
         "webm": "video/webm", "mov": "video/quicktime", "m4v": "video/mp4"}


async def _extract_media(text: str):
    """Pull direct image/gif/video URLs out of a note, download them, and return (text_without_those,
    [(bytes, mime), …]) so they post as real fediverse ATTACHMENTS instead of a bare URL (the reported
    'gif rendered as a URL' issue). Bounded in count/size; non-media and page URLs are left in text."""
    import httpx, re as _re
    _blossom = _re.compile(r'/[0-9a-f]{64}(?:\.[a-z0-9]+)?$', _re.I)   # host/<sha256> blossom blob — usually EXTENSIONLESS
    media, consumed = [], []
    fetched = 0
    for u in _URL_RE.findall(text or ""):
        if len(media) >= 4:
            break
        m = _MEDIA_EXT.search(u)
        # Accept a media extension OR a blossom sha-addressed blob (e.g. media.poster.place/<sha256>), which
        # carries no extension — the old ext-only check left those as a bare LINK on the fediverse side
        # instead of a real image attachment (the reported Nostr→Pleroma image-as-link bug).
        if not m and not _blossom.search(urlparse(u).path):
            continue
        if fetched >= 8:   # bound TOTAL downloads: an extensionless non-media 64-hex URL still costs a fetch,
            break          # and the media>=4 cap counts attachments not fetches — so cap the fetches too
        fetched += 1
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
                r = await c.get(u, headers={"User-Agent": "posterchanai-bridge/1.0"})
            if r.status_code != 200 or len(r.content) > 40_000_000:
                continue
            ct = r.headers.get("content-type", "").split(";")[0].strip()
            if not m and not ct.startswith(("image/", "video/")):
                continue   # extensionless URL that isn't actually media (a page, not a blob) → leave it as a link
            mime = ct or (_MIME.get(m.group(1).lower()) if m else None) or "application/octet-stream"
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
    # NOT marked seen here: a transient send failure would then be short-circuited on every replay for
    # the life of the process, silently dropping the DM. The durable kind="dm-out" guard above already
    # covers restart/replay. Marked after the send succeeds (see below), matching _handle's design.
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
        _seen_events.add(eid)   # only NOW — a failed send must stay un-seen so the next replay retries it
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
    # Scope the dedup to THIS author. A tombstone/delivery row is now written keyed on (event id, pubkey);
    # without the pubkey filter here, a row carrying someone else's event id would suppress their cross-post —
    # i.e. user A deleting a note e-tagged to B's id could permanently stop B's post from ever federating.
    if db.query(FediBridgeDelivered).filter(
            FediBridgeDelivered.nostr_event_id == eid,
            FediBridgeDelivered.nostr_pubkey == ev.get("pubkey", "")).first():
        return
    text = _strip_nostr_refs(await _translate_mentions(db, ev))
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


# A NIP-30 custom-emoji reaction: the content is exactly ":shortcode:" (optionally :name@host: for a
# remote pack) and an ["emoji", shortcode, url] tag carries the image.
_SHORTCODE_RE = _re.compile(r"^:([A-Za-z0-9_+\-]+(?:@[A-Za-z0-9.\-]+)?):$")


async def _react(inst: str, token: str, target_id: str, ev: dict) -> tuple:
    """Perform a Nostr kind-7 as the closest fediverse action. Returns (action, emoji) — what was
    actually done, so the caller can record it and undo it if the reaction is later deleted.

    This used to be a bare favourite for EVERY reaction, which silently threw the emoji away: picking
    :blobcat: (or 🔥) in the client federated as an anonymous ❤ like. Pleroma/Akkoma carry the emoji
    natively (PUT …/reactions/<emoji>) for unicode AND for a custom emoji the reacting instance knows,
    so send the real thing and keep the favourite only for a plain NIP-25 like.

    The emoji is accepted both as `:shortcode:` and bare `shortcode`, so try the canonical form first
    and the bare one after. If the instance rejects BOTH (4xx — it has no such emoji, or predates emoji
    reactions) fall back to a favourite: the like still carries the intent. Anything else (5xx, network)
    is transient and re-raised, so the caller leaves the event un-seen and the next replay retries it
    rather than permanently downgrading the reaction to a like."""
    content = (ev.get("content") or "").strip()
    if content in ("", "+"):
        await pleroma_service.favourite_status(inst, token, target_id)   # server-idempotent
        return ("favourite", None)
    emoji = "\U0001f44e" if content == "-" else content    # NIP-25 downvote → 👎, never a "like"
    m = _SHORTCODE_RE.match(emoji)
    forms = [emoji, m.group(1)] if m else [emoji]
    last = None
    for form in forms:
        try:
            await pleroma_service.emoji_react(inst, token, target_id, form)   # server-idempotent
            return ("react", form)     # the ACCEPTED form — an undo replays this exact URL with DELETE
        except Exception as e:
            last = e
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if not 400 <= code < 500:
                raise
    # A '-' gets no fallback: favouriting a downvote would invert what the user said.
    logger.info("[fedi-writeback] instance rejected emoji reaction %r (%s)%s", emoji, last,
                "" if content == "-" else " — favouriting instead")
    if content != "-":
        await pleroma_service.favourite_status(inst, token, target_id)
        return ("favourite", None)
    return ("", None)


def _record_action(db, ev: dict, inst: str, target_id: str, action: str, emoji) -> None:
    """Remember an interaction so a NIP-09 delete of it can be undone on the fediverse. Best-effort:
    failing to record must not undo (or re-run) an action that already succeeded — the cost is only
    that this one can't be un-done later."""
    if not action:
        return
    try:
        # A reconnect replays the last _LOOKBACK_SEC and the fediverse action itself is idempotent, so the
        # same reaction lands here repeatedly across a restart — record it ONCE rather than growing a row
        # per replay.
        if db.query(FediBridgeAction).filter(
                FediBridgeAction.nostr_event_id == ev.get("id", ""),
                FediBridgeAction.target_id == target_id,
                FediBridgeAction.action == action).first():
            return
        db.add(FediBridgeAction(nostr_event_id=ev.get("id", ""), nostr_pubkey=ev.get("pubkey", ""),
                                platform="pleroma", instance_url=inst, target_id=target_id,
                                action=action, emoji=emoji))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("[fedi-writeback] could not record %s on %s: %s", action, target_id, e)


async def _undo_actions(db, user, ev: dict, target: str) -> tuple:
    """Undo every fediverse interaction recorded for the deleted event `target`.

    Returns (found, ok). `found` says the deleted event WAS an interaction of ours, so the caller
    skips the note/tombstone path — it is not a cross-post. `ok` is False only on a transient failure:
    the row stays and the event is left un-seen so the next replay retries, because an un-react that
    silently didn't apply is exactly the bug this exists to fix. No row (a plain note, or a reaction
    made before this was recorded) is (False, True) — nothing to undo."""
    inst, token = user.pleroma_instance_url, user.pleroma_access_token
    host = (inst or "").rstrip("/").lower()
    rows = db.query(FediBridgeAction).filter(
        FediBridgeAction.nostr_event_id == target,
        FediBridgeAction.nostr_pubkey == ev.get("pubkey", ""),
        FediBridgeAction.undone_at.is_(None)).all()   # only ever this actor's own, and not already undone
    ok = True
    for row in rows:
        # Same rule as _delete_federated: this user's bearer token goes ONLY to the instance that
        # issued it, never to whatever host the row happens to name.
        if (row.instance_url or "").rstrip("/").lower() != host:
            logger.warning("[fedi-writeback] not undoing %s on %s: recorded for %s, account is on %s",
                           row.action, row.target_id, row.instance_url, inst)
            continue
        try:
            if row.action == "react" and row.emoji:
                await pleroma_service.emoji_unreact(inst, token, row.target_id, row.emoji)
            elif row.action == "reblog":
                await pleroma_service.unreblog_status(inst, token, row.target_id)
            else:
                await pleroma_service.unfavourite_status(inst, token, row.target_id)
            # TOMBSTONE, don't delete: this row is also the "already performed" marker _handle checks.
            # Removing it would let the reconnect replay of the still-live kind-7 put the reaction the
            # user just deleted straight back.
            row.undone_at = datetime.utcnow()
            db.commit()
            logger.info("[fedi-writeback] undid %s%s on %s (nostr %s)", row.action,
                        f" {row.emoji}" if row.emoji else "", row.target_id, target[:10])
        except Exception as e:
            db.rollback()
            ok = False
            logger.warning("[fedi-writeback] could not undo %s on %s: %s", row.action, row.target_id, e)
    return (bool(rows), ok)


async def _delete_federated(db, user, ev: dict) -> bool:
    """NIP-09 delete (kind 5) → delete the fediverse status this note became. True if nothing failed.

    Deleting on Nostr used to leave the cross-posted copy standing on the fediverse forever: kind 5 wasn't
    even subscribed to, so nothing told Pleroma to remove it. A delete that only half-applies is worse than
    no delete — the user believes the post is gone. Each e-tag names a Nostr event we may have federated;
    FediBridgeDelivered maps it to the status id.
    """
    inst, token = user.pleroma_instance_url, user.pleroma_access_token
    if not inst or not token:
        return True
    host = (inst or "").rstrip("/").lower()
    targets = [t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "e" and t[1]]
    ok = True
    pk = ev.get("pubkey", "")
    for target in targets:
        # A kind-5 deletes whatever the user made — a cross-posted NOTE (below) or an INTERACTION.
        # Un-reacting/un-boosting used to do nothing at all here, so removing a reaction on Nostr left
        # it standing on the fediverse forever. Interactions are undone from their own recorded rows.
        found, undone = await _undo_actions(db, user, ev, target)
        if not undone:
            ok = False
        if found:
            continue          # it was a like/reaction/boost, not a note — nothing to delete or tombstone
        rows = db.query(FediBridgeDelivered).filter(
            FediBridgeDelivered.nostr_event_id == target,
            FediBridgeDelivered.nostr_pubkey == pk).all()   # only ever our OWN statuses
        if not rows:
            # Nothing federated (yet). Record a TOMBSTONE anyway: the cross-post may merely have FAILED so far
            # (a 422ing instance), and the reconnect replay would happily federate it later — publishing a note
            # the user has already deleted, with the tombstone long since consumed. _crosspost skips any note
            # that already has a row, so this closes that race permanently.
            try:
                db.add(FediBridgeDelivered(
                    platform="pleroma", instance_url=inst, note_id="", note_uri=None,
                    author_acct=None, nostr_event_id=target, nostr_pubkey=pk))
                db.commit()
                logger.info("[fedi-writeback] tombstoned %s — deleted before it federated", target[:10])
            except Exception as e:
                db.rollback()
                ok = False
                logger.warning("[fedi-writeback] could not tombstone %s: %s", target[:10], e)
            continue
        for row in rows:
            if not row.note_id:
                continue   # already a tombstone — nothing on the fediverse to delete
            if row.deleted_at:
                continue   # already deleted; the row only survives to keep the mirror off it
            # NEVER send this user's bearer token anywhere but the instance that ISSUED it. The row records
            # the instance the status was posted to; if the user has since relinked to a different instance,
            # posting their NEW token to the OLD host would hand a live credential to a third party.
            if (row.instance_url or "").rstrip("/").lower() != host:
                logger.warning("[fedi-writeback] not deleting %s: it lives on %s but the account is now on %s",
                               row.note_id, row.instance_url, inst)
                continue
            try:
                await pleroma_service.delete_status(inst, token, row.note_id)
                # KEEP the FediBridgeDelivered row. It is what stops the global fedi mirror re-importing the
                # user's own post as a puppet note — drop it and a still-live (or re-fetched) status comes
                # straight back into the Nostr timeline under a puppet key the user can't delete.
                # Mark it deleted instead, or the next reconnect replays this kind-5 and deletes the same
                # status again — which it did, on every restart, for as long as the event stayed in the
                # lookback window.
                row.deleted_at = datetime.utcnow()
                db.commit()
                logger.info("[fedi-writeback] deleted fediverse status %s (nostr %s)", row.note_id, target[:10])
            except Exception as e:
                ok = False   # transient → leave the event un-seen so the next replay retries it
                logger.warning("[fedi-writeback] could not delete fediverse status %s: %s", row.note_id, e)
    return ok


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
    if int(ev.get("kind", 1)) == 5:
        # Only mark it handled if every delete actually succeeded — otherwise a transient failure would burn
        # the retry and the status would stay up on the fediverse forever.
        if await _delete_federated(db, user, ev):
            _seen_events.add(eid)
        return
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
        # Durable idempotency for INTERACTIONS, the same guard kind-1 has below. _seen_events is
        # in-process, so every restart replayed the last _LOOKBACK_SEC and re-performed every reaction
        # and repeat in it. "Server-idempotent" was true of the instance's own STATE and false of
        # FEDERATION: each call re-emits the Like/EmojiReact/Announce to the target's instance, so the
        # author sees a fresh notification every time. A day of ordinary deploys turned one reaction
        # into ~100 and got us reported by several instances.
        # Checked against the row _record_action writes, INCLUDING tombstoned (undone) ones — a
        # reaction the user removed must not come back on the next replay.
        if kind in (6, 7):
            if db.query(FediBridgeAction).filter(
                    FediBridgeAction.nostr_event_id == eid,
                    FediBridgeAction.nostr_pubkey == pk).first():
                _seen_events.add(eid)      # done in an earlier life of this process; stop re-querying
                return
        if kind == 7:
            action, emoji = await _react(inst, token, target_id, ev)   # emoji reaction if it IS one, else favourite
            _record_action(db, ev, inst, target_id, action, emoji)     # so a later kind-5 can undo it
        elif kind == 6:
            await pleroma_service.reblog_status(inst, token, target_id)      # server-idempotent
            _record_action(db, ev, inst, target_id, "reblog", None)
        elif kind == 1:
            # Durable idempotency across restart/replay: skip if this reply already federated. Scoped by
            # author so a foreign tombstone can't suppress it.
            if db.query(FediBridgeDelivered).filter(FediBridgeDelivered.nostr_event_id == eid,
                                                    FediBridgeDelivered.nostr_pubkey == pk).first():
                return
            text = _strip_nostr_refs(await _translate_mentions(db, ev))
            q = await _quote_link(db, ev)
            if q:
                text = (text + "\n\n" + q).strip()   # a reply can also quote — keep the quoted context
            text, media = await _extract_media(text)
            if not text and not media:
                return
            # Address the WHOLE thread, not just the direct parent: fediverse replies carry every
            # participant forward, and dropping them broke thread continuity for everyone else.
            parent = await _parent_status(inst, token, target_id)
            own_acct = await _own_acct(user)
            handles = _thread_handles(parent, own_acct)
            if not handles and row.author_acct:
                handles = [row.author_acct]          # parent unavailable → at least keep the author
            prefix = " ".join(f"@{h}" for h in handles if ("@" + h) not in text)
            if prefix:
                text = f"{prefix} {text}".strip()
            # Idempotency-Key = the Nostr event id → even a crash/replay double-send can't create a
            # duplicate fediverse status (server dedups on the key).
            # INHERIT the parent's audience. post_status defaults to "public", and the mirror admits
            # unlisted/home parents (_PUBLIC_AUDIENCE), so a reply to an unlisted thread was federating
            # publicly — listed in the instance's public/federated timelines and hashtag search while
            # the post it answers deliberately isn't. Fall back to unlisted (not public) when the
            # parent's audience can't be determined, so an unknown parent fails quiet, not loud.
            vis = _visibility_of(parent)     # same fetch as the mention list above
            status = await pleroma_service.post_status(inst, token, text, in_reply_to_id=target_id,
                                                       media=media or None, idempotency_key=eid,
                                                       visibility=vis)
            # Record so the global mirror won't re-publish the user's own reply as a puppet note.
            # NOTE: if this commit fails the STATUS IS ALREADY LIVE — see the fresh-session retry below.
            if isinstance(status, dict) and status.get("id"):
                try:
                    db.add(FediBridgeDelivered(
                        platform="pleroma", instance_url=inst, note_id=status["id"],
                        note_uri=status.get("uri") or status.get("url"), author_acct=None,
                        nostr_event_id=eid, nostr_pubkey=pk))
                    db.commit()
                except Exception:
                    db.rollback()
                    _persist_delivered(inst, status["id"], status.get("uri") or status.get("url"), eid, pk)
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
        # Drive the filter from _WRITEBACK_KINDS (minus the gift-wrap kind, which has its own subscription
        # below with a much longer window). This literal used to be hardcoded [1, 6, 7], so adding kind 5 to
        # the constant did NOTHING — deletes were never even subscribed to, and the delete-propagation code
        # below could never run.
        kinds = [k for k in _WRITEBACK_KINDS if k != 1059]
        await ws.send(json.dumps(["REQ", sub_pub, {"kinds": kinds, "authors": authors, "since": since}]))
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
