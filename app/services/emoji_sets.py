"""The instance's custom emoji packs, published as Nostr EMOJI SETS (NIP-51 kind 30030).

Emoji already travel inside posts (NIP-30 `["emoji", shortcode, url]` tags), which is why Ditto and the
fediverse render them. What other Nostr clients could not do was ADD a pack: Amethyst, Nostrudel, Coracle and
the rest browse kind-30030 sets and keep the ones a person uses in their kind-10030 list. So every pack here is
published as a set signed by this node's operator key, on this node's relay, and an `naddr` points at it.

Rules, each because a client depends on it:
  * a NIP-30 shortcode is letters, digits and `_` only -- anything else is mapped to `_` (the IMAGE URL keeps the
    pack's real file name), and duplicates that mapping produces are dropped;
  * URLs are absolute on the instance's public origin, or other clients cannot load them;
  * a big pack is split into several sets (`d`, `d-2`, ...), because relays refuse oversized events;
  * a pack that is gone is WITHDRAWN (an empty set, then a NIP-09 deletion), never left advertising dead images;
  * nothing is published when nothing changed (a content hash is kept), so a restart does not rewrite every set.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import struct

from app.services import emoji_service, keystore, settings_store
from app.services.activitypub import config as ap_config
from app.services.nostr import bech32, bip340, event as nostr_event, nostr_service

logger = logging.getLogger(__name__)

KIND_SET = 30030
D_PREFIX = "pcai-emoji-"
PER_SET = 300                     # ~300 tags stays well under the common 64-128 KB relay event limits
STATE_KEY = "emoji_sets_cursor"   # {"hash": ..., "d": [...]}; per node (a `_cursor` key never leaves it)


def nip30_shortcode(sc: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_]", "_", str(sc or "")).strip("_")
    return out[:64]


def _slug(pack: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", str(pack or "").lower()).strip("-_")
    return s or "default"      # the loose-images pack "_" included


def build_sets(entries: list[dict], base: str, titles: dict | None = None) -> list[dict]:
    """Pure: emoji index entries -> [{"d", "title", "tags"}] (no signing)."""
    base = (base or "").rstrip("/")
    titles = titles or {}
    by_pack: dict[str, list] = {}
    for e in entries or []:
        by_pack.setdefault(e.get("pack") or "_", []).append(e)
    sets = []
    for pack in sorted(by_pack):
        seen, tags = set(), []
        for e in sorted(by_pack[pack], key=lambda x: x.get("shortcode", "")):
            code = nip30_shortcode(e.get("shortcode", ""))
            if not code or code in seen:
                continue
            seen.add(code)
            tags.append(["emoji", code, f"{base}/client/emoji/{e['pack']}/{e['shortcode']}{e.get('ext', '')}"])
        if not tags:
            continue
        title = titles.get(pack) or ("PosterChan" if pack == "_" else pack)
        chunks = [tags[i:i + PER_SET] for i in range(0, len(tags), PER_SET)]
        for n, chunk in enumerate(chunks, 1):
            d = D_PREFIX + _slug(pack) + ("" if n == 1 else f"-{n}")
            t = title if len(chunks) == 1 else f"{title} ({n}/{len(chunks)})"
            sets.append({"d": d, "title": t, "tags": [["d", d], ["title", t], ["image", chunk[0][2]]] + chunk})
    return sets


def naddr(kind: int, pubkey_hex: str, identifier: str, relays: list[str]) -> str:
    """NIP-19 naddr (TLV 0 identifier, 1 relay, 2 author, 3 kind)."""
    def tlv(t, v):
        return bytes([t, len(v)]) + v
    payload = tlv(0, identifier.encode())
    for r in relays:
        payload += tlv(1, r.encode())
    payload += tlv(2, bytes.fromhex(pubkey_hex)) + tlv(3, struct.pack(">I", kind))
    return bech32.encode("naddr", payload)


def _relay_url() -> str:
    b = ap_config.base_url()
    return (b.replace("https://", "wss://", 1) + "/relay") if b else ""


def _operator():
    nsec = keystore.get_operator_nsec()
    if not nsec:
        return None, ""
    sk = nostr_service.decode_seckey(nsec)
    return sk, bip340.pubkey_from_seckey(sk).hex()


def listing() -> list[dict]:
    """What Admin -> Emoji shows: each published set with its naddr."""
    base = ap_config.base_url()
    sk, pk = _operator()
    if not base or not pk:
        return []
    relay = _relay_url()
    return [{"d": s["d"], "title": s["title"], "count": sum(1 for t in s["tags"] if t[0] == "emoji"),
             "naddr": naddr(KIND_SET, pk, s["d"], [relay] if relay else [])}
            for s in build_sets(emoji_service.index(), base)]


async def _publish_local(ev: dict) -> bool:
    port = int(settings_store.get("nostr_relay_port", "3052") or "3052")
    return bool(await nostr_service.relay.publish([f"ws://127.0.0.1:{port}"], ev))


async def publish_all(force: bool = False) -> dict:
    """Publish every pack as a set; withdraw sets whose pack is gone. Returns a small report."""
    base = ap_config.base_url()
    sk, pk = _operator()
    if not base or sk is None:
        return {"published": 0, "skipped": "no public domain or operator key"}
    sets = build_sets(emoji_service.index(), base)
    digest = hashlib.sha256(json.dumps([s["tags"] for s in sets], sort_keys=True).encode()).hexdigest()
    try:
        state = json.loads(settings_store.get(STATE_KEY, "") or "{}")
    except Exception:
        state = {}
    if not force and state.get("hash") == digest:
        return {"published": 0, "skipped": "unchanged"}
    ok = 0
    for s in sets:
        if await _publish_local(nostr_event.build_event(sk, KIND_SET, "", s["tags"])):
            ok += 1
    gone = [d for d in state.get("d", []) if d not in {s["d"] for s in sets}]
    for d in gone:
        await _publish_local(nostr_event.build_event(sk, KIND_SET, "", [["d", d]]))
        await _publish_local(nostr_event.build_event(sk, 5, "", [["a", f"{KIND_SET}:{pk}:{d}"], ["k", str(KIND_SET)]]))
    if ok == len(sets):   # only a complete publish is remembered; a partial one is retried next time
        settings_store.put(STATE_KEY, json.dumps({"hash": digest, "d": [s["d"] for s in sets]}))
    logger.info("[emoji-sets] published %d/%d set(s), withdrew %d", ok, len(sets), len(gone))
    return {"published": ok, "sets": len(sets), "withdrawn": len(gone)}


_pending: asyncio.Task | None = None


def schedule_publish(delay: float = 5.0) -> None:
    """Debounced: an admin uploading twenty emoji publishes once, a few seconds after the last."""
    global _pending
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _pending and not _pending.done():
        _pending.cancel()

    async def run():
        await asyncio.sleep(delay)
        try:
            await publish_all()
        except Exception as e:
            logger.warning("[emoji-sets] publish failed: %s", e)
    _pending = loop.create_task(run())
