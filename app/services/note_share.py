"""Link previews for shared POSTS and PEOPLE: `poster.place/<note1|nevent1|npub1|nprofile1…>`.

"our links do not embed like Ditto's do in Discord" (2026-10-30): a ditto.pub/nevent1… link unfurls into a card --
site name, "<name> on Ditto", the post's text and the author's picture -- while the same post at
poster.place/nevent1… unfurled into nothing, because the `/{entity}` route served the bare shell for every entity but
an article. Same shape as article_share / git_share / profile_share: the event (and its author's kind 0) is read off
THIS node's relay and becomes `og:*` in <head>. Best-effort and local only -- never a fetch from a relay hint in the
URL (a link anyone can mint must not make this server dial an address of their choosing). Anything that cannot be
read leaves the page exactly as before.
"""
from __future__ import annotations

import logging
import re
import time

from app.services.article_share import _polymod, _CHARSET

logger = logging.getLogger(__name__)

SITE = "PosterChan"
_CARD_TTL = 300.0
_cache: dict[str, tuple[float, dict | None]] = {}
_IMG = re.compile(r"https://[^\s<>\"']+?\.(?:png|jpe?g|gif|webp|avif)(?:\?[^\s<>\"']*)?", re.I)
_SHOWN_KINDS = (1, 20, 1068, 1111, 30023)          # what a shared post link can usefully preview


def decode(entity: str) -> dict | None:
    """note1/nevent1/npub1/nprofile1 -> {'type', 'id'|'pubkey', 'author'?} (checksum verified), else None."""
    s = (entity or "").strip().lower()
    hrp = s.split("1", 1)[0] if "1" in s else ""
    if hrp not in ("note", "nevent", "npub", "nprofile") or len(s) > 2000:
        return None
    data = s[len(hrp) + 1:]
    try:
        vals = [_CHARSET.index(c) for c in data]
    except ValueError:
        return None
    if len(vals) < 7 or _polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + vals) != 1:
        return None
    acc = bits = 0
    out = bytearray()
    for v in vals[:-6]:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if hrp in ("note", "npub"):
        if len(out) != 32:
            return None
        return {"type": "note", "id": out.hex()} if hrp == "note" else {"type": "profile", "pubkey": out.hex()}
    res: dict = {}
    i = 0
    while i + 2 <= len(out):
        t, ln = out[i], out[i + 1]
        val = bytes(out[i + 2:i + 2 + ln])
        if len(val) != ln:
            return None
        if t == 0 and ln == 32:
            res["id" if hrp == "nevent" else "pubkey"] = val.hex()
        elif t == 2 and ln == 32 and hrp == "nevent":
            res["author"] = val.hex()
        i += 2 + ln
    if hrp == "nevent":
        return {"type": "note", **res} if "id" in res else None
    return {"type": "profile", "pubkey": res["pubkey"]} if "pubkey" in res else None


def _plain(text: str, n: int = 300) -> str:
    """A post's text as one preview line: links to media and nostr: references dropped, whitespace folded."""
    s = _IMG.sub(" ", text or "")
    s = re.sub(r"https?://\S+\.(?:mp4|webm|mov|m3u8|mp3|ogg|wav)(?:\?\S*)?", " ", s, flags=re.I)
    s = re.sub(r"nostr:[a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def _first_image(ev: dict) -> str:
    for t in ev.get("tags") or []:
        if isinstance(t, list) and t and t[0] == "imeta":
            url = next((x[4:] for x in t[1:] if isinstance(x, str) and x.startswith("url ")), "")
            mime = next((x[2:] for x in t[1:] if isinstance(x, str) and x.startswith("m ")), "")
            if url.startswith("https://") and (mime.startswith("image/") or _IMG.fullmatch(url)):
                return url[:2048]
    m = _IMG.search(ev.get("content") or "")
    return m.group(0)[:2048] if m else ""


async def note_card(port: int, ref: dict) -> dict | None:
    """The shareable facts about one post, read off this node's relay; None when it is not here."""
    key = ref.get("id") or ""
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    card = None
    try:
        from app.services import nostr_store
        from app.services.profile_share import profile_card
        # strict: a dead relay must RAISE, not read as "no such post" and be cached for five minutes.
        evs = await nostr_store._ws_query(port, [{"ids": [key], "limit": 1}], timeout=4.0, strict=True)
        ev = next((e for e in (evs or []) if e.get("id") == key and e.get("kind") in _SHOWN_KINDS), None)
        if ev:
            prof = {}
            try:
                prof = await profile_card(port, ev.get("pubkey") or "") or {}
            except Exception:
                prof = {}
            card = {"author": prof.get("name") or "", "avatar": prof.get("picture") or "",
                    "text": _plain(ev.get("content") or ""), "image": _first_image(ev), "pubkey": ev.get("pubkey") or ""}
    except Exception as e:
        logger.debug("[note-share] card read failed for %s: %s", key[:12], e)
        return None
    _cache[key] = (now + _CARD_TTL, card)
    if len(_cache) > 4000:
        _cache.clear()
    return card


def note_meta(card: dict, url: str, fallback_image: str = "") -> dict:
    """A post card -> the shell's `meta`: "<name> on PosterChan", the text, and the post's picture (a big card)
    or the author's (a small one, as Ditto's is)."""
    who = card.get("author") or ("npub…" + (card.get("pubkey") or "")[-6:] if card.get("pubkey") else "Someone")
    big = bool(card.get("image"))
    return {"title": ("%s on %s" % (who, SITE))[:120],
            "description": card.get("text") or ("A post on %s." % SITE),
            "url": url, "image": card.get("image") or card.get("avatar") or fallback_image or "",
            "type": "article", "site": SITE, "card": "summary_large_image" if big else "summary"}
