"""
Music streaming API endpoint.
Proxies WebDAV audio streams with user authentication.
Supports on-the-fly transcoding for bandwidth savings.
"""
import logging
import subprocess
import tempfile
import os
from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
import requests
from requests.auth import HTTPBasicAuth

from app.database import get_db
from app.auth import get_current_user
from app.models import User
from app.services.webdav_music_service import get_user_webdav_config, connect_webdav

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/music", tags=["music"])

# Quality presets: bitrate in kbps
QUALITY_PRESETS = {
    'low': 64,      # ~0.5 MB/min - mobile data saver
    'medium': 128,  # ~1 MB/min - good balance
    'high': 256,    # ~2 MB/min - high quality
}


async def stream_transcoded(full_url: str, config: dict, quality: str):
    """Stream audio with on-the-fly transcoding via ffmpeg."""
    bitrate = QUALITY_PRESETS.get(quality, 128)

    def generate():
        # Start ffmpeg process that reads from URL and outputs MP3
        # Using ffmpeg's ability to read from HTTP with auth
        import base64
        auth_string = f"{config['username']}:{config['password']}"
        auth_b64 = base64.b64encode(auth_string.encode()).decode()
        ffmpeg_cmd = [
            'ffmpeg',
            '-hide_banner',
            '-loglevel', 'error',
            '-headers', f'Authorization: Basic {auth_b64}',
            '-i', full_url,
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
    path: str = Query(..., description="WebDAV file path"),
    quality: Optional[str] = Query(None, description="Quality: low (64k), medium (128k), high (256k), or original"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Stream audio file from WebDAV server with range request support and optional transcoding."""
    config = get_user_webdav_config(current_user.id, db)
    if not config:
        raise HTTPException(status_code=400, detail="WebDAV music not configured")

    if not config.get('url'):
        raise HTTPException(status_code=400, detail="WebDAV URL not configured")

    # Build full URL
    base_url = config['url'].rstrip('/')
    # Handle path - it might be a full path or relative
    if path.startswith('http'):
        full_url = path
    elif path.startswith('/'):
        # Extract base URL (scheme + host)
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        full_url = f"{parsed.scheme}://{parsed.netloc}{path}"
    else:
        full_url = f"{base_url}/{path}"

    logger.debug(f"Streaming from: {full_url}, quality: {quality}")

    # If quality is specified (not original), use transcoding
    if quality and quality in QUALITY_PRESETS:
        return await stream_transcoded(full_url, config, quality)

    # Determine content type from extension
    ext = path.split('.')[-1].lower() if '.' in path else 'mp3'
    content_types = {
        'mp3': 'audio/mpeg',
        'flac': 'audio/flac',
        'ogg': 'audio/ogg',
        'wav': 'audio/wav',
        'm4a': 'audio/mp4',
        'aac': 'audio/aac',
        'opus': 'audio/opus',
        'wma': 'audio/x-ms-wma'
    }
    content_type = content_types.get(ext, 'audio/mpeg')

    # Check for Range header (required for iOS Safari and seeking)
    range_header = request.headers.get('range')
    logger.info(f"Stream request - Range header: {range_header}")

    # First, get the file size with a HEAD request
    try:
        head_resp = requests.head(
            full_url,
            auth=HTTPBasicAuth(config['username'], config['password']),
            timeout=10
        )
        file_size = int(head_resp.headers.get('content-length', 0))
        logger.info(f"HEAD response - status: {head_resp.status_code}, content-length: {file_size}")
    except Exception as e:
        logger.warning(f"HEAD request failed: {e}, will stream without size")
        file_size = 0

    if range_header and file_size > 0:
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
                    headers = {'Range': f'bytes={start}-{end}'}
                    with requests.get(
                        full_url,
                        auth=HTTPBasicAuth(config['username'], config['password']),
                        headers=headers,
                        stream=True,
                        timeout=(10, 300)  # 10s connect, 5min read - tolerant of slow connections
                    ) as r:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                yield chunk
                except requests.RequestException as e:
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
            with requests.get(
                full_url,
                auth=HTTPBasicAuth(config['username'], config['password']),
                stream=True,
                timeout=(10, 300)  # 10s connect, 5min read - tolerant of slow connections
            ) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
        except requests.RequestException as e:
            logger.error(f"Stream error: {e}")
            raise

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache"
    }
    if file_size > 0:
        headers["Content-Length"] = str(file_size)

    return StreamingResponse(
        generate(),
        media_type=content_type,
        headers=headers
    )


class WebDAVTestRequest(BaseModel):
    url: str
    username: str
    password: Optional[str] = None
    use_stored_password: bool = False


@router.post("/test")
async def test_connection(
    request: WebDAVTestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Test WebDAV connection with provided or stored credentials."""
    url = request.url
    username = request.username
    password = request.password

    # If using stored password, get it from the database
    if request.use_stored_password:
        config = get_user_webdav_config(current_user.id, db)
        if config and config.get('password'):
            password = config['password']

    if not url:
        return {"success": False, "message": "WebDAV URL is required"}

    if not username:
        return {"success": False, "message": "Username is required"}

    if not password:
        return {"success": False, "message": "Password is required"}

    success = connect_webdav(url, username, password)
    return {
        "success": success,
        "message": "Connected successfully" if success else "Connection failed - check URL and credentials"
    }
