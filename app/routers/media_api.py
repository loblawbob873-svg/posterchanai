"""Generic media-processing API (compress / clip / convert / meme / dildo / poo / cum / blood / bullethole / fire / gay / blacked) for the bots.

Identity-agnostic: these are pure byte transforms, so this endpoint only
authenticates the caller (API key or JWT, reusing the image API's auth) — it does
not run as a specific user. Shared by the Matrix, Misskey and Pleroma listener
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
from app.services import effects_service, media_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/media", tags=["media"])


class MediaItem(BaseModel):
    filename: str
    data: str  # base64-encoded file bytes
    content_type: Optional[str] = ""


class MediaProcessRequest(BaseModel):
    command: str
    arg: Optional[str] = ""
    media: List[MediaItem] = []


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


@router.post("/process")
async def process_media(
    req: MediaProcessRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth),
):
    """Run a compress/clip/convert/meme/dildo/poo/cum/blood/bullethole/fire/gay/blacked/kosher/barked operation on the supplied attachments."""
    command = (req.command or "").strip().lower()
    if command not in ("compress", "clip", "convert", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "gay", "blacked", "kosher", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug", "feltedtables"):
        return {"error": f"unsupported command '{command}'"}

    # Trailing subcommands on an effect: <effect> [zoom|shake] [meme <text>]
    # (e.g. `dildo zoom meme top text`). Strip them here; apply motion then
    # caption to the produced files after dispatch.
    arg = req.arg or ""
    motion = None
    trippy = False
    meme_text = None
    if command not in ("compress", "clip", "convert"):
        _toks = arg.split()
        _low = [t.lower() for t in _toks]
        if command not in ("meme", "thug") and "meme" in _low:
            _i = _low.index("meme")
            meme_text = " ".join(_toks[_i + 1:]).strip()
            _toks, _low = _toks[:_i], _low[:_i]
        # Trailing motion cluster (cap 2): one geometry motion + the `trippy`
        # colour pass, either order, at the very END — so a caption word like
        # "trippy" mid-text is never mistaken for a motion.
        _motions = ("zoom", "shake", "medshake", "beginshake", "pulse", "trippy")
        for _ in range(2):
            if not _low or _low[-1] not in _motions:
                break
            _t = _low.pop()
            _toks.pop()
            if _t == "trippy":
                trippy = True
            elif motion is None:
                motion = _t
        arg = " ".join(_toks)

    attachments = []
    for item in (req.media or []):
        try:
            attachments.append((item.filename, base64.b64decode(item.data), item.content_type or ""))
        except Exception as e:
            logger.warning(f"[MEDIA-API] bad media item {item.filename}: {e}")
    if not attachments:
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
        elif command == "dildo":
            outputs, summary = await asyncio.to_thread(effects_service.dildo_attachments, attachments)
        elif command == "poo":
            outputs, summary = await asyncio.to_thread(effects_service.poo_attachments, attachments)
        elif command == "cum":
            outputs, summary = await asyncio.to_thread(effects_service.cum_attachments, attachments)
        elif command == "blood":
            outputs, summary = await asyncio.to_thread(effects_service.blood_attachments, attachments)
        elif command == "bullethole":
            outputs, summary = await asyncio.to_thread(effects_service.bullethole_attachments, attachments)
        elif command == "fire":
            outputs, summary = await asyncio.to_thread(effects_service.fire_attachments, attachments)
        elif command == "gay":
            outputs, summary = await asyncio.to_thread(effects_service.gay_attachments, attachments)
        elif command == "blacked":
            outputs, summary = await asyncio.to_thread(effects_service.blacked_attachments, attachments)
        elif command == "kosher":
            outputs, summary = await asyncio.to_thread(effects_service.kosher_attachments, attachments)
        elif command == "barked":
            outputs, summary = await asyncio.to_thread(effects_service.barked_attachments, attachments)
        elif command == "hava":
            outputs, summary = await asyncio.to_thread(effects_service.hava_attachments, attachments)
        elif command == "indian":
            outputs, summary = await asyncio.to_thread(effects_service.indian_attachments, attachments)
        elif command == "yakety":
            outputs, summary = await asyncio.to_thread(effects_service.yakety_attachments, attachments)
        elif command == "yamete":
            outputs, summary = await asyncio.to_thread(effects_service.yamete_attachments, attachments)
        elif command == "curb":
            outputs, summary = await asyncio.to_thread(effects_service.curb_attachments, attachments)
        elif command == "depressing":
            outputs, summary = await asyncio.to_thread(effects_service.depressing_attachments, attachments)
        elif command == "fahh":
            outputs, summary = await asyncio.to_thread(effects_service.fahh_attachments, attachments)
        elif command == "helpme":
            outputs, summary = await asyncio.to_thread(effects_service.helpme_attachments, attachments)
        elif command == "gong":
            outputs, summary = await asyncio.to_thread(effects_service.gong_attachments, attachments)
        elif command == "fbi":
            outputs, summary = await asyncio.to_thread(effects_service.fbi_attachments, attachments)
        elif command == "redeem":
            outputs, summary = await asyncio.to_thread(effects_service.redeem_attachments, attachments)
        elif command == "gigity":
            outputs, summary = await asyncio.to_thread(effects_service.gigity_attachments, attachments)
        elif command == "beavis":
            outputs, summary = await asyncio.to_thread(effects_service.beavis_attachments, attachments)
        elif command == "smell":
            outputs, summary = await asyncio.to_thread(effects_service.smell_attachments, attachments)
        elif command == "hood":
            outputs, summary = await asyncio.to_thread(effects_service.hood_attachments, attachments)
        elif command == "akbar":
            outputs, summary = await asyncio.to_thread(effects_service.akbar_attachments, attachments)
        elif command == "retard":
            outputs, summary = await asyncio.to_thread(effects_service.retard_attachments, attachments)
        elif command == "whoabuddy":
            outputs, summary = await asyncio.to_thread(effects_service.whoabuddy_attachments, attachments)
        elif command == "sopranos":
            outputs, summary = await asyncio.to_thread(effects_service.sopranos_attachments, attachments)
        elif command == "cheers":
            outputs, summary = await asyncio.to_thread(effects_service.cheers_attachments, attachments)
        elif command == "munsters":
            outputs, summary = await asyncio.to_thread(effects_service.munsters_attachments, attachments)
        elif command == "happydays":
            outputs, summary = await asyncio.to_thread(effects_service.happydays_attachments, attachments)
        elif command == "dontwanttowait":
            outputs, summary = await asyncio.to_thread(effects_service.dontwanttowait_attachments, attachments)
        elif command == "strangerthings":
            outputs, summary = await asyncio.to_thread(effects_service.strangerthings_attachments, attachments)
        elif command == "adamsfamily":
            outputs, summary = await asyncio.to_thread(effects_service.adamsfamily_attachments, attachments)
        elif command == "xmen":
            outputs, summary = await asyncio.to_thread(effects_service.xmen_attachments, attachments)
        elif command == "futurama":
            outputs, summary = await asyncio.to_thread(effects_service.futurama_attachments, attachments)
        elif command == "charliesangles":
            outputs, summary = await asyncio.to_thread(effects_service.charliesangles_attachments, attachments)
        elif command == "differentstroke":
            outputs, summary = await asyncio.to_thread(effects_service.differentstroke_attachments, attachments)
        elif command == "seinfeld":
            outputs, summary = await asyncio.to_thread(effects_service.seinfeld_attachments, attachments)
        elif command == "onepiece":
            outputs, summary = await asyncio.to_thread(effects_service.onepiece_attachments, attachments)
        elif command == "overtaken":
            outputs, summary = await asyncio.to_thread(effects_service.overtaken_attachments, attachments)
        elif command == "freebird":
            outputs, summary = await asyncio.to_thread(effects_service.freebird_attachments, attachments)
        elif command == "kanye":
            outputs, summary = await asyncio.to_thread(effects_service.kanye_attachments, attachments)
        elif command == "darkness":
            outputs, summary = await asyncio.to_thread(effects_service.darkness_attachments, attachments)
        elif command == "bike":
            outputs, summary = await asyncio.to_thread(effects_service.bike_attachments, attachments)
        elif command == "jobs":
            outputs, summary = await asyncio.to_thread(effects_service.jobs_attachments, attachments)
        elif command == "ree":
            outputs, summary = await asyncio.to_thread(effects_service.ree_attachments, attachments)
        elif command == "liberal":
            outputs, summary = await asyncio.to_thread(effects_service.liberal_attachments, attachments)
        elif command == "moving":
            outputs, summary = await asyncio.to_thread(effects_service.moving_attachments, attachments)
        elif command == "harlem":
            outputs, summary = await asyncio.to_thread(effects_service.harlem_attachments, attachments)
        elif command == "chimp":
            outputs, summary = await asyncio.to_thread(effects_service.chimp_attachments, attachments)
        elif command == "consider":
            outputs, summary = await asyncio.to_thread(effects_service.consider_attachments, attachments)
        elif command == "clay":
            outputs, summary = await asyncio.to_thread(effects_service.clay_attachments, attachments)
        elif command == "wasteland":
            outputs, summary = await asyncio.to_thread(effects_service.wasteland_attachments, attachments)
        elif command == "mixalot":
            outputs, summary = await asyncio.to_thread(effects_service.mixalot_attachments, attachments)
        elif command == "thug":
            outputs, summary = await asyncio.to_thread(effects_service.thug_attachments, attachments)
        elif command == "feltedtables":
            outputs, summary = await asyncio.to_thread(effects_service.feltedtables_attachments, attachments)
        else:  # clip
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
        if motion and outputs:
            _apply = {
                "zoom": effects_service.apply_zoom,
                "shake": effects_service.apply_shake,
                "medshake": effects_service.apply_medshake,
                "beginshake": effects_service.apply_beginshake,
                "pulse": effects_service.apply_pulse,
            }.get(motion, effects_service.apply_zoom)
            outputs = await asyncio.to_thread(_apply, outputs)
        if trippy and outputs:
            outputs = await asyncio.to_thread(effects_service.apply_trippy, outputs)
        if meme_text and outputs:
            outputs = await asyncio.to_thread(effects_service.apply_meme_text, outputs, meme_text)
        # Shrink oversized effect videos before delivery (same as the command path).
        # Skip the media tools (compress already ran; clip/convert are user-controlled).
        if outputs and command not in ("compress", "clip", "convert"):
            outputs = await asyncio.to_thread(media_service.compress_output_videos, outputs)
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
    so this only authenticates the caller (bot API key). Shared by the Matrix,
    Misskey and Pleroma listeners so they all reuse the backend's single headless
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
    page is empty (dead Nitter instances), where link previews fail. The bot
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

    return {"data": base64.b64encode(png).decode("ascii"), "content_type": "image/png"}


@router.post("/ytdl")
async def fetch_ytdl(
    req: YtdlRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth),
):
    """Download a YouTube/X URL and return the media as base64.

    Identity-agnostic like /process and /screenshot — a pure URL→media transform
    authenticated by the bot API key (not a linked user), so the Matrix, Misskey
    and Pleroma listeners share one yt-dlp path. Audio (MP3) by default; video=true
    fetches MP4 (capped at 1080p). The optional `clip` ("start end") and `compress`
    modifiers post-process the video server-side (clip → compress) so the bot gets
    the trimmed/shrunk result in one round-trip. Cookies/SSL come from the global
    ytdl_* settings.

    Response: {"ok": True, "filename", "mime", "data"(b64)} or {"ok": False, "error"}.
    """
    from app.models import Setting
    from app.services.youtube_service import download_ytdl_bytes
    import os as _os

    _cookies_s = db.query(Setting).filter(Setting.key == "ytdl_cookies_path").first()
    _cookies_path = str(_cookies_s.value).strip() if _cookies_s and _cookies_s.value else None
    if _cookies_path and not _os.path.isfile(_cookies_path):
        _cookies_path = None
    _ssl_s = db.query(Setting).filter(Setting.key == "ytdl_no_ssl_verify").first()
    _no_ssl = str(_ssl_s.value).strip().lower() in ("true", "1", "yes") if _ssl_s and _ssl_s.value else False

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
