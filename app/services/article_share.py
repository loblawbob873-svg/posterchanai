"""Link previews for long-form ARTICLES (NIP-23 kind 30023) shared as `poster.place/<naddr1…>`.

"article links ... are not showing as link previews when shared" (2026-10-30): the client shell carries no OpenGraph
on the `/{entity}` route, so every shared article rendered as the bare app title -- indistinguishable from a dead
link. Same shape as git_share (the repo page): the article is read off THIS node's relay and its title, summary and
image become `og:*` in <head>. Best-effort: anything that cannot be read leaves the page exactly as it was.
"""
from __future__ import annotations

import json
import logging
import re
import time

logger = logging.getLogger(__name__)

ARTICLE_KIND = 30023
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CARD_TTL = 300.0
_cache: dict[str, tuple[float, dict | None]] = {}


def _polymod(values) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def decode_naddr(s: str) -> dict | None:
    """`naddr1…` -> {kind, pubkey, d, relays}, or None for anything that is not a valid naddr (checksum verified)."""
    s = (s or "").strip().lower()
    if not s.startswith("naddr1") or len(s) > 2000:
        return None
    hrp, data = "naddr", s[6:]
    try:
        vals = [_CHARSET.index(c) for c in data]
    except ValueError:
        return None
    if len(vals) < 7:
        return None
    if _polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + vals) != 1:
        return None
    acc = bits = 0
    out = bytearray()
    for v in vals[:-6]:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    res: dict = {"relays": []}
    i = 0
    while i + 2 <= len(out):
        t, ln = out[i], out[i + 1]
        val = bytes(out[i + 2:i + 2 + ln])
        if len(val) != ln:
            return None
        if t == 0:
            res["d"] = val.decode("utf-8", "replace")
        elif t == 1:
            res["relays"].append(val.decode("utf-8", "replace"))
        elif t == 2 and ln == 32:
            res["pubkey"] = val.hex()
        elif t == 3 and ln == 4:
            res["kind"] = int.from_bytes(val, "big")
        i += 2 + ln
    if "pubkey" not in res or "kind" not in res or "d" not in res:
        return None
    return res


def _tag(ev: dict, key: str) -> str:
    for t in ev.get("tags") or []:
        if isinstance(t, list) and len(t) >= 2 and t[0] == key and isinstance(t[1], str):
            return t[1].strip()
    return ""


def _plain(md: str, n: int = 300) -> str:
    """The opening of a markdown article as plain prose for a preview line."""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md or "")            # images
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)               # links -> their text
    s = re.sub(r"nostr:[a-z0-9]+", " ", s)
    s = re.sub(r"[#>*_`~|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


async def article_card(port: int, ref: dict) -> dict | None:
    """The shareable facts about one article, read off this node's relay; None when it is not here."""
    if ref.get("kind") != ARTICLE_KIND:
        return None
    key = "%s/%s" % (ref["pubkey"], ref["d"])
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    card = None
    try:
        from app.services import nostr_store
        # strict: a dead relay must RAISE, not read as "no such article" and be cached for five minutes.
        evs = await nostr_store._ws_query(port, [{"authors": [ref["pubkey"]], "kinds": [ARTICLE_KIND],
                                                  "#d": [ref["d"]], "limit": 3}], timeout=4.0, strict=True)
        evs = [e for e in (evs or []) if e.get("pubkey") == ref["pubkey"]]
        if evs:
            e = max(evs, key=lambda x: x.get("created_at") or 0)
            card = {"title": _tag(e, "title") or "Article", "summary": _tag(e, "summary") or _plain(e.get("content") or ""),
                    "image": _tag(e, "image"), "author": "", "published": int(_tag(e, "published_at") or e.get("created_at") or 0)}
            if not card["image"].startswith(("https://", "http://")):
                card["image"] = ""
            try:
                metas = await nostr_store._ws_query(port, [{"authors": [ref["pubkey"]], "kinds": [0], "limit": 1}], timeout=3.0)
                if metas:
                    prof = json.loads(metas[0].get("content") or "{}")
                    if isinstance(prof, dict):
                        card["author"] = (prof.get("display_name") or prof.get("name") or "").strip()[:80]
            except Exception:
                pass
    except Exception as e:
        logger.debug("[article-share] card read failed for %s: %s", key, e)
        return None
    _cache[key] = (now + _CARD_TTL, card)
    if len(_cache) > 2000:
        _cache.clear()
    return card


def slug(title: str, limit: int = 40) -> str:
    """A title as the short, readable part of an article's link: the MAIN title (before a ':', '|' or dash -- a
    subtitle only makes the link longer), lowercase ASCII words joined by '-', cut at a word boundary within `limit`. MUST match static/js/client/app.js `_articleSlug` character for character (the client mints
    the link, this resolves it); tests/test_article_vanity_link.py runs both on the same titles."""
    main = re.split(r"\s*[:|\u2014\u2013]\s*|\s+-\s+", title or "", maxsplit=1)[0]
    words = re.findall(r"[a-z0-9]+", main.lower()) or re.findall(r"[a-z0-9]+", (title or "").lower())
    out = ""
    for w in words:
        nxt = (out + "-" + w) if out else w
        if len(nxt) > limit:
            break
        out = nxt
    return out or (words[0][:limit] if words else "")


async def article_by_name(port: int, pubkey: str, name: str) -> dict | None:
    """`/@<who>/<name>` -> {"pubkey", "d"}: the article whose `d` IS `name`, else the newest whose title slug is."""
    from app.services import nostr_store
    evs = await nostr_store._ws_query(port, [{"authors": [pubkey], "kinds": [ARTICLE_KIND], "limit": 300}], timeout=4.0, strict=True)
    evs = sorted((e for e in (evs or []) if e.get("pubkey") == pubkey), key=lambda e: e.get("created_at") or 0, reverse=True)
    for e in evs:
        if _tag(e, "d") == name:
            return {"kind": ARTICLE_KIND, "pubkey": pubkey, "d": name}
    for e in evs:
        if slug(_tag(e, "title")) == name.lower():
            return {"kind": ARTICLE_KIND, "pubkey": pubkey, "d": _tag(e, "d")}
    return None


def og_meta(card: dict, url: str, fallback_image: str = "") -> dict:
    """An article card -> the `meta` dict the client shell renders into <head> (escaped there)."""
    title = card.get("title") or "Article"
    if card.get("author"):
        title = "%s · %s" % (title, card["author"])
    return {"title": title[:120], "description": (card.get("summary") or "A long-form post on Nostr.")[:300],
            "url": url, "image": card.get("image") or fallback_image or "", "type": "article"}
