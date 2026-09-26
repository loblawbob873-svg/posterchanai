"""Talking replies: a Nostr bot answers with a short line SPOKEN by a face, the Meme Builder's `talk`.

The bot's operator uploads a face picture and a voice clip in Admin → Bots and places the mouth (or
accepts the detected spot). Every reply is then: the reply text → speech in that voice
(`voice_factory.generate_voice`, which owns the GPU lock, the VRAM swap and the node round-robin) →
the face lip-syncing it (`effects_service.talk.add_talk`, CPU, inside the Meme Builder's render queue)
→ an MP4 the bot posts as its reply.

The bot sends only TEXT. Its face, voice and mouth placement are read here, from its own config row,
so a bot's credential can only ever render as that bot.

Assets live on this node's Blossom, owned by the bot's key and marked `keep` — the age sweep
(`blossom_blob_ttl_days`) would otherwise delete a bot's face and voice some weeks after setup, and
the bot would fall back to text replies with nothing to say why.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import tempfile

logger = logging.getLogger(__name__)

MAX_FACE_BYTES = 12 * 1024 * 1024
MAX_VOICE_BYTES = 40 * 1024 * 1024
# A reply is SHORT: speech costs ~10x realtime on the GPU, and the render queue is shared with every
# Meme Builder user. The bot is asked for a few words; this is the backstop if the model rambles.
MAX_CHARS = 220


def token(bot_name: str) -> str:
    """The credential the manager hands ONE bot: good for rendering as that bot and nothing else."""
    from app.auth import SECRET_KEY
    return hmac.new(str(SECRET_KEY).encode(), f"pcai-talkbot-v1:{bot_name}".encode(),
                    hashlib.sha256).hexdigest()


def token_ok(bot_name: str, tok: str) -> bool:
    return bool(bot_name and tok) and hmac.compare_digest(str(tok), token(bot_name))


def _bot_pubkey(nsec: str) -> str:
    from app.services.nostr import nostr_service
    return nostr_service.derive_pubkey(nostr_service.decode_seckey(nsec))


def clean_mouth(m) -> dict | None:
    """A mouth placement from a form: normalised x/y/w, clamped. None means "detect it"."""
    if not isinstance(m, dict):
        try:
            m = json.loads(m) if m else None
        except (TypeError, ValueError):
            return None
        if not isinstance(m, dict):
            return None
    try:
        out = {"x": min(1.0, max(0.0, float(m.get("x", 0.5)))),
               "y": min(1.0, max(0.0, float(m.get("y", 0.62)))),
               "w": min(0.6, max(0.02, float(m.get("w", 0.12)))),
               "angle": max(-45.0, min(45.0, float(m.get("angle") or 0.0))),
               "anime": bool(m.get("anime"))}
    except (TypeError, ValueError):
        return None
    return out


MAX_FACES = 3


def faces_of(cfg: dict) -> list:
    """The bot's faces as [{sha, mouth}] -- up to MAX_FACES, each with ITS OWN mouth placement (the
    mouth sits somewhere different in every picture). One is picked at random for each reply."""
    raw = cfg.get("talk_faces")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            raw = []
    out = []
    for f in raw if isinstance(raw, list) else []:
        if isinstance(f, dict) and re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha") or "")):
            out.append({"sha": f["sha"], "mouth": clean_mouth(f.get("mouth"))})
    return out[:MAX_FACES]


def clean_text(text: str) -> str:
    """What gets spoken: no links or nostr references (they would be read out letter by letter)."""
    t = re.sub(r"https?://\S+|nostr:\S+|\b(?:npub1|nprofile1|note1|nevent1|naddr1)\w+", "", text or "")
    t = re.sub(r"#(\w+)", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > MAX_CHARS:
        cut = t[:MAX_CHARS]
        t = cut[:max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(" "))].strip() or cut
    return t


async def _read_blob(db, sha: str) -> bytes:
    from app.services import blossom_service
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        raise RuntimeError("asset not set")
    meta = blossom_service.get_blob_meta(db, sha)
    if meta is None:
        raise RuntimeError(f"asset {sha[:12]} is not on this node")
    data = await blossom_service.read_full(db, meta)
    if not data:
        raise RuntimeError(f"asset {sha[:12]} could not be read")
    return data


async def save_face(db, nsec: str, data: bytes) -> dict:
    """Store the face; return its hash, url path and where the mouth appears to be."""
    from PIL import Image, ImageOps
    from app.services import blossom_service
    from app.services.effects_service.talk import detect_mouth
    if not data or len(data) > MAX_FACE_BYTES:
        raise ValueError("the picture is empty or too large (12 MB max)")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            fmt = (im.format or "PNG").upper()
            w, h = im.size
    except Exception:
        raise ValueError("that file is not a picture")
    if min(w, h) < 64:
        raise ValueError("the picture is too small to animate (64 px minimum)")
    mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt, "image/png")
    desc = await blossom_service.save_blob(db, _bot_pubkey(nsec), data, mime, keep=True,
                                           filename="talking-face")
    mouth = await asyncio.to_thread(detect_mouth, data)
    return {"sha": desc["sha256"], "mime": mime, "width": w, "height": h, "mouth": mouth}


async def save_voice(db, nsec: str, data: bytes, filename: str) -> dict:
    """Normalise the clip ONCE (the shared voice_reference rules) and store the result."""
    from app.services import blossom_service
    from app.services.voice_reference import normalize_reference
    if not data or len(data) > MAX_VOICE_BYTES:
        raise ValueError("the clip is empty or too large (40 MB max)")
    tmp = tempfile.mkdtemp(prefix="talkbot_voice_")
    try:
        _path, wav, err = await normalize_reference(data, filename or "voice", tmp)
        if err:
            raise ValueError(err)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    seconds = max(0, (len(wav) - 44)) / (24000 * 2)
    if seconds < 2:
        raise ValueError("the clip needs at least a couple of seconds of speech")
    desc = await blossom_service.save_blob(db, _bot_pubkey(nsec), wav, "audio/wav", keep=True,
                                           filename="talking-voice.wav")
    return {"sha": desc["sha256"], "seconds": round(seconds, 1)}


async def render(db, face_sha: str, voice_sha: str, mouth, text: str) -> bytes:
    """The talking clip for `text`, as MP4 bytes. Raises RuntimeError with a readable reason."""
    from app.services import voice_factory
    from app.services.effects_service.talk import TALK_FPS, add_talk
    from app.routers.client import _meme_slot

    line = clean_text(text)
    if not line:
        raise RuntimeError("nothing to say")
    face = await _read_blob(db, face_sha)
    voice = await _read_blob(db, voice_sha)
    wav, where = await voice_factory.generate_voice(db, line, voice)
    if not wav:
        raise RuntimeError("the voice model returned nothing")
    tmp = tempfile.mkdtemp(prefix="talkbot_")
    try:
        path = os.path.join(tmp, "line.wav")
        with open(path, "wb") as f:
            f.write(wav)
        async with _meme_slot():
            clip, _ct = await asyncio.to_thread(add_talk, face, path, TALK_FPS, False, clean_mouth(mouth))
        logger.info("[talkbot] rendered %d chars -> %d bytes (voice on %s)", len(line), len(clip), where)
        return clip
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def bot_config(db, bot_name: str) -> dict:
    from app.models import Bot
    row = db.query(Bot).filter(Bot.name == bot_name).first()
    if row is None:
        raise LookupError("no such bot")
    try:
        cfg = json.loads(row.config or "{}")
    except (TypeError, ValueError):
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}
