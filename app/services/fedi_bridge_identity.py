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
from urllib.parse import urlparse
import asyncio
import hashlib
import logging
from collections import OrderedDict
from datetime import datetime, timedelta

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


def _https_or_blank(url) -> str:
    """A profile picture is shown to every reader: only an https address is kept (a plain-http or
    LAN one would let the remote server track readers or reach into their network)."""
    u = str(url or "").strip()
    return u if u.startswith("https://") and len(u) < 2048 else ""


_COMMUNITY_PATH = re.compile(r"^/(?:c|m)/[^/]+/?$")


def is_community_uri(uri: str) -> bool:
    """A Lemmy (`/c/foo`) or Mbin (`/m/foo`) community -- which shares its `foo@host` handle with the
    PERSON `/u/foo` on the same server."""
    return bool(_COMMUNITY_PATH.match(urlparse(uri or "").path or ""))


# ---- profile fields and the payment addresses in them ------------------------------------------
#
# A fediverse profile's key/value FIELDS (Mastodon `fields`, ActivityPub `attachment` PropertyValues)
# are where people put a wallet address — "Monero Wallet: 4Avre3…". The puppet's kind-0 used to carry
# the bio only, so every one of those was dropped on the floor here, while momostr (which flattens the
# fields into the bio) made the same person tippable on Primal. Fediverse people rarely have a
# Lightning address, so for most of them a field address is the ONLY way anybody here can pay them.
# 20, not a guess at "most people": Akkoma/Pleroma default to TEN fields and an admin can raise it,
# and a wallet is typically listed LAST. At 8 the real report this was written for
# (matty@nicecrew.digital: 9 fields, `$eth` ninth) silently lost its Ethereum address.
_MAX_FIELDS = 20
_XMR_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[48][1-9A-HJ-NP-Za-km-z]{94}(?:[1-9A-HJ-NP-Za-km-z]{11})?(?![1-9A-HJ-NP-Za-km-z])")
_ETH_RE = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])")
# A Lightning ADDRESS is shaped exactly like an e-mail address, and a profile field saying
# "contact: me@example.com" is far commoner than one saying "⚡: me@getalby.com" (measured on the 329
# fediverse actors this node federates with: 2 Lightning fields, many more contact links). Read as a
# zap target, a mail address sends the payment to whatever `example.com/.well-known/lnurlp/me` answers —
# possibly a different person. So a user@domain value counts ONLY under a Lightning LABEL; an LNURL
# (bech32, `lnurl1…`) is self-describing and counts anywhere in a field.
_LN_LABEL = re.compile(r"(?i)⚡|\blightning\b|\blnurl\b|\bln\b|\bzaps?\b|\blud16\b|\bsats\b|\btips?\b")
_LN_ADDR = re.compile(r"^(?:lightning:)?([a-z0-9._+-]{1,64}@[a-z0-9-]+(?:\.[a-z0-9-]+)+)$", re.I)
_LNURL_RE = re.compile(r"(?<![0-9a-z])lnurl1[02-9ac-hj-np-z]{20,}(?![0-9a-z])", re.I)


def profile_fields(account: dict) -> list:
    """[(name, value)] as plain text, from either shape: the Mastodon API's `fields` or an AP actor's
    `attachment` PropertyValues. Values are HTML on both (a link is an <a>), so they are flattened."""
    raw = account.get("fields")
    if not isinstance(raw, list):
        raw = [a for a in (account.get("attachment") or []) if isinstance(a, dict)
               and a.get("type") == "PropertyValue"] if isinstance(account.get("attachment"), list) else []
    out = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        name = _strip_html(str(f.get("name") or "")).strip().rstrip(":").strip()[:100]
        value = _strip_html(str(f.get("value") or "")).strip()[:1000]
        if name or value:
            out.append((name, value))
        if len(out) >= _MAX_FIELDS:
            break
    return out


def payment_addresses(fields: list, about: str = "") -> dict:
    """{"monero", "ethereum", "lightning"(lud16), "lnurl"(lud06)} found in the fields first, then the bio.
    Monero and Ethereum count only as a token that IS an address — never a guess from a label — so a
    mislabelled field cannot misroute money; the worst a wrong label can do is nothing. Lightning is the
    one exception and is the stricter for it: see `_LN_LABEL`."""
    found = {}
    for name, value in fields:
        if "lightning" not in found and _LN_LABEL.search(name or ""):
            m = _LN_ADDR.match((value or "").strip())
            if m:
                found["lightning"] = m.group(1).lower()
        if "lnurl" not in found:
            m = _LNURL_RE.search(value or "")
            if m:
                found["lnurl"] = m.group(0).lower()
    texts = [v for _, v in fields] + [about or ""]
    for t in texts:
        if "monero" not in found:
            m = _XMR_RE.search(t)
            if m:
                found["monero"] = m.group(0)
        if "ethereum" not in found:
            m = _ETH_RE.search(t)
            if m:
                found["ethereum"] = m.group(0)
    return found


def _fields_text(fields: list) -> str:
    return "\n".join(f"{n}: {v}" if n else v for n, v in fields)


def puppet_for(account: dict, instance_host: str = "") -> dict:
    """Resolve the full puppet identity for a fediverse account (no I/O, no DB)."""
    actor_uri = actor_uri_of(account)
    acct = acct_of(account, instance_host)
    nip05 = nip05_name_for(acct)
    if is_community_uri(actor_uri):
        # Its own name: the person and the community have separate keys, and one NIP-05 name for both
        # (the relay's map is last-write-wins) flipped between them on every profile refresh.
        nip05 = f"{nip05[:57].strip('._-')}_group"
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
        "nip05_name": nip05,
        # display_name is PLAIN TEXT on Mastodon/Pleroma (never HTML) — do NOT tag-strip it, or
        # angle-bracket kaomoji like <(^o^)> get eaten. It keeps its :shortcode: emoji (rendered via the
        # NIP-30 tags below). The BIO is fediverse HTML (<br>, <a>, entities) → flatten to text or the
        # client shows raw markup. Custom-emoji shortcode→url map drives the profile's NIP-30 emoji tags.
        "display_name": (account.get("display_name") or account.get("name") or "").strip(),
        "avatar_url": _https_or_blank(account.get("avatar") or account.get("avatar_static")
                                      or account.get("avatarUrl") or "") if isinstance(
            account.get("avatar") or account.get("avatar_static") or account.get("avatarUrl") or "", str) else "",
        "about": _strip_html(account.get("note") or account.get("description") or ""),
        "fields": profile_fields(account),
        "emojis": _emoji_url_map(account.get("emojis")),
    }


def puppet_from_actor(actor_uri: str, acct: str = "") -> dict:
    """Re-derive a puppet's signing identity from just its canonical actor URI — used to sign a
    NIP-09 deletion for a note we mirrored earlier (we only stored the pubkey, not the secret)."""
    sk = bridge_keys.derive_seckey(_secret(), actor_uri)
    pubkey_hex = nostr_service.derive_pubkey(sk)
    return {"seckey": sk, "pubkey_hex": pubkey_hex, "npub": nostr_service.npub_of(pubkey_hex),
            "actor_uri": actor_uri, "acct": acct, "host": "", "nip05_name": "",
            "display_name": "", "avatar_url": "", "about": "", "fields": []}


async def delete_note(port: int, actor_uri: str, nostr_event_id: str, broadcast: bool = False) -> bool:
    """Publish a NIP-09 kind-5 deletion (signed by the puppet) for a mirrored note that was removed
    on the fediverse. Federates upstream iff broadcast is on (see build_event's nofederate handling)."""
    p = puppet_from_actor(actor_uri)
    ev = build_event(p, 5, "deleted on source", tags=[["e", nostr_event_id]], broadcast=broadcast)
    ok, _ = await publish(port, ev)
    return ok


def _profile_content(p: dict) -> dict:
    domain = nip05_domain()
    fields = p.get("fields") or []
    about = p["about"] or ""
    # The fields go into the bio as "name: value" lines, which is what every Nostr client can show —
    # there is no kind-0 key for them — and is exactly how momostr presents the same profile.
    if fields:
        about = ((about + "\n\n") if about else "") + _fields_text(fields)
    out = {
        "name": p["display_name"] or p["nip05_name"],
        "display_name": p["display_name"] or p["acct"].partition("@")[0],
        "about": ((about + "\n\n") if about else "") + f"🔗 bridged from {p['acct']} (fediverse)",
        "fediverse": p["acct"],
        "bridged": True,
    }
    # …and an address among them becomes the keys clients actually read, so the person gets a tip
    # button: `monero_address` (+ `xmr`, the alias this client writes) and `ethereum`.
    pay = payment_addresses(fields, p["about"] or "")
    if pay.get("monero"):
        out["monero_address"] = out["xmr"] = pay["monero"]
    if pay.get("ethereum"):
        out["ethereum"] = pay["ethereum"]
    # Lightning goes to the standard keys, so the ordinary ⚡ zap button works for them. A zap to an
    # LNURL server without Nostr support is still a payment (tips.js simply sends no zap request).
    if pay.get("lightning"):
        out["lud16"] = pay["lightning"]
    if pay.get("lnurl"):
        out["lud06"] = pay["lnurl"]
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


def _profile_sig_from(display_name: str, avatar_url: str, about: str, emoji_tags: list | None = None,
                      fields: list | None = None) -> str:
    # Sign over the emoji tags we ACTUALLY emit (shortcodes present in the name/bio), not the whole
    # declared map — so an already-mirrored puppet whose plain text is unchanged still republishes once
    # to GAIN its tags, but an upstream emoji change unused in the name/bio doesn't force a no-op rewrite.
    emo = ",".join(f"{t[1]}={t[2]}" for t in (emoji_tags or []) if len(t) >= 3)
    raw = "\x1f".join([display_name or "", avatar_url or "", (about or "")[:200], nip05_domain(), emo])
    # Fields are hashed WHOLE and only when present, so a puppet with none keeps the signature it was
    # published under (no mass republish on deploy) while one with an address republishes once to gain it.
    if fields:
        raw += "\x1f" + hashlib.sha256(_fields_text(fields).encode("utf-8")).hexdigest()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _account_profile_sig(account: dict) -> str:
    """The profile signature computed straight from a raw account object — WITHOUT deriving the puppet
    key — so a cache lookup can decide 'unchanged' cheaply (no HMAC/EC work for repeat authors)."""
    dn = (account.get("display_name") or account.get("name") or "").strip()
    about = _strip_html(account.get("note") or account.get("description") or "")
    etags = emoji_tags_for(dn + " " + about, _emoji_url_map(account.get("emojis")), limit=20)
    av = account.get("avatar") or account.get("avatar_static") or account.get("avatarUrl") or ""
    return _profile_sig_from(dn, av.strip() if isinstance(av, str) else "", about, etags,
                             profile_fields(account))


# Provisioned-this-process puppets: actor_uri → {"p": puppet dict, "sig": profile sig}. A hit skips
# key derivation + the puppet-registry round-trip entirely (timelines repeat the same authors a lot).
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


def dm_relay_url() -> str:
    """This node's public relay address -- where a puppet receives DMs."""
    url = (settings_store.get("client_relay_url", "") or "").strip()
    if url.startswith(("ws://", "wss://")):
        return url
    domain = nip05_domain()
    return f"wss://{domain}/relay" if domain else ""


async def _publish_dm_relays(port: int, p: dict, broadcast: bool) -> None:
    """A puppet's NIP-17 DM-relay list (kind 10050) naming THIS relay. A Nostr client sends a DM
    to wherever the recipient's 10050 says; a puppet with none is a person nobody can write to --
    and this relay is where its messages can be opened and carried to the fediverse (the relay
    accepts DMs addressed to puppets). Best effort: the profile is what matters for the mirror."""
    url = dm_relay_url()
    if not url:
        return
    try:
        await publish(port, build_event(p, 10050, "", tags=[["relay", url]], object_uri=p["actor_uri"],
                                        broadcast=broadcast))
    except Exception as e:
        logger.debug("[fedi-bridge] DM relay list not published for %s: %s", p.get("acct"), e)


def _same_person_alias(a: str, b: str) -> bool:
    """Two actor addresses that are ONE person's two URI forms: the SAME server, and one of them a
    profile URL (`https://host/@alice` beside `https://host/users/alice`) -- the only split the reuse
    below exists for. A handle alone never proves it: an `acct` is whatever the sender typed, so
    reusing on it let any server that named `victim@mastodon.social` first claim that person's key
    (a Mention tag, an import answer), and on Lemmy `/u/foo` and `/c/foo` share `foo@host`."""
    pa, pb = urlparse(a or ""), urlparse(b or "")
    if not pa.hostname or (pa.hostname or "").lower() != (pb.hostname or "").lower():
        return False
    return (pa.path or "").startswith("/@") or (pb.path or "").startswith("/@")


def _cache_puppet(actor_uri: str, p: dict, raw_sig: str) -> None:
    _PUPPET_CACHE[actor_uri] = {"p": p, "raw_sig": raw_sig}
    _PUPPET_CACHE.move_to_end(actor_uri)
    while len(_PUPPET_CACHE) > _PUPPET_CACHE_MAX:
        _PUPPET_CACHE.popitem(last=False)


_SEEN_EVERY = timedelta(days=1)     # last_seen is bookkeeping: one registry write a day per account, not per sighting


async def ensure_puppet(db, port: int, account: dict, instance_host: str = "",
                        profile_refresh: bool = True) -> dict | None:
    """Provision (or refresh) a fediverse account's puppet: upsert the registry row, and (re)publish
    its kind-0 profile when first seen or when the display name/avatar/bio/domain changed. Returns
    the puppet dict, or None if the account has no usable actor URI.

    The registry is the DocTable `fedi_puppets` (fedi_tables); `db` is no longer used and is kept only
    so callers need not change. A registry that cannot be read RAISES (relay_reader.Unavailable) before
    any key is derived or anything is registered: read as "unknown person", the alias rule below would be
    skipped and a second identity minted beside the real one."""
    from app.services import fedi_tables
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

    # The registry FIRST: nothing is derived from an answer that was never given.
    row = await fedi_tables.apuppet_by_uri(actor_uri)
    # One person, one puppet. actor_uri is the PK and comes from `uri or url` — but Mastodon exposes an
    # actor as BOTH https://host/users/alice (uri) and https://host/@alice (url), and the mention path
    # builds a synthetic account that only has `url`. So the same person arrived under two keys and got
    # two puppets with two different pubkeys and one shared nip05_name: their follows/mentions/DMs split
    # across two Nostr identities, and the NIP-05 lookup flip-flopped between them (the relay map is
    # last-write-wins). If this handle already has a puppet under the other URI form, REUSE it.
    acct = acct_of(account, instance_host)
    if row is None and acct:
        alts = await fedi_tables.apuppets_with_acct(acct)
        alt = alts[0] if alts else None
        if alt is not None and _same_person_alias(alt.actor_uri, actor_uri):
            row = alt
            account = {**account, "uri": alt.actor_uri, "url": alt.actor_uri}  # keep the original key
    p = puppet_for(account, instance_host)
    now = datetime.utcnow()

    def _new_row(profile_sig=None) -> dict:
        return {"actor_uri": p["actor_uri"], "acct": p["acct"], "instance_host": p["host"],
                "pubkey_hex": p["pubkey_hex"], "nip05_name": p["nip05_name"],
                "display_name": p["display_name"], "avatar_url": p["avatar_url"],
                "profile_sig": profile_sig, "last_seen": now, "created_at": now}

    async def _save(r: dict) -> bool:
        try:
            await fedi_tables.aput_puppet(r)
            return True
        except Exception as e:      # noqa: BLE001
            logger.debug("[fedi-bridge] puppet upsert failed for %s: %s", p["acct"], type(e).__name__)
            return False
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
        if await _save(_new_row()):
            _cache_puppet(actor_uri, p, raw_sig)
        return p
    if row is not None and not profile_refresh:
        from app.services.table_migrate import dt as _dt
        seen = _dt(row.last_seen)
        ok = True
        if seen is None or now - seen >= _SEEN_EVERY:
            ok = await _save({**row, "last_seen": now})
        if ok:
            _cache_puppet(actor_uri, p, raw_sig)
        return p
    # Don't DOWNGRADE a known avatar: a real sighting can still be momentarily avatar-less, so an
    # existing good avatar must survive. The kind-0's `picture` is what the client renders in both
    # timeline and profile view, so a blank republish leaves the stored-latest profile pictureless.
    if row is not None and not p["avatar_url"] and row.avatar_url:
        p["avatar_url"] = row.avatar_url
    # Signature over exactly what gets published (name/avatar/bio/domain + the emoji tags we actually
    # emit) so an upstream emoji change that isn't used in the name/bio doesn't trigger a no-op republish.
    emoji_tags = _profile_emoji_tags(p)
    sig = _profile_sig_from(p["display_name"], p["avatar_url"], p["about"], emoji_tags,
                            p.get("fields") or [])
    if row is None:
        rec = _new_row()
        need_profile = True
    else:
        rec = {**row, "acct": p["acct"], "instance_host": p["host"], "display_name": p["display_name"],
               "avatar_url": p["avatar_url"], "nip05_name": p["nip05_name"], "last_seen": now}
        # Re-publish the kind-0 only when the display name / avatar / bio / domain actually changed
        # (profile_sig captures all of those) or it was never published.
        need_profile = row.profile_sig != sig
    saved = await _save(rec)

    if need_profile:
        broadcast = str(settings_store.get("fedi_bridge_broadcast", "false")).lower() in ("1", "true", "yes", "on")
        ev = build_event(p, 0, json.dumps(_profile_content(p)), tags=emoji_tags,
                         object_uri=p["actor_uri"], broadcast=broadcast)
        ok, msg = await publish(port, ev)
        if ok:
            await _publish_dm_relays(port, p, broadcast)
            saved = await _save({**rec, "profile_sig": sig})
        else:
            logger.debug("[fedi-bridge] profile publish failed for %s: %s", p["acct"], msg)
            return p   # don't cache as 'done' until the profile actually published
    if not saved:
        return p       # nor until the registry holds it

    _PUPPET_CACHE[actor_uri] = {"p": p, "raw_sig": raw_sig}
    _PUPPET_CACHE.move_to_end(actor_uri)
    while len(_PUPPET_CACHE) > _PUPPET_CACHE_MAX:
        _PUPPET_CACHE.popitem(last=False)
    return p
