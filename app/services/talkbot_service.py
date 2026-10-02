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
# Speech costs ~10x realtime on the GPU and the render queue is shared with every Meme Builder user, so
# the bot is asked for a bounded reply (NOSTR_TALK_MAX_WORDS, default 40, at most 80). This is only the
# backstop if the model rambles -- sized for 80 words, since 220 chars cut a 40-word answer mid-sentence.
MAX_CHARS = 600


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


MAX_FACES = 10


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


def clean_text(text: str, max_words: int = 0) -> str:
    """What gets spoken: no links or nostr references (they would be read out letter by letter), and
    no more than `max_words` -- cut at a sentence end where one exists. The prompt ASKS for a short
    line; a model ignored it (72 words, fever 19:38) and the voice model then dropped 33 of them."""
    t = re.sub(r"https?://\S+|nostr:\S+|\b(?:npub1|nprofile1|note1|nevent1|naddr1)\w+", "", text or "")
    t = re.sub(r"#(\w+)", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    words = t.split(" ")
    if max_words and len(words) > max_words:
        cut = " ".join(words[:max_words])
        ends = [cut.rfind(p) for p in (". ", "! ", "? ")] + [len(cut) - 1 if cut[-1:] in ".!?" else -1]
        end = max(ends)
        t = cut[:end + 1].strip() if end >= len(cut) // 3 else cut.rstrip(",;:- ") + "."
    if len(t) > MAX_CHARS:
        cut = t[:MAX_CHARS]
        t = cut[:max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(" "))].strip() or cut
    return t


# ---- is the speech the line? ---------------------------------------------------------------------
# The voice model fails in two ways that reached real posts: it SAYS FEWER WORDS than it was given
# (10 of 15 lost), and it pads with dead air (19.6 s of silence before the line, one 24.9 s clip).
# So every take is tidied (silence trimmed; long pauses shortened) and then HEARD by a small Whisper
# on the CPU; a take that misses words or runs far too long is made again, and the best is kept.
_SILENCE_AF = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1:"
               "stop_periods=-1:stop_duration=0.6:stop_threshold=-45dB:stop_silence=0.35")
TAKES = 3
MIN_COVERAGE = 0.8
_whisper = None
_whisper_lock = __import__("threading").Lock()


def tidy_silence(wav: bytes) -> bytes:
    """Trim silence at both ends and shorten any pause over 0.6 s to 0.35 s. Speech is untouched."""
    from app.services import media_service
    tmp = tempfile.mkdtemp(prefix="talkbot_")
    try:
        src, dst = os.path.join(tmp, "in.wav"), os.path.join(tmp, "out.wav")
        with open(src, "wb") as f:
            f.write(wav)
        import subprocess
        r = subprocess.run([media_service.resolve_ffmpeg(), "-v", "error", "-y", "-i", src, "-af", _SILENCE_AF, dst],
                           capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 1000:
            return wav                                # a failed tidy never costs the take
        with open(dst, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _words(text: str) -> list:
    # Digits are left out: "1,094" is said "one thousand ninety-four" and could never match.
    return [w for w in re.findall(r"[a-z']+", (text or "").lower().replace("’", "'"))]


def coverage(expected: str, heard: str) -> float:
    """The share of the line's words that were actually said (order-free, so a mishearing of word
    order is not counted as loss)."""
    want = _words(expected)
    if not want:
        return 1.0
    got = set(_words(heard))
    return sum(1 for w in want if w in got) / len(want)


def max_seconds(line: str) -> float:
    return 4.0 + 0.7 * len(_words(line))


def hear(wav: bytes):
    """What a small Whisper hears in `wav`, or None when no Whisper is available (the check is then
    skipped rather than blocking every reply)."""
    global _whisper
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    with _whisper_lock:
        if _whisper is None:
            _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
        tmp = tempfile.mkdtemp(prefix="talkbot_")
        try:
            path = os.path.join(tmp, "take.wav")
            with open(path, "wb") as f:
                f.write(wav)
            segs, _ = _whisper.transcribe(path, language="en")
            return " ".join(s.text.strip() for s in segs)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def wav_seconds(wav: bytes) -> float:
    import wave
    try:
        with wave.open(io.BytesIO(wav)) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


async def speak_checked(db, line: str, voice: bytes, ref_path: str):
    """(wav, where, report): the best of up to TAKES takes of `line`. Raises when none is usable."""
    from app.services import voice_factory
    best, report = None, []
    for take in range(1, TAKES + 1):
        wav, where = await voice_factory.generate_voice(db, line, voice, reference_path=ref_path)
        if not wav:
            continue
        wav = await asyncio.to_thread(tidy_silence, wav)
        secs = wav_seconds(wav)
        heard = await asyncio.to_thread(hear, wav)
        cov = 1.0 if heard is None else coverage(line, heard)
        good = cov >= MIN_COVERAGE and secs <= max_seconds(line)
        report.append({"take": take, "coverage": round(cov, 2), "secs": round(secs, 1), "where": where})
        logger.info("[talkbot] take %d: %.0f%% of the words, %.1fs (limit %.1fs) on %s",
                    take, cov * 100, secs, max_seconds(line), where)
        score = (good, cov, -abs(secs - max_seconds(line) / 2))
        if best is None or score > best[0]:
            best = (score, wav, where)
        if good:
            break
    if best is None or best[0][1] < 0.5:
        raise RuntimeError("the voice model could not say the line")
    return best[1], best[2], report


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


async def render(db, face_sha: str, voice_sha: str, mouth, text: str, max_words: int = 0) -> bytes:
    """The talking clip for `text`, as MP4 bytes. Raises RuntimeError with a readable reason."""
    from app.services.effects_service.talk import TALK_FPS, add_talk
    from app.routers.client import _meme_slot

    line = clean_text(text, max_words)
    if not line:
        raise RuntimeError("nothing to say")
    face = await _read_blob(db, face_sha)
    voice = await _read_blob(db, voice_sha)
    tmp = tempfile.mkdtemp(prefix="talkbot_")
    try:
        # The LOCAL voice path needs the reference as a FILE; given only bytes, voice_factory logged
        # "local failed: no local copy of the reference clip" and sent every render to another node --
        # this node's GPU never spoke a single talking reply.
        ref_path = os.path.join(tmp, "ref.wav")
        with open(ref_path, "wb") as f:
            f.write(voice)
        wav, where, _report = await speak_checked(db, line, voice, ref_path)
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
