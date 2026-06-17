"""Nostr bot client — the listener-facing surface, delegating to app.services.nostr.

This exposes the same functions the listeners import via ``_mk.*``
(get_mentions/get_note/get_own_account/send_reply/post_image_to_fediverse/
get_thread_history/get_thread_images/download_image_from_url), but shaped for Nostr.
All crypto/relay/media lives in the shared ``app.services.nostr`` package (importable
the same way the Misskey/Pleroma shims import ``app.services``), so there is no
duplicated protocol code. Media is uploaded to the configured Blossom/NIP-96 host
and the resulting URL embedded in the note content (Nostr's media model).
"""

import os
import re
import sys
import asyncio
import logging
from datetime import datetime, timezone

import requests

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from app.services.nostr import nostr_service as _svc  # noqa: E402
from config import (  # noqa: E402
    NOSTR_NSEC, NOSTR_RELAYS, NOSTR_MEDIA_SERVICE, NOSTR_MEDIA_ENDPOINT,
)

logger = logging.getLogger(__name__)

_SECKEY = _svc.decode_seckey(NOSTR_NSEC) if NOSTR_NSEC else None
_PUBKEY = _svc.derive_pubkey(_SECKEY) if _SECKEY else None
_RELAYS = _svc.relay.normalize_relays(NOSTR_RELAYS) or _svc.DEFAULT_RELAYS
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
        from app.services.misskey_service import _detect_mime
        mime, _ = _detect_mime(data)
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
    """Resolve a pubkey to {username, host, avatarUrl} via its kind-0 profile (cached).
    Network — call only when the richer identity is actually needed (own account, effect
    outro brand), not for every mention in the poll."""
    if pubkey_hex in _meta_cache:
        return _meta_cache[pubkey_hex]
    meta = {}
    try:
        meta = _run(_svc.get_metadata(pubkey_hex, _RELAYS)) or {}
    except Exception:
        meta = {}
    user = {
        "username": meta.get("name") or meta.get("display_name") or _short_npub(pubkey_hex),
        "host": None,  # Nostr has no instance host; handles are npubs/NIP-05
        "avatarUrl": meta.get("picture"),
        "pubkey": pubkey_hex,
    }
    _meta_cache[pubkey_hex] = user
    return user


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
    return [n for n in notes if n]


def get_note(note_id):
    try:
        ev = _run(_svc.fetch_event(_RELAYS, note_id))
    except Exception as e:
        logger.warning(f"[nostr] get_note failed: {e}")
        return None
    return _shape_note(ev) if ev else None


_QUOTE_RE = re.compile(r"(?:nostr:)?((?:nevent1|note1)[023456789acdefghjklmnpqrstuvwxyz]+)", re.IGNORECASE)


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


def download_image_from_url(url, timeout=30):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; PosterChanBot/1.0)"}
        r = requests.get(url, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r.content
    except requests.exceptions.RequestException as e:
        logger.warning(f"[nostr] download {url[:80]} failed: {e}")
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


def post_image_to_fediverse(text, image_bytes=None, audio_bytes=None, video_bytes=None):
    if not _SECKEY:
        print("ERROR: NOSTR_NSEC not configured; cannot post.")
        return
    media = _to_media_list(image_bytes, video_bytes, audio_bytes)
    try:
        _run(_svc.post_note(_SECKEY, _RELAYS, text or "", media_list=media, media_cfg=_MEDIA_CFG))
        print("[nostr] posted note")
    except Exception as e:
        print(f"[nostr] post failed: {e}")
