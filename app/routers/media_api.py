"""Generic media-processing API (compress / clip / convert / meme / dildo / poo / cum / blood / bullethole / fire / gay / blacked) for the bots.

Identity-agnostic: these are pure byte transforms, so this endpoint only
authenticates the caller (API key or JWT, reusing the image API's auth) — it does
not run as a specific user. Shared by the Pleroma listener
bots so they all reuse one HW-accelerated ffmpeg/Pillow path instead of each
reimplementing it: the byte transforms live in `app/services/media_service.py`,
the creative effects (meme/dildo/poo/cum/blood/bullethole/fire/gay/blacked) in `app/services/effects_service.py`.

Request:  {"command": "compress|clip|convert|meme|dildo|poo|cum|blood|bullethole|fire|gay|blacked", "arg": "", "media": [{filename, data(b64), content_type}]}
Response: {"summary": str, "files": [{filename, data(b64), content_type}]}  — or {"error": str}
"""
import asyncio
import base64
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.image_api import get_image_auth
from app.services import effects_service, media_service, settings_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/media", tags=["media"])


class MediaItem(BaseModel):
    filename: str
    data: str  # base64-encoded file bytes
    content_type: Optional[str] = ""


_MEDIA_TOOLS = ("compress", "clip", "convert")
# Effects this endpoint runs by calling `effects_service.<name>_attachments(attachments)` directly.
_DIRECT_EFFECTS = frozenset({
    "dildo", "poo", "cum", "blood", "bullethole", "fire", "nakedman", "glow", "gay", "blacked", "kosher",
    "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong",
    "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "heat", "whoabuddy", "diarrhea",
    "seth", "robocop", "titan", "terminator", "reze", "vibe", "rebecca", "makima", "feliz", "horse",
    "knightrider", "hugebitch", "sleepwell", "prayer", "sopranos", "cheers", "munsters", "happydays",
    "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles",
    "differentstroke", "seinfeld", "jerry", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike",
    "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "uwu", "nami", "wasteland",
    "mixalot", "nonematters", "thug", "feltedtables",
})


class MediaProcessRequest(BaseModel):
    command: str
    arg: Optional[str] = ""
    media: List[MediaItem] = []
    # Optional branding identity for the outro end-card: the fediverse poster who
    # invoked the effect. `brand_handle` is shown as @handle (already in fediverse
    # form — bare for local users, user@host for remote); `brand_avatar` is their
    # profile picture. Absent → the static "made with PosterChanAI" card.
    brand_handle: Optional[str] = None
    brand_avatar: Optional[MediaItem] = None


class ScreenshotRequest(BaseModel):
    url: str


class YtdlRequest(BaseModel):
    url: str
    video: Optional[bool] = False
    clip: Optional[str] = None      # "start end" (e.g. "0:10 0:30"); video only
    compress: Optional[bool] = False  # compress the (clipped) video; video only


class PostCardRequest(BaseModel):
    handle: str
    text: Optional[str] = ""
    display_name: Optional[str] = ""
    timestamp: Optional[str] = ""
    media: Optional[MediaItem] = None   # pre-fetched tweet media, embedded as a data: URI
    avatar: Optional[MediaItem] = None  # pre-fetched profile picture, embedded as a data: URI


def _brand_videos(outputs: list, db: Session,
                  username: Optional[str] = None, avatar_bytes: Optional[bytes] = None) -> list:
    """Append the PosterChanAI end-card to each video output. When `username` is given (the
    fediverse poster who invoked the effect), it's a per-user card with their @handle + avatar;
    otherwise the STATIC "made with PosterChanAI" card. Gated by `effect_outro_enabled`;
    best-effort (failure leaves the file untouched)."""
    try:
        s = settings_store.get("effect_outro_enabled")
        if s is not None and str(s).strip().lower() in ("false", "0", "no", "off"):
            return outputs
    except Exception:
        pass
    out = []
    for f in (outputs or []):
        try:
            if isinstance(f, dict) and f.get("content_type") == "video/mp4" and f.get("data"):
                f = {**f, "data": media_service.append_outro(
                    f["data"], f.get("filename", "video.mp4"),
                    username=username, avatar_bytes=avatar_bytes)}
        except Exception as e:
            logger.warning(f"[MEDIA-API] outro branding failed: {e}")
        out.append(f)
    return out


@router.post("/process")
async def process_media(
    req: MediaProcessRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth),
):
    """Run a compress/clip/convert/meme/dildo/poo/cum/blood/bullethole/fire/gay/blacked/kosher/barked operation on the supplied attachments."""
    command = (req.command or "").strip().lower()
    # THE ALLOWLIST IS THE COMMAND SERVICE'S OWN SETS, not a copy. A hand-typed tuple of ~90 names lived here and
    # drifted: 14 effects the app supports (carl, collage, gura, shrug, soyjack, woodchipper, …) answered the
    # fediverse bots "unsupported command" (code review, 2026-10-07).
    from app.services.command_service import CommandService
    if command not in _MEDIA_TOOLS and command not in CommandService.MOTION_EFFECTS and command not in CommandService.ANIMATED_EFFECTS:
        return {"error": f"unsupported command '{command}'"}

    # Trailing subcommands on an effect: <effect> [zoom|shake] [meme <text>]
    # (e.g. `dildo zoom meme top text`). Strip them here; apply motion then
    # caption to the produced files after dispatch.
    arg = req.arg or ""
    mods = []
    meme_text = None
    character = None
    if command not in ("compress", "clip", "convert"):
        _toks = arg.split()
        _low = [t.lower() for t in _toks]
        # `char <name>` (anywhere) → overlay a character; parse before `meme` (which eats to end).
        if "char" in _low:
            _ci = _low.index("char")
            if _ci + 1 < len(_toks) and effects_service._character_path(_toks[_ci + 1]):
                character = _toks[_ci + 1].lower()
                _toks = _toks[:_ci] + _toks[_ci + 2:]
                _low = [t.lower() for t in _toks]
        if command not in ("meme", "thug") and "meme" in _low:
            _i = _low.index("meme")
            meme_text = " ".join(_toks[_i + 1:]).strip()
            _toks, _low = _toks[:_i], _low[:_i]
        # Trailing modifier cluster (one movement + glow + trippy), in any order, at the very
        # END — so a caption word like "trippy" mid-text is never mistaken for a modifier. The
        # cap is looser than the 3 that can validly combine so check_motion_combo can SEE (and
        # refuse) a bad one. Rules live in CommandService, so this path (fediverse bots)
        # accepts exactly what the web UI and Telegram do.
        for _ in range(len(CommandService.MOTION_ARGS)):
            if not _low or _low[-1] not in CommandService.MOTION_ARGS:
                break
            mods.insert(0, _low.pop())
            _toks.pop()
        mods, _combo_err = CommandService.check_motion_combo(command, mods)
        if _combo_err:
            return {"error": _combo_err}
        arg = " ".join(_toks)

    attachments = []
    for item in (req.media or []):
        try:
            attachments.append((item.filename, base64.b64decode(item.data), item.content_type or ""))
        except Exception as e:
            logger.warning(f"[MEDIA-API] bad media item {item.filename}: {e}")
    if not attachments:
        # Text-only glow → render a glowing neon text card (no image needed). Uses the
        # ORIGINAL req.arg (not the motion-stripped `arg`) so the full text becomes the
        # card, even if it happens to end in a word like "zoom"/"trippy".
        _glow_text = (req.arg or "").strip()
        if command == "glow" and _glow_text:
            png = await asyncio.to_thread(effects_service.render_glow_text_card, _glow_text)
            return {
                "summary": "## ✨ Glow",
                "files": [{
                    "filename": "glow.png",
                    "data": base64.b64encode(png).decode("ascii"),
                    "content_type": "image/png",
                }],
            }
        return {"error": "no media supplied"}

    try:
        if command == "compress":
            outputs, summary = await asyncio.to_thread(media_service.compress_attachments, attachments)
        elif command == "convert":
            outputs, summary = await asyncio.to_thread(media_service.convert_attachments, attachments, req.arg or "")
        elif command == "meme":
            if not arg.strip():
                return {"error": "meme needs caption text, e.g. 'meme top text'"}
            outputs, summary = await asyncio.to_thread(effects_service.meme_attachments, attachments, arg)
        elif command in _DIRECT_EFFECTS:
            # A picture in, the effect out: its own `<name>_attachments`, called exactly as each
            # copied branch here used to call it (so a failure still comes back as its summary).
            outputs, summary = await asyncio.to_thread(getattr(effects_service, f"{command}_attachments"), attachments)
        elif command == "alive":
            from app.services import parallax_service
            outputs, summary = await asyncio.to_thread(parallax_service.alive_attachments, attachments, arg)
        elif command == "mentioned":
            if not effects_service.mentioned_caption(arg):
                return {"error": effects_service.MENTIONED_ASK}
            outputs, summary = await asyncio.to_thread(effects_service.mentioned_attachments, attachments, arg.strip())
        elif command == "clip":
            parts = (req.arg or "").split()
            if len(parts) < 2:
                return {"error": "clip needs <start> <end>, e.g. '0:10 0:30'"}
            start = media_service.parse_timecode(parts[0])
            end = media_service.parse_timecode(parts[1])
            if start is None or end is None:
                return {"error": "could not parse start/end times (use seconds or M:SS / H:MM:SS)"}
            if end <= start:
                return {"error": "end time must be after start time"}
            outputs, summary = await asyncio.to_thread(media_service.clip_attachment, attachments, start, end)
        else:
            # An effect with no branch above runs through the SAME dispatch chat and Telegram use (the raw one:
            # the outro and modifiers are applied below, as for every other effect). It used to fall into the
            # clip branch and answer "clip needs <start> <end>".
            res = await CommandService(db)._execute_command_inner(command, arg, None, None, attachments, None)
            if not (isinstance(res, dict) and res.get("type") == "files" and res.get("files")):
                return {"error": (res or {}).get("content") or f"{command} produced nothing"}
            outputs, summary = res["files"], res.get("content") or ""
        # Modifiers, already ordered + validated by check_motion_combo: the movement builds the
        # frames, then glow/trippy recolour those real frames (keeping the motion).
        for _mod in (mods if outputs else []):
            _apply = CommandService.motion_applier(_mod)
            if _apply:
                outputs = await asyncio.to_thread(_apply, outputs)
        if character and outputs:
            outputs = await asyncio.to_thread(effects_service.apply_character, outputs, character)
        if meme_text and outputs:
            outputs = await asyncio.to_thread(effects_service.apply_meme_text, outputs, meme_text)
        # Shrink oversized effect videos before delivery (same as the command path).
        # Skip the media tools (compress already ran; clip/convert are user-controlled).
        if outputs and command not in ("compress", "clip", "convert"):
            outputs = await asyncio.to_thread(media_service.compress_effect_outputs, outputs)
        # TikTok-style branding end-card. If the caller supplied the fediverse poster's
        # identity (brand_handle/brand_avatar), it's a per-user card with their @handle +
        # avatar; otherwise the STATIC "made with PosterChanAI" card. Gated, best-effort.
        if outputs and command not in ("compress", "clip", "convert"):
            _brand_avatar_bytes = None
            if req.brand_avatar and req.brand_avatar.data:
                try:
                    _brand_avatar_bytes = base64.b64decode(req.brand_avatar.data)
                except Exception:
                    _brand_avatar_bytes = None
            outputs = await asyncio.to_thread(
                _brand_videos, outputs, db,
                (req.brand_handle or "").strip() or None, _brand_avatar_bytes)
    except Exception as e:
        logger.error(f"[MEDIA-API] {command} failed: {e}", exc_info=True)
        return {"error": str(e)}

    return {
        "summary": summary,
        "files": [
            {
                "filename": f.get("filename", "file"),
                "data": base64.b64encode(f["data"]).decode("ascii"),
                "content_type": f.get("content_type", "application/octet-stream"),
            }
            for f in (outputs or [])
        ],
    }


@router.post("/screenshot")
async def capture_screenshot(
    req: ScreenshotRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth),
):
    """Capture a full-page screenshot of a website and return it as a PNG.

    Identity-agnostic like /process — screenshotting is a pure URL→image transform,
    so this only authenticates the caller (bot API key). Shared by the
    Pleroma listener so they all reuse the backend's single headless
    Chrome/Firefox path (`app/services/command_service.py`).

    Response: {"summary": str, "data": b64 PNG, "content_type": "image/png"} on
    success, or {"error": str} if capture failed / no browser is installed.
    """
    from app.services.command_service import CommandService

    url = (req.url or "").strip()
    if not url:
        return {"error": "no url supplied"}

    try:
        result = await CommandService(db).execute_command("screenshot", url)
    except Exception as e:
        logger.error(f"[MEDIA-API] screenshot failed: {e}", exc_info=True)
        return {"error": str(e)}

    # The command returns a `generated_image` shape on success; any other type
    # (e.g. "text") is an error/usage message we surface verbatim.
    if result.get("type") != "generated_image" or not result.get("image"):
        return {"error": result.get("content", "screenshot failed")}

    img = result["image"]
    if isinstance(img, str) and img.startswith("data:image"):
        img = img.split(",", 1)[1]
    img_b64 = img if isinstance(img, str) else base64.b64encode(img).decode("ascii")
    return {
        "summary": result.get("content", ""),
        "data": img_b64,
        "content_type": "image/png",
    }


@router.post("/render-post-card")
async def render_post_card(
    req: PostCardRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth),
):
    """Render a tweet-style "post card" (author + text + media) as a PNG.

    Identity-agnostic like /process and /screenshot. The card is built from the
    structured fields supplied by the caller and screenshotted via the shared
    headless-browser path — so it renders correctly even when the original source
    page is empty, where link previews fail. The bot
    pre-fetches any media and passes the bytes so the server does no outbound
    network (no SSRF surface here).

    Response: {"data": b64 PNG, "content_type": "image/png"} or {"error": str}.
    """
    from app.services.command_service import _render_post_card_png

    if not (req.handle or "").strip() and not (req.text or "").strip():
        return {"error": "nothing to render (handle and text both empty)"}

    media_uri = ""
    if req.media and req.media.data:
        ct = req.media.content_type or "image/jpeg"
        media_uri = f"data:{ct};base64,{req.media.data}"
    avatar_uri = ""
    if req.avatar and req.avatar.data:
        act = req.avatar.content_type or "image/jpeg"
        avatar_uri = f"data:{act};base64,{req.avatar.data}"

    try:
        png = await asyncio.to_thread(
            _render_post_card_png,
            req.display_name or req.handle, req.handle, req.text or "",
            req.timestamp or "", media_uri, avatar_uri,
        )
    except Exception as e:
        logger.error(f"[MEDIA-API] render-post-card failed: {e}", exc_info=True)
        return {"error": str(e)}

    # Screenshots come back as PNG, which is the worst format for a card that is mostly PHOTO —
    # measured here: a card embedding a tweet image is 293 KB as PNG and 59 KB through the shared
    # compressor, and each one is an upload to Pleroma plus a fetch by every viewer. So run it
    # through the SAME media_service.compress_image the rest of the app uses, not a bot-local
    # re-encode.
    #
    # But only KEEP the JPEG when it is a real win. PNG is the right format for the flat text a
    # media-less card is made of: that card is 10 KB as PNG and 6.9 KB as JPEG — 3 KB saved in
    # exchange for ringing artifacts on the tweet text, which is the one thing on a card that has
    # to stay readable. The size ratio tells the two cases apart on its own (20% for photo, 68%
    # for text), so it picks the format instead of a flag someone has to remember to set.
    # Best-effort throughout: a compressor failure must not cost the caller its card.
    ct = "image/png"
    try:
        from app.services import media_service
        smaller = await asyncio.to_thread(media_service.compress_image, png, quality=85)
        if smaller and len(smaller) <= 0.6 * len(png):
            png, ct = smaller, "image/jpeg"
    except Exception as e:
        logger.warning("[MEDIA-API] post-card compression skipped: %s", e)

    return {"data": base64.b64encode(png).decode("ascii"), "content_type": ct}


@router.post("/ytdl")
async def fetch_ytdl(
    req: YtdlRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth),
):
    """Download a YouTube/X URL and return the media as base64.

    Identity-agnostic like /process and /screenshot — a pure URL→media transform
    authenticated by the bot API key (not a linked user), so the Pleroma
    and Pleroma listeners share one yt-dlp path. Audio (MP3) by default; video=true
    fetches MP4 (capped at 1080p). The optional `clip` ("start end") and `compress`
    modifiers post-process the video server-side (clip → compress) so the bot gets
    the trimmed/shrunk result in one round-trip. Cookies/SSL come from the global
    ytdl_* settings.

    Response: {"ok": True, "filename", "mime", "data"(b64)} or {"ok": False, "error"}.
    """
    from app.services.youtube_service import download_ytdl_bytes
    import os as _os

    _cookies_s = settings_store.get("ytdl_cookies_path")
    _cookies_path = str(_cookies_s).strip() if _cookies_s else None
    if _cookies_path and not _os.path.isfile(_cookies_path):
        _cookies_path = None
    _ssl_s = settings_store.get("ytdl_no_ssl_verify")
    _no_ssl = str(_ssl_s).strip().lower() in ("true", "1", "yes") if _ssl_s else False

    # 95 MB keeps files under Cloudflare's 100 MB request-body cap (the real
    # bottleneck for fediverse uploads); reject larger rather than fail downstream.
    result = await asyncio.to_thread(
        download_ytdl_bytes, req.url,
        video=bool(req.video), clip=req.clip, compress=bool(req.compress),
        cookies_path=_cookies_path, no_ssl_verify=_no_ssl,
        max_bytes=95 * 1024 * 1024, quality="1080p",
    )
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "filename": result["filename"],
        "mime": result["mime"],
        "data": base64.b64encode(result["data"]).decode("ascii"),
    }
