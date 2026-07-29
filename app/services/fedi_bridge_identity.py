"""Identity provisioning for the Nostr ↔ Fediverse bridge.

Turns a fediverse account into a Nostr "puppet": a deterministic keypair (see nostr.bridge_keys),
a NIP-05 name on this instance, and a mirrored kind-0 profile. The relay validates puppet events by
re-deriving the key from the actor URI carried in a `fedibridge` tag, so nothing here has to register
keys with the relay — the app just signs and publishes, and the relay serves the puppet's NIP-05 the
moment it stores the kind-0 (server._register_bridge_nip05).

Public surface:
  - actor_uri_of(account)           canonical AP actor URI (the derivation key)
  - nip05_name_for(acct)            stable local-part, e.g. alice@mastodon.social → alice_mastodon.social
  - puppet_for(account)             {seckey, pubkey_hex, npub, actor_uri, nip05_name, acct, host}
  - ensure_puppet(db, port, account)  provision/refresh registry row + kind-0; returns the puppet dict
  - build_event(p, kind, content, tags, object_uri, broadcast)   sign a puppet event (adds bridge tags)
  - publish(port, ev)               publish to the local relay; (ok, msg)
"""

import re
import json
import asyncio
import hashlib
import logging
from collections import OrderedDict
from datetime import datetime

from app.services import keystore, settings_store
from app.services.nostr import bridge_keys, nostr_service
from app.services.nostr.event import build_event as _build_event
# HTML→text + custom-emoji parsing shared with the timeline/note mirror (no import cycle: neither
# fedi_timeline_service nor this module imports the other's owner).
from app.services.fedi_normalize import _strip_html, _emoji_url_map, emoji_tags_for

logger = logging.getLogger(__name__)

# --- persistent local-relay publisher ---------------------------------------
# The global-timeline mirror publishes a lot of events; opening a fresh WebSocket (TCP + WS upgrade)
# per event is the dominant CPU/latency cost. Keep ONE warm connection to ws://127.0.0.1:<port>/relay
# and serialize sends through it (we await the OK, so one in-flight at a time). Reconnect on error.
_ws = None
_ws_port = None
_ws_lock = asyncio.Lock()


async def _relay_ws(port: int):
    global _ws, _ws_port
    if _ws is not None and _ws_port == port:
        if getattr(_ws, "open", True):
            return _ws
    import websockets
    if _ws is not None:
        try:
            await _ws.close()
        except Exception:
            pass
    _ws = await websockets.connect(f"ws://127.0.0.1:{port}/relay", open_timeout=10,
                                   close_timeout=2, ping_interval=30, max_queue=64)
    _ws_port = port
    return _ws


def _secret() -> bytes:
    return keystore.get_bridge_secret()


def nip05_domain() -> str:
    """The domain puppet NIP-05 identifiers are served under (must match where this node's
    /.well-known/nostr.json is reachable). Reuses the relay's NIP-05 domain setting."""
    return (settings_store.get("nostr_relay_nip05_domain", "") or "").strip().lstrip("@").lower()


def _sanitize(s: str) -> str:
    """NIP-05 local-part charset is a-z0-9-_. — collapse everything else out."""
    return re.sub(r"[^a-z0-9_.\-]", "", (s or "").strip().lower()).strip("._-")


def actor_uri_of(account: dict) -> str:
    """The canonical ActivityPub actor URI for a Mastodon/Pleroma account object. `url` is the
    profile URL (stable, canonical); `uri` is the AP id on some servers. Prefer whichever is set."""
    return (account.get("uri") or account.get("url") or "").strip()


def acct_of(account: dict, instance_host: str = "") -> str:
    """Fully-qualified handle user@host. Mastodon/Pleroma give bare `acct` for LOCAL users (no host),
    so qualify it with the instance we read it from."""
    acct = (account.get("acct") or account.get("username") or "").strip()
    if acct and "@" not in acct and instance_host:
        acct = f"{acct}@{instance_host}"
    return acct.lstrip("@")


def nip05_name_for(acct: str) -> str:
    """Stable local-part for a handle: alice@mastodon.social → alice_mastodon.social.

    NOT 1:1 on the sanitized form alone — that was the old assumption and it was wrong. _sanitize drops
    disallowed characters AND strips leading/trailing "._-", so `alice`, `_alice_` and `_alice` all
    collapse to `alice`; the [:64] truncation collides long handles too. Live data had three distinct
    accounts sharing one name. Since the relay's NIP-05 map is last-write-wins, that let anyone who could
    register `_victim_` on the same instance take over the victim's verified name.

    So: when sanitising is lossy (or truncating), append a short digest of the FULL original acct. Handles
    that sanitise cleanly keep the pretty name they already have, so existing puppets are unaffected."""
    raw = (acct or "").strip().lower()
    local, _, host = raw.partition("@")
    base = _sanitize(local) or "user"
    h = _sanitize(host)
    name = (f"{base}_{h}" if h else base)[:64].strip("._-")
    # Lossy if the round-trip doesn't reproduce the original handle exactly.
    expected = f"{local}_{host}" if host else local
    if name != expected:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
        name = f"{name[:56].strip('._-')}_{digest}"
    return name


def puppet_for(account: dict, instance_host: str = "") -> dict:
    """Resolve the full puppet identity for a fediverse account (no I/O, no DB)."""
    actor_uri = actor_uri_of(account)
    acct = acct_of(account, instance_host)
    sk = bridge_keys.derive_seckey(_secret(), actor_uri)
    pubkey_hex = nostr_service.derive_pubkey(sk)
    host = acct.partition("@")[2] or instance_host
    return {
        "seckey": sk,
        "pubkey_hex": pubkey_hex,
        "npub": nostr_service.npub_of(pubkey_hex),
        "actor_uri": actor_uri,
        "acct": acct,
        "host": host,
        "nip05_name": nip05_name_for(acct),
        # display_name is PLAIN TEXT on Mastodon/Pleroma (never HTML) — do NOT tag-strip it, or
        # angle-bracket kaomoji like <(^o^)> get eaten. It keeps its :shortcode: emoji (rendered via the
        # NIP-30 tags below). The BIO is fediverse HTML (<br>, <a>, entities) → flatten to text or the
        # client shows raw markup. Custom-emoji shortcode→url map drives the profile's NIP-30 emoji tags.
        "display_name": (account.get("display_name") or account.get("name") or "").strip(),
        "avatar_url": (account.get("avatar") or account.get("avatar_static")
                       or account.get("avatarUrl") or "").strip(),
        "about": _strip_html(account.get("note") or account.get("description") or ""),
        "emojis": _emoji_url_map(account.get("emojis")),
    }


def puppet_from_actor(actor_uri: str, acct: str = "") -> dict:
    """Re-derive a puppet's signing identity from just its canonical actor URI — used to sign a
    NIP-09 deletion for a note we mirrored earlier (we only stored the pubkey, not the secret)."""
    sk = bridge_keys.derive_seckey(_secret(), actor_uri)
    pubkey_hex = nostr_service.derive_pubkey(sk)
    return {"seckey": sk, "pubkey_hex": pubkey_hex, "npub": nostr_service.npub_of(pubkey_hex),
            "actor_uri": actor_uri, "acct": acct, "host": "", "nip05_name": "",
            "display_name": "", "avatar_url": "", "about": ""}


async def delete_note(port: int, actor_uri: str, nostr_event_id: str, broadcast: bool = False) -> bool:
    """Publish a NIP-09 kind-5 deletion (signed by the puppet) for a mirrored note that was removed
    on the fediverse. Federates upstream iff broadcast is on (see build_event's nofederate handling)."""
    p = puppet_from_actor(actor_uri)
    ev = build_event(p, 5, "deleted on source", tags=[["e", nostr_event_id]], broadcast=broadcast)
    ok, _ = await publish(port, ev)
    return ok


def _profile_content(p: dict) -> dict:
    domain = nip05_domain()
    out = {
        "name": p["display_name"] or p["nip05_name"],
        "display_name": p["display_name"] or p["acct"].partition("@")[0],
        "about": ((p["about"] + "\n\n") if p["about"] else "") + f"🔗 bridged from {p['acct']} (fediverse)",
        "fediverse": p["acct"],
        "bridged": True,
    }
    if p["avatar_url"]:
        out["picture"] = p["avatar_url"]
    if domain:
        out["nip05"] = f"{p['nip05_name']}@{domain}"
    return out


def _profile_emoji_tags(p: dict) -> list:
    """NIP-30 emoji tags for the custom-emoji :shortcodes: in the puppet's name/bio, so clients render
    the emoji images instead of raw `:shortcode:` text (fediverse display names are full of them)."""
    return emoji_tags_for((p.get("display_name") or "") + " " + (p.get("about") or ""),
                          p.get("emojis") or {}, limit=20)


def _profile_sig_from(display_name: str, avatar_url: str, about: str, emoji_tags: list | None = None) -> str:
    # Sign over the emoji tags we ACTUALLY emit (shortcodes present in the name/bio), not the whole
    # declared map — so an already-mirrored puppet whose plain text is unchanged still republishes once
    # to GAIN its tags, but an upstream emoji change unused in the name/bio doesn't force a no-op rewrite.
    emo = ",".join(f"{t[1]}={t[2]}" for t in (emoji_tags or []) if len(t) >= 3)
    raw = "\x1f".join([display_name or "", avatar_url or "", (about or "")[:200], nip05_domain(), emo])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _account_profile_sig(account: dict) -> str:
    """The profile signature computed straight from a raw account object — WITHOUT deriving the puppet
    key — so a cache lookup can decide 'unchanged' cheaply (no HMAC/EC work for repeat authors)."""
    dn = (account.get("display_name") or account.get("name") or "").strip()
    about = _strip_html(account.get("note") or account.get("description") or "")
    etags = emoji_tags_for(dn + " " + about, _emoji_url_map(account.get("emojis")), limit=20)
    return _profile_sig_from(
        dn,
        (account.get("avatar") or account.get("avatar_static") or account.get("avatarUrl") or "").strip(),
        about, etags)


# Provisioned-this-process puppets: actor_uri → {"p": puppet dict, "sig": profile sig}. A hit skips
# key derivation + the FediPuppet DB round-trip entirely (timelines repeat the same authors a lot).
_PUPPET_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_PUPPET_CACHE_MAX = 5000


def build_event(p: dict, kind: int, content: str, tags: list | None = None,
                object_uri: str | None = None, broadcast: bool = False,
                created_at: int | None = None) -> dict:
    """Sign a puppet event, attaching the mandatory `fedibridge` actor anchor (so the relay validates
    it), a NIP-48 `proxy` deep-link to the original fedi object, and — unless broadcast is enabled —
    a `nofederate` marker so the relay keeps the mirror local-only (see server._broadcastable).

    `created_at` (unix seconds) pins the timestamp so a caller that may RE-PUBLISH the same logical
    event (e.g. a retried favourite/boost) produces the IDENTICAL event id each time and the relay
    dedups it instead of storing a second copy. Defaults to now (a fresh id every call)."""
    t = list(tags or [])
    t.append([bridge_keys.ACTOR_TAG, p["actor_uri"]])
    if object_uri:
        t.append(["proxy", object_uri, "activitypub"])
    if not broadcast:
        t.append(["nofederate"])
    return _build_event(p["seckey"], kind, content, tags=t, created_at=created_at)


async def publish(port: int, ev: dict, timeout: float = 8.0) -> tuple[bool, str]:
    """Publish over the warm persistent connection; reconnect once on failure. Serialized by a lock
    so concurrent callers don't interleave their OK responses on the shared socket."""
    async with _ws_lock:
        for attempt in (1, 2):
            try:
                ws = await _relay_ws(port)
                await ws.send(json.dumps(["EVENT", ev]))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                    if msg[0] == "OK" and msg[1] == ev["id"]:
                        return bool(msg[2]), (msg[3] if len(msg) > 3 else "")
                    # NOTICE / other control frames on a publish-only socket → ignore and keep reading.
                    # (Oversized events never reach a NOTICE — the websockets layer drops the >512KB frame
                    # first — so the bridge guards event size proactively before publishing instead.)
            except Exception as e:
                global _ws
                _ws = None        # drop the dead socket; second attempt reconnects
                if attempt == 2:
                    return False, str(e)
    return False, "unreachable"


async def query_one(port: int, filt: dict, timeout: float = 8.0) -> tuple[bool, dict | None]:
    """Fetch the single most-recent event matching `filt` from the local relay over a short-lived
    connection (NOT the shared publish socket). Returns (ok, event|None); ok=False means the query
    itself failed — the caller must NOT treat that as 'no such event' (avoids the replaceable-list
    wipe bug where an empty read overwrites a real list)."""
    import os
    import websockets
    sub = "q" + os.urandom(4).hex()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/relay", open_timeout=10,
                                      close_timeout=2, ping_interval=30) as ws:
            await ws.send(json.dumps(["REQ", sub, filt]))
            got = None
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if msg[0] == "EVENT" and msg[1] == sub and isinstance(msg[2], dict):
                    if got is None or (msg[2].get("created_at", 0) > got.get("created_at", 0)):
                        got = msg[2]
                elif msg[0] == "EOSE" and msg[1] == sub:
                    return True, got
    except Exception as e:
        logger.debug("[fedi-bridge] query_one failed: %s", e)
        return False, None


async def ensure_puppet(db, port: int, account: dict, instance_host: str = "",
                        profile_refresh: bool = True) -> dict | None:
    """Provision (or refresh) a fediverse account's puppet: upsert the registry row, and (re)publish
    its kind-0 profile when first seen or when the display name/avatar/bio/domain changed. Returns
    the puppet dict, or None if the account has no usable actor URI."""
    from app.models import FediPuppet
    actor_uri = actor_uri_of(account)
    if not actor_uri:
        return None
    raw_sig = _account_profile_sig(account)
    # Fast path: already provisioned this run with an unchanged raw account → no key derivation, no DB.
    # Keyed on the RAW account sig (cheap) so an avatar-less mention sighting of a known user still
    # hits the cache and we don't redo work every note.
    cached = _PUPPET_CACHE.get(actor_uri)
    if cached is not None and cached["raw_sig"] == raw_sig:
        _PUPPET_CACHE.move_to_end(actor_uri)
        return cached["p"]

    p = puppet_for(account, instance_host)
    row = db.query(FediPuppet).filter(FediPuppet.actor_uri == p["actor_uri"]).first()
    # One person, one puppet. actor_uri is the PK and comes from `uri or url` — but Mastodon exposes an
    # actor as BOTH https://host/users/alice (uri) and https://host/@alice (url), and the mention path
    # builds a synthetic account that only has `url`. So the same person arrived under two keys and got
    # two puppets with two different pubkeys and one shared nip05_name: their follows/mentions/DMs split
    # across two Nostr identities, and the NIP-05 lookup flip-flopped between them (the relay map is
    # last-write-wins). If this handle already has a puppet under the other URI form, REUSE it.
    if row is None and p.get("acct"):
        alt = (db.query(FediPuppet)
               .filter(FediPuppet.acct == p["acct"])
               .order_by(FediPuppet.created_at.asc()).first())
        if alt is not None:
            actor_uri = alt.actor_uri            # keep the original key (and its derived pubkey)
            p = puppet_for({**account, "uri": alt.actor_uri, "url": alt.actor_uri}, instance_host)
            row = alt
    # A mention-only sighting passes a SYNTHETIC account ({url, acct, username, display_name=username})
    # with no real profile fields (the caller sets profile_refresh=False). Recomputing the kind-0 from it
    # would downgrade an already-mirrored profile — blank the bio, drop emoji tags, revert the name to the
    # bare username. So for a KNOWN puppet, don't touch the profile: mark it seen and return the identity.
    # A mention-only sighting carries a SYNTHETIC account (no avatar, no bio, display_name = username).
    # The guard below only covered a KNOWN puppet, so a FIRST sighting via a mention fell through and
    # published a degraded kind-0 — blank bio, no picture, no emoji — which then owned that identity's
    # NIP-05 registration. Register the row but publish nothing; the next sighting with a real account
    # object fills the profile in (profile_sig stays NULL so it will).
    if row is None and not profile_refresh:
        row = FediPuppet(actor_uri=p["actor_uri"], acct=p["acct"], instance_host=p["host"],
                         pubkey_hex=p["pubkey_hex"], nip05_name=p["nip05_name"],
                         display_name=p["display_name"], avatar_url=p["avatar_url"],
                         profile_sig=None, last_seen=datetime.utcnow(), created_at=datetime.utcnow())
        db.add(row)
        try:
            db.commit()
        except Exception:
            db.rollback()
        _PUPPET_CACHE[actor_uri] = {"p": p, "raw_sig": raw_sig}
        _PUPPET_CACHE.move_to_end(actor_uri)
        while len(_PUPPET_CACHE) > _PUPPET_CACHE_MAX:
            _PUPPET_CACHE.popitem(last=False)
        return p
    if row is not None and not profile_refresh:
        row.last_seen = datetime.utcnow()
        try:
            db.commit()
        except Exception:
            db.rollback()
        _PUPPET_CACHE[actor_uri] = {"p": p, "raw_sig": raw_sig}
        _PUPPET_CACHE.move_to_end(actor_uri)
        while len(_PUPPET_CACHE) > _PUPPET_CACHE_MAX:
            _PUPPET_CACHE.popitem(last=False)
        return p
    # Don't DOWNGRADE a known avatar: a real sighting can still be momentarily avatar-less, so an
    # existing good avatar must survive. The kind-0's `picture` is what the client renders in both
    # timeline and profile view, so a blank republish leaves the stored-latest profile pictureless.
    if row is not None and not p["avatar_url"] and row.avatar_url:
        p["avatar_url"] = row.avatar_url
    # Signature over exactly what gets published (name/avatar/bio/domain + the emoji tags we actually
    # emit) so an upstream emoji change that isn't used in the name/bio doesn't trigger a no-op republish.
    emoji_tags = _profile_emoji_tags(p)
    sig = _profile_sig_from(p["display_name"], p["avatar_url"], p["about"], emoji_tags)
    now = datetime.utcnow()
    need_profile = False
    if row is None:
        row = FediPuppet(actor_uri=p["actor_uri"], acct=p["acct"], instance_host=p["host"],
                         pubkey_hex=p["pubkey_hex"], nip05_name=p["nip05_name"],
                         display_name=p["display_name"], avatar_url=p["avatar_url"],
                         profile_sig=None, last_seen=now, created_at=now)
        db.add(row)
        need_profile = True
    else:
        row.acct = p["acct"]
        row.instance_host = p["host"]
        row.display_name = p["display_name"]
        row.avatar_url = p["avatar_url"]
        row.nip05_name = p["nip05_name"]
        row.last_seen = now
        # Re-publish the kind-0 only when the display name / avatar / bio / domain actually changed
        # (profile_sig captures all of those) or it was never published.
        need_profile = row.profile_sig != sig
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.debug("[fedi-bridge] puppet upsert failed for %s: %s", p["acct"], e)

    if need_profile:
        broadcast = str(settings_store.get("fedi_bridge_broadcast", "false")).lower() in ("1", "true", "yes", "on")
        ev = build_event(p, 0, json.dumps(_profile_content(p)), tags=emoji_tags,
                         object_uri=p["actor_uri"], broadcast=broadcast)
        ok, msg = await publish(port, ev)
        if ok:
            row.profile_sig = sig
            try:
                db.commit()
            except Exception:
                db.rollback()
        else:
            logger.debug("[fedi-bridge] profile publish failed for %s: %s", p["acct"], msg)
            return p   # don't cache as 'done' until the profile actually published

    _PUPPET_CACHE[actor_uri] = {"p": p, "raw_sig": raw_sig}
    _PUPPET_CACHE.move_to_end(actor_uri)
    while len(_PUPPET_CACHE) > _PUPPET_CACHE_MAX:
        _PUPPET_CACHE.popitem(last=False)
    return p
