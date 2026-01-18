"""
Music streaming API endpoint.
Serves local audio files with user authentication.
Supports on-the-fly transcoding for bandwidth savings.
"""
import logging
import subprocess
import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user
from app.models import User
from app.services.local_music_service import (
    get_user_music_config,
    get_file_path,
    test_directory_access,
    AUDIO_EXTENSIONS
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/music", tags=["music"])

# Quality presets: bitrate in kbps
QUALITY_PRESETS = {
    'low': 64,      # ~0.5 MB/min - mobile data saver
    'medium': 128,  # ~1 MB/min - good balance
    'high': 256,    # ~2 MB/min - high quality
}


async def stream_transcoded(file_path: Path, quality: str):
    """Stream audio with on-the-fly transcoding via ffmpeg."""
    bitrate = QUALITY_PRESETS.get(quality, 128)

    def generate():
        ffmpeg_cmd = [
            'ffmpeg',
            '-hide_banner',
            '-loglevel', 'error',
            '-i', str(file_path),
            '-vn',  # No video
            '-acodec', 'libmp3lame',
            '-b:a', f'{bitrate}k',
            '-f', 'mp3',
            '-'  # Output to stdout
        ]

        try:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=8192
            )

            # Stream output chunks
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                yield chunk

            process.wait()
            if process.returncode != 0:
                stderr = process.stderr.read().decode()
                logger.error(f"ffmpeg error: {stderr}")
        except Exception as e:
            logger.error(f"Transcoding error: {e}")

    return StreamingResponse(
        generate(),
        media_type='audio/mpeg',
        headers={
            "Cache-Control": "no-cache",
            "X-Quality": quality,
            "X-Bitrate": f"{bitrate}kbps"
        }
    )


@router.get("/stream")
async def stream_audio(
    request: Request,
    path: str = Query(..., description="Relative file path within music directory"),
    quality: Optional[str] = Query(None, description="Quality: low (64k), medium (128k), high (256k), or original"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Stream audio file from local directory with range request support and optional transcoding."""
    config = get_user_music_config(current_user.id, db)
    if not config:
        raise HTTPException(status_code=400, detail="Music directory not configured")

    directory = config.get('directory')
    if not directory:
        raise HTTPException(status_code=400, detail="Music directory not set")

    # Get the absolute file path with security check
    file_path = get_file_path(directory, path)
    if not file_path:
        raise HTTPException(status_code=404, detail="File not found")

    logger.debug(f"Streaming from: {file_path}, quality: {quality}")

    # If quality is specified (not original), use transcoding
    if quality and quality in QUALITY_PRESETS:
        return await stream_transcoded(file_path, quality)

    # Determine content type from extension
    ext = file_path.suffix.lower()
    content_types = {
        '.mp3': 'audio/mpeg',
        '.flac': 'audio/flac',
        '.ogg': 'audio/ogg',
        '.wav': 'audio/wav',
        '.m4a': 'audio/mp4',
        '.aac': 'audio/aac',
        '.opus': 'audio/opus',
        '.wma': 'audio/x-ms-wma'
    }
    content_type = content_types.get(ext, 'audio/mpeg')

    # Get file size
    file_size = file_path.stat().st_size

    # Check for Range header (required for iOS Safari and seeking)
    range_header = request.headers.get('range')
    logger.info(f"Stream request - Range header: {range_header}, file size: {file_size}")

    if range_header:
        # Parse range header: "bytes=start-end" or "bytes=start-"
        try:
            range_spec = range_header.replace('bytes=', '')
            if '-' in range_spec:
                parts = range_spec.split('-')
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else file_size - 1
            else:
                start = int(range_spec)
                end = file_size - 1

            # Ensure valid range
            start = max(0, start)
            end = min(end, file_size - 1)
            content_length = end - start + 1

            def generate_range():
                try:
                    with open(file_path, 'rb') as f:
                        f.seek(start)
                        remaining = content_length
                        chunk_size = 8192
                        
                        while remaining > 0:
                            chunk = f.read(min(chunk_size, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            yield chunk
                except Exception as e:
                    logger.error(f"Stream error: {e}")

            return StreamingResponse(
                generate_range(),
                status_code=206,
                media_type=content_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(content_length),
                    "Cache-Control": "no-cache"
                }
            )
        except (ValueError, IndexError) as e:
            logger.warning(f"Invalid range header: {range_header}, error: {e}")

    # Full file streaming (no range request)
    def generate():
        try:
            with open(file_path, 'rb') as f:
                chunk_size = 8192
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except Exception as e:
            logger.error(f"Stream error: {e}")
            raise

    return StreamingResponse(
        generate(),
        media_type=content_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Cache-Control": "no-cache"
        }
    )


class MusicDirectoryTestRequest(BaseModel):
    directory: str
    recursive: bool = True


@router.post("/test-directory")
async def test_music_directory(
    request: MusicDirectoryTestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Test music directory access and count files."""
    result = test_directory_access(request.directory, request.recursive)
    
    if result['success']:
        return {
            "success": True,
            "message": f"✓ {result['message']} ({result['track_count']} tracks found)"
        }
    else:
        return {
            "success": False,
            "error": result['error']
        }
