"""Generic media-processing API (compress / clip / convert) for the bots.

Identity-agnostic: compress/clip/convert are pure byte transforms, so this
endpoint only authenticates the caller (API key or JWT, reusing the image API's
auth) — it does not run as a specific user. Shared by the Matrix, Misskey and
Pleroma listener bots so they all reuse one HW-accelerated ffmpeg/Pillow path
(`app/services/media_service.py`) instead of each reimplementing it.

Request:  {"command": "compress|clip|convert", "arg": "", "media": [{filename, data(b64), content_type}]}
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
from app.services import media_service

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


@router.post("/process")
async def process_media(
    req: MediaProcessRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth),
):
    """Run a compress/clip/convert operation on the supplied attachments."""
    command = (req.command or "").strip().lower()
    if command not in ("compress", "clip", "convert"):
        return {"error": f"unsupported command '{command}'"}

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
