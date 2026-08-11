"""Nostr bot client — the listener-facing surface, delegating to app.services.nostr.

This exposes the same functions the listeners import via ``_mk.*``
(get_mentions/get_note/get_own_account/send_reply/post_image_to_fediverse/
get_thread_history/get_thread_images/download_image_from_url), but shaped for Nostr.
All crypto/relay/media lives in the shared ``app.services.nostr`` package (importable
the same way the Pleroma shim imports ``app.services``), so there is no
duplicated protocol code. Media is uploaded to the configured Blossom/NIP-96 host
and the resulting URL embedded in the note content (Nostr's media model).
"""

import os
import re
import sys
import asyncio
import logging
import time
from datetime import datetime, timezone

import requests

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from app.services.nostr import nostr_service as _svc  # noqa: E402
from config import (  # noqa: E402
    NOSTR_NSEC, NOSTR_RELAYS, NOSTR_MEDIA_SERVICE, NOSTR_MEDIA_ENDPOINT, BOT_NOSTR_PUBKEYS,
)

logger = logging.getLogger(__name__)

_SECKEY = _svc.decode_seckey(NOSTR_NSEC) if NOSTR_NSEC else None
_PUBKEY = _svc.derive_pubkey(_SECKEY) if _SECKEY else None
# Bots talk ONLY to this node's local WoT relay — never the public upstream list (the local relay
# handles all federation). The manager always injects NOSTR_RELAYS=ws://127.0.0.1:<relay_port>; if it
# were ever missing, fall back to the LOCAL relay (NOT the public DEFAULT_RELAYS) so a misconfig can
# never make a bot ping upstream relays directly.
_RELAYS = _svc.relay.normalize_relays(NOSTR_RELAYS) or ["ws://127.0.0.1:3052"]
_MEDIA_CFG = {"service": NOSTR_MEDIA_SERVICE or "blossom", "endpoint": NOSTR_MEDIA_ENDPOINT or ""}
_meta_cache: dict = {}

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
    "webp": "image/webp", "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "ogg": "audio/ogg", "wav": "audio/wav",
}
# Extensionless media links: a Blossom URL is /<64-hex sha256>, and the common Nostr media
# CDNs serve blobs. These have no file extension and (if the client omitted imeta) would
# otherwise be missed; their real type is sniffed from the bytes at download time.
_BLOSSOM_RE = re.compile(r"/[0-9a-f]{64}(?:\.\w+)?(?:[?#]|$)", re.IGNORECASE)
_MEDIA_HOST_HINTS = ("blossom", "nostr.build", "nostrcheck", "nostr.media", "void.cat", "satellite.earth")


def _is_media_url(url: str) -> bool:
    """True for extensionless URLs that are very likely media (Blossom hash path / media CDN)."""
    if _BLOSSOM_RE.search(url):
        return True
    u = url.lower()
    return any(h in u for h in _MEDIA_HOST_HINTS)


def sniff_mime(data: bytes) -> str:
    """Detect an image/video mime from magic bytes (reuses the shared sniffer)."""
    try:
        from app.services.media_service import detect_mime
        mime, _ = detect_mime(data)
        return mime
    except Exception:
        return ""


def _run(coro):
    return asyncio.run(coro)


def _short_npub(pubkey_hex: str) -> str:
    try:
        return _svc.npub_of(pubkey_hex)[:12] + "…"
    except Exception:
        return pubkey_hex[:10]


def _light_sender(pubkey_hex: str) -> dict:
    """Cheap display user from just the pubkey — NO network. Used when shaping every
    fetched note (a kind-0 lookup per mention would make a poll take minutes)."""
    return {"username": _short_npub(pubkey_hex), "host": None,
            "avatarUrl": None, "pubkey": pubkey_hex}


def resolve_user(pubkey_hex: str) -> dict:
    """Resolve a pubkey to {username, host, avatarUrl, nip05} via its kind-0 profile (cached).
    The display `username` prefers the kind-0 name/display_name, then the NIP-05 handle, then a short
    npub — so a seat with only a NIP-05 identity (and the bot's own "dealer" seat) shows e.g. @alice,
    never a raw npub. Network — call only when the richer identity is actually needed (own account,
    effect outro brand, game seat names), not for every mention in the poll."""
    if pubkey_hex in _meta_cache:
        return _meta_cache[pubkey_hex]
    meta = {}
    try:
        meta = _run(_svc.get_metadata(pubkey_hex, _RELAYS)) or {}
    except Exception:
        meta = {}
    # Our OWN account: fall back to the manager-injected profile env, so we never render our own npub
    # (e.g. the dealer/house seat in a game post) even if the relay query for our kind-0 transiently
    # misses or the profile was published without a `name`.
    if pubkey_hex == _PUBKEY:
        if not meta.get("name"):
            meta["name"] = (os.getenv("NOSTR_PROFILE_NAME", "") or "").strip()
        if not meta.get("nip05"):
            meta["nip05"] = (os.getenv("NOSTR_PROFILE_NIP05", "") or "").strip()
    nip05 = (meta.get("nip05") or "").strip()
    # NIP-05 "name@domain" → the local part as a handle ("_@domain" root identity has no useful handle).
    nip05_handle = nip05.split("@", 1)[0] if (nip05 and not nip05.startswith("_@")) else ""
    user = {
        "username": meta.get("name") or meta.get("display_name") or nip05_handle or _short_npub(pubkey_hex),
        "host": None,  # Nostr has no instance host; handles are npubs/NIP-05
        "avatarUrl": meta.get("picture"),
        "nip05": nip05,
        "pubkey": pubkey_hex,
    }
    _meta_cache[pubkey_hex] = user
    return user


def get_timeline(limit: int = 60, since: int | None = None) -> list:
    """Recent kind-1 notes from the relay's firehose (WoT) timeline — for the random-reply feature.
    One short REQ per relay; the listener gates hard before doing any per-note work."""
    flt = {"kinds": [1], "limit": limit}
    if since:
        flt["since"] = int(since)
    try:
        evs = _run(_svc.relay.query(_RELAYS, [flt])) or []
    except Exception as e:
        logger.warning(f"[nostr] get_timeline failed: {e}")
        return []
    notes = [_shape_note(ev) for ev in evs if ev.get("kind") == 1]
    return [n for n in notes if n]


_nip05_cache: dict = {}


def verify_nip05(pubkey_hex: str, nip05: str) -> bool:
    """True if `nip05` (name@domain) actually resolves to `pubkey_hex` at the domain's
    /.well-known/nostr.json (NIP-05). Cached; only called for a rare gated candidate, so the HTTP
    fetch isn't a hot path. Any error → False (treat unverifiable as not-NIP-05)."""
    nip05 = (nip05 or "").strip().lower()
    if not nip05 or "@" not in nip05:
        return False
    key = (pubkey_hex, nip05)
    if key in _nip05_cache:
        return _nip05_cache[key]
    ok = False
    try:
        local, domain = nip05.split("@", 1)
        import httpx
        with httpx.Client(timeout=8.0, follow_redirects=True) as c:
            data = c.get(f"https://{domain}/.well-known/nostr.json", params={"name": local}).json()
        ok = ((data.get("names") or {}).get(local) or "").lower() == pubkey_hex.lower()
    except Exception:
        ok = False
    if len(_nip05_cache) > 5000:
        _nip05_cache.clear()
    _nip05_cache[key] = ok
    return ok


def get_brand(note: dict) -> tuple:
    """(handle, avatar) for the effect outro end-card — resolves the sender's profile
    (network) only when an effect actually runs, not on every poll."""
    pubkey = (note.get("user") or {}).get("pubkey", "")
    user = resolve_user(pubkey) if pubkey else {}
    avatar = None
    if user.get("avatarUrl"):
        data = download_image_from_url(user["avatarUrl"], timeout=30)
        if data:
            avatar = (data, "")
    return user.get("username") or _short_npub(pubkey), avatar


def _files_from_event(ev: dict) -> list:
    """Extract media references (imeta tags + bare URLs in content) as file dicts."""
    files = []
    seen = set()
    for tag in ev.get("tags", []):
        if tag and tag[0] == "imeta":
            url = ""
            mime = ""
            for part in tag[1:]:
                if part.startswith("url "):
                    url = part[4:].strip()
                elif part.startswith("m "):
                    mime = part[2:].strip()
            if url and url not in seen:
                seen.add(url)
                files.append({"url": url, "name": url.rsplit("/", 1)[-1], "type": mime})
    for url in _URL_RE.findall(ev.get("content") or ""):
        url = url.rstrip(").,")
        if url in seen:
            continue
        last = url.rsplit("/", 1)[-1]
        ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
        if ext in _MIME_BY_EXT:
            seen.add(url)
            files.append({"url": url, "name": last, "type": _MIME_BY_EXT[ext]})
        elif _is_media_url(url):
            # Extensionless media link (no imeta) — real type sniffed at download time.
            seen.add(url)
            files.append({"url": url, "name": last, "type": ""})
    return files


def _reply_parent_id(ev: dict) -> str | None:
    """The immediate parent event id per NIP-10 (reply marker, else last e-tag)."""
    e_tags = [t for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "e"]
    for t in e_tags:
        if len(t) >= 4 and t[3] == "reply":
            return t[1]
    for t in e_tags:
        if len(t) >= 4 and t[3] == "root":
            return t[1]
    return e_tags[-1][1] if e_tags else None


def _shape_note(ev: dict) -> dict | None:
    """Turn a raw Nostr kind-1 event into the note dict the listeners consume."""
    if not ev or ev.get("kind") != 1:
        return None
    created = ev.get("created_at", 0)
    iso = datetime.fromtimestamp(created, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "id": ev.get("id"),
        "text": ev.get("content", ""),
        "content": ev.get("content", ""),
        "createdAt": iso,
        "user": _light_sender(ev.get("pubkey", "")),
        "files": _files_from_event(ev),
        "replyId": _reply_parent_id(ev),
        "visibility": "public",
        "_event": ev,  # raw event kept for reply tagging / reactions
    }


# --- surface used by the listener ------------------------------------------

def send_dm(peer_hex: str, text: str, extra_tags=None):
    """Send a NIP-17 gift-wrapped private DM (kind 1059) to `peer_hex` — the bot's private channel for
    game moves/boards. The recipient's client (the app's Messages, or any NIP-17 client) decrypts it;
    embed a Blossom image URL in `text` to deliver a board picture privately.

    A DM is fire-and-forget, so a lost write means the player never gets prompted. We can't trust
    publish()'s count: the relay client reports success when it merely SENT the EVENT, even if the
    relay (busy blasting its outbox) never replied OK and the socket is torn down before the event
    commits (relay.py: "some relays don't send OK promptly → return True"). That false-positive is
    exactly how the board DM vanished while the kind-30078 state still saved. So we don't trust the
    send — we VERIFY by reading the wrap back from the relay, and RETRY until it's actually there."""
    if not _SECKEY:
        return None
    from app.services.nostr import nip17

    def _stored(eid: str) -> bool:
        try:
            got = _run(_svc.relay.query(_RELAYS, [{"ids": [eid], "limit": 1}])) or []
            return any(e.get("id") == eid for e in got)
        except Exception:
            return False

    for attempt in range(5):
        try:
            # fresh wrap each attempt — new ephemeral key + id, so a half-sent earlier attempt
            # can't dedup-suppress the retry.
            w = nip17.wrap(_SECKEY, peer_hex, text, extra_tags=extra_tags)
            _run(_svc.relay.publish(_RELAYS, w))
            if _stored(w["id"]):
                if attempt:
                    print(f"[nostr] send_dm to {peer_hex[:8]} confirmed on retry {attempt}", flush=True)
                return w
            print(f"[nostr] send_dm to {peer_hex[:8]} not confirmed (attempt {attempt + 1}/5) — retrying", flush=True)
        except Exception as e:
            print(f"[nostr] send_dm attempt {attempt + 1}/5 failed: {e}", flush=True)
        time.sleep(1.0 + attempt)
    print(f"[nostr] send_dm to {peer_hex[:8]} GAVE UP after 5 attempts (relay not confirming)", flush=True)
    return None


# Decrypt-once cache for gift wraps. Unwrapping a kind-1059 does TWO NIP-44 ECDH point-mults, which
# are SLOW in pure Python — and read_dms re-queries the newest `limit` wraps every poll. Without this
# cache the bot re-decrypted the same ~100 wraps every 10s, pegging a core. Keyed by the (stable)
# outer 1059 id; value is the decrypted DM, or None for "tried, can't decrypt" (so we never retry it).
_wrap_cache: dict = {}
_WRAP_CACHE_MAX = 4000


def read_dms(limit: int = 100) -> list:
    """Return decrypted NIP-17 DMs sent TO the bot: [{sender, text, rumor_id, created_at, tags}].
    rumor_id (the inner kind-14 id) is stable → use it for dedup (the outer 1059 id is random)."""
    if not _PUBKEY:
        return []
    from app.services.nostr import nip17
    try:
        evs = _run(_svc.relay.query(_RELAYS, [{"kinds": [1059], "#p": [_PUBKEY], "limit": limit}])) or []
    except Exception as e:
        print(f"[nostr] read_dms query failed: {e}", flush=True)
        return []
    out = []
    for w in evs:
        wid = w.get("id")
        if wid in _wrap_cache:                  # already decrypted this wrap → no repeat ECDH
            cached = _wrap_cache[wid]
            if cached is not None:
                out.append(cached)
            continue
        try:
            sender, text, rumor = nip17.unwrap(_SECKEY, w)
            dm = {"sender": sender, "text": text, "rumor_id": rumor.get("id"),
                  "created_at": rumor.get("created_at", 0), "tags": rumor.get("tags", [])}
            _wrap_cache[wid] = dm
            out.append(dm)
        except Exception:
            _wrap_cache[wid] = None             # remember it's undecryptable; don't retry the ECDH
    if len(_wrap_cache) > _WRAP_CACHE_MAX:       # bound memory (drop oldest insertions)
        for k in list(_wrap_cache)[:len(_wrap_cache) - _WRAP_CACHE_MAX]:
            _wrap_cache.pop(k, None)
    if BOT_NOSTR_PUBKEYS:   # anti-loop: never act on a DM from another of our bots
        out = [d for d in out if (d.get("sender") or "").lower() not in BOT_NOSTR_PUBKEYS]
    return out


def ensure_profile():
    """Publish/refresh this bot's kind-0 profile from the manager-injected NOSTR_PROFILE_* env, on
    startup. By the time a bot process runs it's an operator key (always in the relay's WoT), so the
    profile is reliably accepted — unlike trying to publish it at provision time. Idempotent; retries
    until the relay confirms it stored the event."""
    if not _SECKEY:
        return
    import json as _json
    from app.services.nostr import event as _ev
    name = (os.getenv("NOSTR_PROFILE_NAME", "") or "").strip()
    nip05 = (os.getenv("NOSTR_PROFILE_NIP05", "") or "").strip()
    picture = (os.getenv("NOSTR_PROFILE_PICTURE", "") or "").strip()
    if not (name or nip05 or picture):
        return
    meta = {"bot": True}
    if name:
        meta["name"] = name
        meta["display_name"] = name
    if nip05:
        meta["nip05"] = nip05
    if picture:
        meta["picture"] = picture
    for attempt in range(8):
        try:
            ev = _ev.build_event(_SECKEY, 0, _json.dumps(meta, separators=(",", ":")), tags=[])
            _run(_svc.relay.publish(_RELAYS, ev))
            got = _run(_svc.relay.query(_RELAYS, [{"authors": [_PUBKEY], "kinds": [0], "limit": 1}])) or []
            if got:
                print(f"[nostr] profile published ({name or nip05})", flush=True)
                return
        except Exception as e:
            print(f"[nostr] ensure_profile attempt {attempt} failed: {e}", flush=True)
        time.sleep(2)
    print("[nostr] ensure_profile: relay never confirmed the profile", flush=True)


def ensure_server_list():
    """Publish this bot's kind-10063 (BUD-03) Blossom server list from the manager-injected
    BLOSSOM_SERVERS env, so clients can fail over by hash for THIS bot's media. Replaceable per-pubkey;
    best-effort (the bot is an operator key by now, so the WoT relay accepts it). No-op if unset."""
    if not _SECKEY:
        return
    servers = [u for u in (os.getenv("BLOSSOM_SERVERS", "") or "").split() if u.startswith(("http://", "https://"))]
    if not servers:
        return
    from app.services.nostr import event as _ev
    tags = [["server", u] for u in servers]
    for attempt in range(5):
        try:
            ev = _ev.build_event(_SECKEY, 10063, "", tags=tags)
            _run(_svc.relay.publish(_RELAYS, ev))
            got = _run(_svc.relay.query(_RELAYS, [{"authors": [_PUBKEY], "kinds": [10063], "limit": 1}])) or []
            if got:
                print(f"[nostr] kind-10063 server list published ({len(servers)} servers)", flush=True)
                return
        except Exception as e:
            print(f"[nostr] ensure_server_list attempt {attempt} failed: {e}", flush=True)
        time.sleep(2)
    print("[nostr] ensure_server_list: relay never confirmed the list", flush=True)


def get_own_account():
    if not _PUBKEY:
        return None
    meta = resolve_user(_PUBKEY)
    return {"username": meta["username"], "host": None, "avatarUrl": meta.get("avatarUrl"),
            "pubkey": _PUBKEY, "npub": _svc.npub_of(_PUBKEY)}


def get_mentions(limit=40):
    if not _PUBKEY:
        return []
    try:
        events = _run(_svc.fetch_mentions(_PUBKEY, _RELAYS, since=None, limit=limit))
    except Exception as e:
        logger.warning(f"[nostr] get_mentions failed: {e}")
        return []
    notes = [_shape_note(ev) for ev in events if ev.get("kind") == 1]
    notes = [n for n in notes if n]
    # Anti-loop at the SOURCE: drop mentions from ANOTHER of our bots so no listener (games included)
    # ever engages a sibling bot. Covers every get_mentions() consumer in one place.
    if BOT_NOSTR_PUBKEYS:
        notes = [n for n in notes if ((n.get("user") or {}).get("pubkey") or "").lower() not in BOT_NOSTR_PUBKEYS]
    return notes


def wait_for_relay(max_wait=90, interval=2):
    """Block until the local relay accepts a websocket connection (or `max_wait` elapses). Game bots are
    spawned at the same instant as the relay subprocess on a deploy/restart, so they begin polling
    against a relay that isn't up yet — a start/move in that window is claimed but its publish fails and
    the game silently never starts. _connect() raises while the relay is still coming up (unlike
    query(), which swallows the error), so we loop on it. Returns True once reachable, else False."""
    if not _PUBKEY or not _RELAYS:
        return True
    import time as _t
    relay = _RELAYS[0]

    async def _probe():
        async with _svc.relay._connect(relay, False):
            pass   # handshake completed → the relay is up and serving

    deadline = _t.time() + max_wait
    while _t.time() < deadline:
        try:
            _run(_probe())
            logger.info("[nostr] relay %s reachable — starting listener", relay)
            return True
        except Exception:
            _t.sleep(interval)
    logger.warning("[nostr] wait_for_relay: relay not reachable after %ss — starting anyway", max_wait)
    return False


def get_note(note_id):
    try:
        ev = _run(_svc.fetch_event(_RELAYS, note_id))
    except Exception as e:
        logger.warning(f"[nostr] get_note failed: {e}")
        return None
    return _shape_note(ev) if ev else None


def has_own_reply(note_id) -> bool:
    """Whether the bot has ALREADY posted a reply that e-tags `note_id`, per the relay.

    The relay is the authoritative 'did I answer this' record — unlike the local processed-ids
    file it survives a restart/redeploy and a mid-render kill, so this closes the double-reply
    window when the listener is restarted while a slow effect render is in flight."""
    if not _PUBKEY:
        return False
    try:
        replies = _run(_svc.fetch_thread(_RELAYS, note_id, limit=50)) or []
    except Exception as e:
        logger.warning(f"[nostr] has_own_reply check failed for {str(note_id)[:12]}: {e}")
        return False  # never block a reply on a transient query failure
    return any(ev.get("pubkey") == _PUBKEY for ev in replies)


_QUOTE_RE = re.compile(r"(?:nostr:)?((?:nevent1|note1)[023456789acdefghjklmnpqrstuvwxyz]+)", re.IGNORECASE)
# Person-mentions only (npub/nprofile), in order, to find who a note is *addressed* to.
# The `nostr:` prefix is OPTIONAL — many clients/users write a bare npub1…/nprofile1…
# (without which the bot would miss the mention and treat it as thread carryover).
_MENTION_RE = re.compile(r"(?:nostr:)?\b((?:npub1|nprofile1)[023456789acdefghjklmnpqrstuvwxyz]+)", re.IGNORECASE)


def mentionify(content: str, pubkeys, name_of) -> str:
    """Turn the `@name`s a game bot writes into REAL mentions: `nostr:npub1…`.

    A bare `@handle` in a kind-1 notifies the person (the p-tag does that) and renders as plain text
    everywhere — no name resolution, no link to the profile. NIP-27 wants the npub in the CONTENT, and
    the p-tag alongside it. Reported as "games not tagging users right": a result post read
    `@npub1mq3s439… wins 80`, which is not only unrendered but TRUNCATED, so nothing could resolve it
    even by hand.

    It was written twice (hold'em and blackjack) and missing from the other four, and both copies threw
    their own work away — see the note at each call site. One helper, six callers.

    Longest name first, so a name that is a prefix of another is not half-replaced. Names that are
    already a `nostr:` reference are left alone.
    """
    for pk in sorted([p for p in (pubkeys or []) if p], key=lambda p: -len(str(name_of(p) or ""))):
        try:
            nm = name_of(pk)
            ref = "nostr:" + _svc.npub_of(pk)
            if not nm or ref in content:
                continue
            content = content.replace(nm, ref)
        except Exception:
            pass
    return content


def _inline_mention_pubkeys(content: str) -> list:
    """Ordered list of pubkey hexes explicitly @-mentioned (nostr:npub/nprofile) in the text."""
    out = []
    for tok in _MENTION_RE.findall(content or ""):
        try:
            raw = _svc.bech32.decode_any(tok)
            if raw:
                out.append(raw.hex())
        except Exception:
            pass
    return out


def is_addressed(note, own_pubkey: str) -> bool:
    """True if the bot is actually being ADDRESSED, not just carried forward as a NIP-10
    p-tag in a thread it once participated in (which would make it reply to every reply).

    Mirrors the fediverse bot's 'first @mention' rule: the bot must be the FIRST inline
    person-mention. With no inline mentions, a top-level note counts (someone tagged the
    bot in a fresh post); a reply counts only if its parent was authored by the bot.
    Fail-open on uncertainty so a real mention is never silently dropped."""
    ev = note.get("_event") or {}
    mentions = _inline_mention_pubkeys(ev.get("content", ""))
    if mentions:
        return mentions[0] == own_pubkey
    parent_id = note.get("replyId")
    if not parent_id:
        return True  # top-level note tagging the bot, no inline mention → a direct ping
    parent = get_note(parent_id)
    if parent is None:
        return True  # can't resolve parent → don't suppress a possible real mention
    return (parent.get("user") or {}).get("pubkey") == own_pubkey


def get_quoted_note(note):
    """If a note quotes another event (NIP-18 `q` tag, or a nevent/note ref in its text),
    fetch and return that event as a shaped note — else None. Lets the bot respond to a
    quote-only mention (which otherwise strips to an empty prompt)."""
    ev = note.get("_event") or {}
    qid = None
    for t in ev.get("tags", []):
        if len(t) >= 2 and t[0] == "q" and t[1]:
            qid = t[1]
            break
    if not qid:
        m = _QUOTE_RE.search(ev.get("content") or "")
        if m:
            try:
                raw = _svc.bech32.decode_any(m.group(1))
                qid = raw.hex() if raw else None
            except Exception:
                qid = None
    return get_note(qid) if qid else None


def get_thread_history(note_id, max_depth=20):
    """Walk up the reply chain and return [{username, content, is_bot}] oldest-first."""
    history = []
    seen = set()
    cur = get_note(note_id)
    hops = 0
    while cur and hops < max_depth and cur["id"] not in seen:
        seen.add(cur["id"])
        history.append({
            "username": cur["user"]["username"],
            "content": cur["text"],
            "is_bot": cur["user"].get("pubkey") == _PUBKEY,
        })
        parent_id = cur.get("replyId")
        cur = get_note(parent_id) if parent_id else None
        hops += 1
    return list(reversed(history))


def get_thread_images(note_id, max_depth=10):
    """Download image attachments found in the note and its ancestors (for vision)."""
    images = []
    note = get_note(note_id)
    chain = [note] if note else []
    cur = note
    hops = 0
    while cur and cur.get("replyId") and hops < max_depth:
        cur = get_note(cur["replyId"])
        if cur:
            chain.append(cur)
        hops += 1
    for n in chain:
        for f in n.get("files", []):
            if (f.get("type") or "").startswith("image/"):
                data = download_image_from_url(f["url"])
                if data:
                    images.append(data)
    return images


def download_image_from_url(url, timeout=30, retries=3):
    """Fetch media bytes, retrying transient failures. A single failed GET (catbox/CDN blip,
    timeout, reset) used to leave the media-effect path with nothing to work on, which then looked
    identical to "no media attached" — so an effect would intermittently reply with the attach-a-file
    help even though the post HAD an image (worked on retry). Retrying makes that path reliable."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PosterChanBot/1.0)"}
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.content
        except requests.exceptions.RequestException as e:
            last = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    logger.warning(f"[nostr] download {url[:80]} failed after {retries} tries: {last}")
    return None


def _to_media_list(image_bytes=None, video_bytes=None, audio_bytes=None) -> list:
    """Normalize the listeners' media args to [(bytes, mime), ...]."""
    media = []
    if image_bytes:
        items = image_bytes if isinstance(image_bytes, list) else [image_bytes]
        for img in items:
            if isinstance(img, tuple) and len(img) == 2:
                media.append((img[0], img[1] or "image/png"))
            elif isinstance(img, bytes):
                media.append((img, "image/png"))
    if video_bytes:
        media.append((video_bytes, "video/mp4"))
    elif audio_bytes:
        media.append((audio_bytes, "audio/mpeg"))
    return media


def send_reply(status_obj, reply_text, own_acct=None, visibility=None,
               image_bytes=None, audio_bytes=None, video_bytes=None):
    if not _SECKEY:
        print("ERROR: NOSTR_NSEC not configured; cannot reply.")
        return
    if not reply_text and not video_bytes and not image_bytes and not audio_bytes:
        return
    parent = status_obj.get("_event") if isinstance(status_obj, dict) else None
    media = _to_media_list(image_bytes, video_bytes, audio_bytes)
    try:
        _run(_svc.post_note(_SECKEY, _RELAYS, reply_text or "", reply_to=parent,
                            media_list=media, media_cfg=_MEDIA_CFG))
        print(f"[nostr] replied to {(parent or {}).get('id','?')[:12]}")
    except Exception as e:
        print(f"[nostr] send_reply failed: {e}")


def post_image_to_fediverse(text, image_bytes=None, audio_bytes=None, video_bytes=None, hashtags=None):
    if not _SECKEY:
        print("ERROR: NOSTR_NSEC not configured; cannot post.")
        return
    media = _to_media_list(image_bytes, video_bytes, audio_bytes)
    try:
        _run(_svc.post_note(_SECKEY, _RELAYS, text or "", media_list=media, media_cfg=_MEDIA_CFG,
                            hashtags=hashtags))
        print("[nostr] posted note")
    except Exception as e:
        print(f"[nostr] post failed: {e}")
