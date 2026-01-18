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
    from app.models import Setting
    import httpx
    
    config = get_user_music_config(current_user.id, db)
    if not config:
        raise HTTPException(status_code=400, detail="Music directory not configured")

    directory = config.get('directory')
    if not directory:
        raise HTTPException(status_code=400, detail="Music directory not set")
    
    # Check if using storage proxy
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    storage_server_token = db.query(Setting).filter(Setting.key == "storage_server_token").first()
    use_proxy = storage_server_url and storage_server_url.value and storage_server_token
    
    logger.info(f"[MUSIC STREAM] path={path}, directory={directory}, use_proxy={use_proxy}")
    
    if use_proxy:
        # Stream from storage proxy
        logger.info(f"[MUSIC STREAM PROXY] Proxying stream request")
        try:
            # The path parameter already includes the full path from storage server (e.g., "Music/filename.mp3")
            # Don't concatenate with directory
            file_url = f"{storage_server_url.value}/api/files/{path}"
            headers = {"Authorization": f"Bearer {storage_server_token.value}"}
            
            # Forward range header if present
            range_header = request.headers.get('range')
            if range_header:
                headers['Range'] = range_header
            
            logger.info(f"[MUSIC STREAM PROXY] Fetching from: {file_url}")
            
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.get(file_url, headers=headers)
                
                if response.status_code not in (200, 206):
                    logger.error(f"[MUSIC STREAM PROXY] Storage server returned {response.status_code}")
                    raise HTTPException(status_code=response.status_code, detail="Storage server error")
                
                # Determine content type from extension
                ext = path.lower().split('.')[-1]
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
                
                # Build response headers
                response_headers = {
                    "Content-Type": content_type,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache"
                }
                
                # Forward content-range if present (for 206 responses)
                if response.status_code == 206:
                    if 'content-range' in response.headers:
                        response_headers['Content-Range'] = response.headers['content-range']
                    if 'content-length' in response.headers:
                        response_headers['Content-Length'] = response.headers['content-length']
                
                return StreamingResponse(
                    iter([response.content]),
                    status_code=response.status_code,
                    media_type=content_type,
                    headers=response_headers
                )
        except httpx.HTTPError as e:
            logger.error(f"[MUSIC STREAM PROXY] HTTP error: {e}")
            raise HTTPException(status_code=500, detail=f"Storage proxy error: {str(e)}")
        except Exception as e:
            logger.error(f"[MUSIC STREAM PROXY] Error: {e}")
            raise HTTPException(status_code=500, detail=f"Stream error: {str(e)}")

    # Local filesystem streaming (original code)
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
    from app.models import Setting
    from pathlib import Path
    
    directory = request.directory
    original_directory = directory  # Keep original for display
    
    logger.info(f"[MUSIC TEST] Received directory: '{directory}', user: {current_user.username}")
    
    # Check if storage proxy is configured
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    use_proxy = storage_server_url and storage_server_url.value
    
    if use_proxy:
        # For storage proxy, pass the original user path (e.g., "/Music")
        # The storage proxy will handle user-relative paths
        logger.info(f"[MUSIC TEST] Using storage proxy, passing original path: {directory}")
        result = test_directory_access(directory, request.recursive, db=db, user_id=current_user.id)
    else:
        # For local filesystem, resolve to full path
        if directory and directory.startswith('/') and not directory.startswith('//'):
            # Use upload_path (same as File Manager) instead of storage_base_path
            upload_path_setting = db.query(Setting).filter(Setting.key == "upload_path").first()
            upload_path = upload_path_setting.value if upload_path_setting and upload_path_setting.value else "/var/lib/posterchanai"
            
            logger.info(f"[MUSIC TEST] upload_path: {upload_path}")
            
            upload_base = Path(upload_path)
            # If the path doesn't contain the username, treat it as relative to user storage
            if current_user.username not in directory:
                relative_path = directory.lstrip('/')
                directory = str(upload_base / current_user.username / relative_path)
                logger.info(f"[MUSIC TEST] Resolved {original_directory} to {directory}")
            else:
                logger.info(f"[MUSIC TEST] Username found in path, no resolution needed")
        
        logger.info(f"[MUSIC TEST] Testing directory: {directory}")
        result = test_directory_access(directory, request.recursive, db=db, user_id=current_user.id)
    
    logger.info(f"[MUSIC TEST] Test result: {result}\n")
    
    if result['success']:
        return {
            "success": True,
            "message": f"Directory accessible",
            "track_count": result.get('track_count', 0)
        }
    else:
        # Show both original and resolved path in error
        error_msg = result['error']
        if not use_proxy and original_directory != directory:
            error_msg = error_msg.replace(original_directory, f"{original_directory} (resolved to {directory})")
        return {
            "success": False,
            "error": error_msg
        }
