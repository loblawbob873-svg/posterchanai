"""High-level Nostr client — the app-facing facade.

Used by the user-linking router, the notification relay, and the bot adapter.
Posting publishes to every configured relay; reading queries them all. Media is
uploaded to an external Blossom/NIP-96 host and its URL embedded in the content
(the Nostr-specific media behavior) — see media.py.
"""

import logging

from . import bech32, bip340, event as _event, relay, media

logger = logging.getLogger(__name__)

# Default relays (shared by bots + users); user/bot config may override.
# relay.damus.io / relay.ditto.pub excluded: they reject our WS connections (HTTP 503) or
# time out during the opening handshake on nearly every poll, so each poll waited out a
# connect timeout and mention pickup/replies were delayed minutes.
# Default upstream relays for fresh installs (the set this deployment syncs with). Used as the
# fallback whenever a relay list is blank — the built-in relay's upstream sync AND the bots'
# fetch/post. Existing installs keep whatever they've saved in nostr_relay_upstream_relays.
DEFAULT_RELAYS = [
    "wss://relay.snort.social/",
    "wss://nos.lol/",
    "wss://relay.primal.net/",
    "wss://nostr.mom/",
    "wss://nostr.oxtr.dev/",
    "wss://offchain.pub/",
    "wss://relay.ditto.pub/",
    "wss://relay.froth.zone/",
    "wss://frens.nostr1.com/",
    "wss://nostr.chaima.info/",
    "wss://relay.wisp.talk/",
    "wss://eden.nostr.land/",
    "wss://nostr.corebreach.com/",
    "wss://social.amanah.eblessing.co/",
    "wss://relay.sovrgn.co.za/",
    "wss://nostr.azzamo.net/",
    "wss://relay.nostr.net/",
    # 0xchat interop (NIP-17 DMs) — relay.0xchat.com is where 0xchat users + their kind-10050 live.
    # We deliberately do NOT add the highest-volume firehoses (relay.damus.io, relay.nostr.band), the
    # AUTH/paid/bridge relays (auth.nostr1.com, inbox.nostr.wine, nostr.wine[429], relay.mostr.pub), or
    # purplepag.es (profile-ONLY — it rejects every note we federate, so it's pure outbox churn): they
    # flooded the relay's startup with so much traffic/errors that it timed out bots' profile publish +
    # sync on every restart (the "bot never goes green / nip05 not set" cascade). A couple of moderate
    # extras are fine.
    "wss://relay.0xchat.com/",
    "wss://yabu.me/",
    "wss://nostr.data.haus/",
]

# Default WoT seed accounts — well-known PUBLIC Nostr npubs whose follow graphs bootstrap a fresh
# relay's web-of-trust (seeds + everyone they follow) so a new install has a populated timeline out
# of the box, not just the operator. The ONE source of truth: database.py seeds these into settings
# and the relay falls back to them when the setting is empty (a fresh node's store isn't seeded yet
# at first WoT build → without the fallback the WoT was just the operator). Admin edits the live set.
DEFAULT_WOT_SEEDS = [
    "npub1gu9wxzm9y3uwunva2d6tedef64r33dfdessjhuvp5hf8zampj5nseec39q",
    "npub153xmex42x4chdf757hp3q6zxagykkek7pdgwuwd074964dkyha9s82ryu8",
    "npub1gcxzte5zlkncx26j68ez60fzkvtkm9e0vrwdcvsjakxf9mu9qewqlfnj5z",
    "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6",
    "npub1jk9h2jsa8hjmtm9qlcca942473gnyhuynz5rmgve0dlu6hpeazxqc3lqz7",
    "npub18ams6ewn5aj2n3wt2qawzglx9mr4nzksxhvrdc4gzrecw7n5tvjqctp424",
    "npub1utx00neqgqln72j22kej3ux7803c2k986henvvha4thuwfkper4s7r50e8",
    "npub1lrnvvs6z78s9yjqxxr38uyqkmn34lsaxznnqgd877j4z2qej3j5s09qnw5",
    "npub1gke42gwrz2ja5np9tpcr449785hx6zxgzf2329x8584h4d06puzqg33xp3",
    "npub1sg6plzptd64u62a878hep2kev88swjh3tw00gjsfl8f237lmu63q0uf63m",
]


# --- key helpers ------------------------------------------------------------

def decode_seckey(s: str) -> bytes:
    """Decode an nsec/hex secret key to 32 raw bytes; raises ValueError if invalid."""
    sk = bech32.decode_key(s)
    if not sk or len(sk) != 32:
        raise ValueError("Invalid Nostr secret key (expected nsec1… or 64-char hex)")
    # Validate it produces a usable pubkey.
    bip340.pubkey_from_seckey(sk)
    return sk


def derive_pubkey(seckey: bytes) -> str:
    """Hex x-only pubkey for a raw secret key."""
    return bip340.pubkey_from_seckey(seckey).hex()


def npub_of(pubkey_hex: str) -> str:
    return bech32.encode("npub", bytes.fromhex(pubkey_hex))


def npub_from_seckey(s: str) -> str:
    return npub_of(derive_pubkey(decode_seckey(s)))


def to_pubkey_hex(s: str) -> str | None:
    """Accept npub/hex (or nprofile) → 32-byte hex pubkey."""
    s = (s or "").strip()
    if bech32.is_hex_key(s):
        return s
    raw = bech32.decode_any(s)
    return raw.hex() if raw else None


# --- posting ----------------------------------------------------------------

async def _attach_media(seckey: bytes, text: str, mediae: list, media_cfg: dict):
    """Upload each (bytes, mime) to the configured host; append URLs to text + imeta tags."""
    tags = []
    for (data, mime) in mediae or []:
        try:
            info = await media.upload(media_cfg or {}, seckey, data, mime)
            if info.get("url"):
                text = (text + "\n" + info["url"]).strip()
                tags.append(_event.imeta_tag(info["url"], info.get("mime", ""),
                                             info.get("sha256", ""), info.get("dim", "")))
        except Exception as e:
            logger.warning(f"[nostr] media upload failed: {e}")
    return text, tags


async def post_note(seckey: bytes, relays, text: str, reply_to: dict | None = None,
                    media_list: list | None = None, media_cfg: dict | None = None,
                    hashtags: list | None = None) -> dict:
    """Build, sign and publish a kind-1 note. Uploads media first and embeds URLs.

    `reply_to` is a parent event dict to reply to (adds NIP-10 tags). `hashtags` adds NIP-12
    indexed `t` tags (so the note shows up in those #hashtag feeds). Returns the event.
    """
    relays = relay.normalize_relays(relays) or DEFAULT_RELAYS
    tags = _event.reply_tags(reply_to) if reply_to else []
    had_text = bool((text or "").strip())
    text, media_tags = await _attach_media(seckey, text, media_list or [], media_cfg or {})
    # Don't publish a junk empty note: if the only content was media and every upload failed
    # (e.g. the host was unreachable, or the process was being torn down mid-send), posting an
    # empty kind-1 is worse than posting nothing — skip it and let the caller surface the failure.
    if not had_text and media_list and not media_tags:
        raise RuntimeError("post_note: all media uploads failed; refusing to publish an empty note")
    tags = tags + media_tags
    # NIP-12 hashtag tags (indexed, lowercased, deduped) so the note lands in #hashtag feeds.
    if hashtags:
        seen = set()
        for h in hashtags:
            h = str(h or "").lstrip("#").strip().lower()
            if h and h not in seen:
                seen.add(h)
                tags.append(["t", h])
    ev = _event.build_event(seckey, 1, text, tags=tags)
    await relay.publish(relays, ev)
    return ev


async def react(seckey: bytes, relays, target: dict, emoji: str = "+") -> dict:
    """NIP-25 reaction (kind 7) to a target event. '+' is the canonical 'like'."""
    relays = relay.normalize_relays(relays) or DEFAULT_RELAYS
    tags = [["e", target["id"]], ["p", target["pubkey"]]]
    ev = _event.build_event(seckey, 7, emoji or "+", tags=tags)
    await relay.publish(relays, ev)
    return ev


async def repost(seckey: bytes, relays, target: dict) -> dict:
    """NIP-18 repost (kind 6) of a target event."""
    relays = relay.normalize_relays(relays) or DEFAULT_RELAYS
    import json as _json
    tags = [["e", target["id"]], ["p", target["pubkey"]]]
    ev = _event.build_event(seckey, 6, _json.dumps(target, separators=(",", ":")), tags=tags)
    await relay.publish(relays, ev)
    return ev


# --- reading ----------------------------------------------------------------

async def fetch_mentions(pubkey_hex: str, relays, since: int | None = None, limit: int = 50) -> list[dict]:
    """kind-1/6/7 events that #p-tag the given pubkey (mentions/replies/reposts/reactions)."""
    relays = relay.normalize_relays(relays) or DEFAULT_RELAYS
    flt: dict = {"kinds": [1, 6, 7], "#p": [pubkey_hex], "limit": limit}
    if since:
        flt["since"] = int(since) + 1
    return await relay.query(relays, [flt])


async def fetch_event(relays, event_id: str) -> dict | None:
    relays = relay.normalize_relays(relays) or DEFAULT_RELAYS
    # Short timeout: a point lookup either EOSEs fast or isn't there — used on every
    # reply-chain hop, so a long wait per miss would compound badly.
    events = await relay.query(relays, [{"ids": [event_id], "limit": 1}], timeout=5)
    return events[0] if events else None


async def fetch_thread(relays, event_id: str, limit: int = 50) -> list[dict]:
    """Events that reference event_id via an e-tag (its replies/descendants)."""
    relays = relay.normalize_relays(relays) or DEFAULT_RELAYS
    return await relay.query(relays, [{"kinds": [1], "#e": [event_id], "limit": limit}])


async def get_metadata(pubkey_hex: str, relays) -> dict:
    """Latest kind-0 profile metadata (name/picture) for a pubkey, or {}."""
    relays = relay.normalize_relays(relays) or DEFAULT_RELAYS
    events = await relay.query(relays, [{"kinds": [0], "authors": [pubkey_hex], "limit": 1}], timeout=5)
    if not events:
        return {}
    import json as _json
    try:
        return _json.loads(events[0].get("content") or "{}")
    except (ValueError, TypeError):
        return {}
