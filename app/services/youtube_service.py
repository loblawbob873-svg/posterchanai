"""YouTube Service - Fetch transcripts, summarize, and download as MP3"""

import re
import logging
import os
import tempfile
import subprocess
import shutil
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Optional import - service works without it but transcript fetching is disabled
try:
    from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
    YOUTUBE_API_AVAILABLE = True
except ImportError:
    YOUTUBE_API_AVAILABLE = False
    logger.warning("youtube-transcript-api not installed. YouTube summarization disabled.")


def extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from various URL formats"""
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def is_youtube_url(text: str) -> bool:
    """Check if text contains a YouTube URL"""
    return bool(re.search(r'(youtube\.com|youtu\.be)', text))


def get_transcript(video_id: str) -> Optional[str]:
    """Fetch transcript for a YouTube video"""
    if not YOUTUBE_API_AVAILABLE:
        return None
    try:
        # New API requires instance and uses fetch() method
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id)
        # Combine all transcript snippets
        full_text = ' '.join([snippet.text for snippet in transcript.snippets])
        return full_text
    except TranscriptsDisabled:
        logger.warning(f"Transcripts disabled for video {video_id}")
        return None
    except NoTranscriptFound:
        logger.warning(f"No transcript found for video {video_id}")
        return None
    except Exception as e:
        logger.error(f"Error fetching transcript for {video_id}: {e}")
        return None


def extract_youtube_urls(text: str) -> list[str]:
    """Extract all YouTube URLs from text"""
    pattern = r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[a-zA-Z0-9_-]+[^\s]*)'
    return re.findall(pattern, text)


async def summarize_youtube(url: str, chat_service) -> Tuple[bool, str]:
    """
    Fetch transcript and summarize a YouTube video.
    Returns (success, result_message)
    """
    if not YOUTUBE_API_AVAILABLE:
        return False, "YouTube summarization is not available. Install: pip install youtube-transcript-api"

    video_id = extract_video_id(url)
    if not video_id:
        return False, "Could not extract video ID from URL"

    transcript = get_transcript(video_id)
    if not transcript:
        return False, "Could not fetch transcript. The video may not have captions available."

    # Truncate transcript if too long (keep ~10k chars for context)
    if len(transcript) > 10000:
        transcript = transcript[:10000] + "..."

    # Use chat service to summarize
    messages = [
        {"role": "system", "content": "You are a helpful assistant that summarizes video transcripts. Provide a clear, concise summary highlighting the main points, key takeaways, and any important details."},
        {"role": "user", "content": f"Please summarize this YouTube video transcript:\n\n{transcript}"}
    ]

    try:
        summary = await chat_service.chat(messages)
        return True, f"## Video Summary\n\n{summary}"
    except Exception as e:
        logger.error(f"Error summarizing transcript: {e}")
        return False, f"Error summarizing video: {str(e)}"


# ============================================================================
# YouTube Download Functionality (yt-dlp)
# ============================================================================

@dataclass
class DownloadResult:
    """Result of a YouTube download operation."""
    success: bool
    title: Optional[str] = None
    artist: Optional[str] = None
    filename: Optional[str] = None
    local_path: Optional[str] = None
    webdav_path: Optional[str] = None
    error: Optional[str] = None
    duration: Optional[int] = None  # seconds


def check_ytdlp_available() -> bool:
    """Check if yt-dlp is available (either as library or binary)."""
    # Try library first
    try:
        import yt_dlp
        return True
    except ImportError:
        pass

    # Try binary
    return shutil.which('yt-dlp') is not None


def download_as_video(url: str, output_dir: Optional[str] = None, quality: str = "best") -> DownloadResult:
    """
    Download YouTube video (not just audio).

    Args:
        url: YouTube video URL
        output_dir: Directory to save video (default: temp directory)
        quality: Video quality preference (default: "best", options: "best", "worst", "720p", "1080p", etc.)

    Returns:
        DownloadResult with success status and file info
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix='ytdl_video_')

    try:
        import yt_dlp

        # Output template - sanitize filename
        output_template = os.path.join(output_dir, '%(title)s.%(ext)s')

        # Format selection based on quality preference
        if quality == "best":
            format_selector = "bestvideo+bestaudio/best"
        elif quality == "worst":
            format_selector = "worstvideo+worstaudio/worst"
        else:
            # Try to match quality (e.g., "720p", "1080p")
            # Extract numeric height value
            height_str = quality.replace('p', '').replace('P', '').strip()
            try:
                height = int(height_str)
                if height <= 0:
                    raise ValueError("Height must be positive")
                format_selector = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
            except (ValueError, AttributeError):
                # Invalid quality, fall back to best
                logger.warning(f"Invalid quality '{quality}', using 'best'")
                format_selector = "bestvideo+bestaudio/best"

        ydl_opts = {
            'format': format_selector,
            'merge_output_format': 'mp4',  # Merge video and audio into MP4
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'restrictfilenames': True,  # Sanitize filenames
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # Find the downloaded video file
            title = info.get('title', 'Unknown')
            artist = info.get('artist') or info.get('uploader') or info.get('channel')
            duration = info.get('duration')

            # yt-dlp sanitizes the filename, find the actual file
            video_extensions = ('.mp4', '.webm', '.mkv', '.flv', '.avi')
            for f in os.listdir(output_dir):
                if f.endswith(video_extensions):
                    video_path = os.path.join(output_dir, f)
                    # Build a clean filename
                    clean_title = title
                    if artist:
                        clean_filename = f"{artist} - {clean_title}"
                    else:
                        clean_filename = clean_title
                    
                    # Preserve extension from downloaded file
                    ext = os.path.splitext(f)[1]
                    if not clean_filename.endswith(ext):
                        clean_filename = clean_filename + ext

                    return DownloadResult(
                        success=True,
                        title=title,
                        artist=artist,
                        filename=clean_filename,
                        local_path=video_path,
                        duration=duration
                    )

            return DownloadResult(success=False, error="Video file not found after download")

    except ImportError:
        # Fall back to binary
        return _download_video_with_binary(url, output_dir, quality)
    except Exception as e:
        logger.error(f"YouTube video download error: {e}")
        return DownloadResult(success=False, error=str(e))


def _download_video_with_binary(url: str, output_dir: str, quality: str = "best") -> DownloadResult:
    """Download video using yt-dlp binary."""
    try:
        output_template = os.path.join(output_dir, '%(title)s.%(ext)s')

        cmd = ['yt-dlp', '-o', output_template, '--restrict-filenames']
        
        # Add quality/format options
        if quality == "best":
            cmd.extend(['-f', 'bestvideo+bestaudio/best'])
        elif quality == "worst":
            cmd.extend(['-f', 'worstvideo+worstaudio/worst'])
        else:
            # Try quality-specific format
            height_str = quality.replace('p', '').replace('P', '').strip()
            try:
                height = int(height_str)
                if height <= 0:
                    raise ValueError("Height must be positive")
                cmd.extend(['-f', f'bestvideo[height<={height}]+bestaudio/best[height<={height}]'])
            except (ValueError, AttributeError):
                # Invalid quality, fall back to best
                logger.warning(f"Invalid quality '{quality}', using 'best'")
                cmd.extend(['-f', 'bestvideo+bestaudio/best'])
        
        # Merge into MP4
        cmd.extend(['--merge-output-format', 'mp4'])

        cmd.append(url)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800  # 30 min timeout for videos
        )

        if result.returncode != 0:
            return DownloadResult(success=False, error=result.stderr or "Download failed")

        # Find the video file
        for f in os.listdir(output_dir):
            if f.endswith(('.mp4', '.webm', '.mkv', '.flv', '.avi')):
                video_path = os.path.join(output_dir, f)
                # Extract title from filename
                title = os.path.splitext(f)[0].replace('_', ' ')

                return DownloadResult(
                    success=True,
                    title=title,
                    filename=title + os.path.splitext(f)[1],
                    local_path=video_path
                )

        return DownloadResult(success=False, error="Video file not found")

    except subprocess.TimeoutExpired:
        return DownloadResult(success=False, error="Download timed out")
    except Exception as e:
        return DownloadResult(success=False, error=str(e))


def download_as_mp3(url: str, output_dir: Optional[str] = None) -> DownloadResult:
    """
    Download YouTube video and convert to MP3.

    Args:
        url: YouTube video URL
        output_dir: Directory to save MP3 (default: temp directory)

    Returns:
        DownloadResult with success status and file info
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix='ytdl_')

    try:
        import yt_dlp

        # Output template - sanitize filename
        output_template = os.path.join(output_dir, '%(title)s.%(ext)s')

        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'restrictfilenames': True,  # Sanitize filenames
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # Find the downloaded MP3 file
            title = info.get('title', 'Unknown')
            artist = info.get('artist') or info.get('uploader') or info.get('channel')
            duration = info.get('duration')

            # yt-dlp sanitizes the filename, find the actual file
            for f in os.listdir(output_dir):
                if f.endswith('.mp3'):
                    mp3_path = os.path.join(output_dir, f)
                    # Build a clean filename for WebDAV
                    clean_title = title
                    if artist:
                        clean_filename = f"{artist} - {clean_title}"
                    else:
                        clean_filename = clean_title

                    return DownloadResult(
                        success=True,
                        title=title,
                        artist=artist,
                        filename=clean_filename,
                        local_path=mp3_path,
                        duration=duration
                    )

            return DownloadResult(success=False, error="MP3 file not found after download")

    except ImportError:
        # Fall back to binary
        return _download_with_binary(url, output_dir)
    except Exception as e:
        logger.error(f"YouTube download error: {e}")
        return DownloadResult(success=False, error=str(e))


def _download_with_binary(url: str, output_dir: str) -> DownloadResult:
    """Download using yt-dlp binary."""
    try:
        output_template = os.path.join(output_dir, '%(title)s.%(ext)s')

        result = subprocess.run([
            'yt-dlp',
            '-x',  # Extract audio
            '--audio-format', 'mp3',
            '--audio-quality', '192K',
            '-o', output_template,
            '--restrict-filenames',
            url
        ], capture_output=True, text=True, timeout=600)  # 10 min timeout

        if result.returncode != 0:
            return DownloadResult(success=False, error=result.stderr or "Download failed")

        # Find the MP3 file
        for f in os.listdir(output_dir):
            if f.endswith('.mp3'):
                mp3_path = os.path.join(output_dir, f)
                # Extract title from filename
                title = os.path.splitext(f)[0].replace('_', ' ')

                return DownloadResult(
                    success=True,
                    title=title,
                    filename=title,
                    local_path=mp3_path
                )

        return DownloadResult(success=False, error="MP3 file not found")

    except subprocess.TimeoutExpired:
        return DownloadResult(success=False, error="Download timed out")
    except Exception as e:
        return DownloadResult(success=False, error=str(e))


async def download_and_upload_to_webdav(
    url: str,
    user_id: int,
    db,
    subfolder: str = "YouTube"
) -> DownloadResult:
    """
    Download YouTube video as MP3 and upload to user's WebDAV music folder.

    Args:
        url: YouTube video URL
        user_id: User ID for WebDAV config lookup
        db: Database session
        subfolder: Subfolder within music library (default: "YouTube")

    Returns:
        DownloadResult with success status and WebDAV path
    """
    from app.services.webdav_music_service import get_user_webdav_config, upload_to_webdav

    # Check WebDAV config
    config = get_user_webdav_config(user_id, db)
    if not config or not config.get('url'):
        return DownloadResult(
            success=False,
            error="WebDAV music not configured. Go to Settings > Music to set up your WebDAV music folder."
        )

    # Check yt-dlp available
    if not check_ytdlp_available():
        return DownloadResult(
            success=False,
            error="yt-dlp not installed. Install with: pip install yt-dlp"
        )

    # Create temp directory for download
    temp_dir = tempfile.mkdtemp(prefix='ytdl_')

    try:
        # Download
        logger.info(f"Downloading YouTube video: {url}")
        result = download_as_mp3(url, temp_dir)

        if not result.success:
            return result

        # Upload to WebDAV
        logger.info(f"Uploading to WebDAV: {result.filename}")
        upload_result = upload_to_webdav(
            url=config['url'],
            username=config['username'],
            password=config['password'],
            local_file_path=result.local_path,
            remote_filename=result.filename,
            subfolder=subfolder
        )

        if upload_result['success']:
            result.webdav_path = upload_result['path']
            logger.info(f"Successfully uploaded: {result.webdav_path}")
        else:
            result.success = False
            result.error = f"Upload failed: {upload_result.get('error', 'Unknown error')}"

        return result

    finally:
        # Cleanup temp directory
        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"Failed to cleanup temp dir: {e}")


async def download_video_and_upload_to_webdav(
    url: str,
    user_id: int,
    db,
    subfolder: str = "YouTube Videos",
    quality: str = "best"
) -> DownloadResult:
    """
    Download YouTube video (not just audio) and upload to user's WebDAV folder.

    Args:
        url: YouTube video URL
        user_id: User ID for WebDAV config lookup
        db: Database session
        subfolder: Subfolder within WebDAV (default: "YouTube Videos")
        quality: Video quality preference (default: "best")

    Returns:
        DownloadResult with success status and WebDAV path
    """
    from app.services.webdav_music_service import get_user_webdav_config

    # Check WebDAV config
    config = get_user_webdav_config(user_id, db)
    if not config or not config.get('url'):
        return DownloadResult(
            success=False,
            error="WebDAV not configured. Go to Settings > Music to set up your WebDAV folder."
        )

    # Check yt-dlp available
    if not check_ytdlp_available():
        return DownloadResult(
            success=False,
            error="yt-dlp not installed. Install with: pip install yt-dlp"
        )

    # Create temp directory for download
    temp_dir = tempfile.mkdtemp(prefix='ytdl_video_')

    try:
        # Download video
        logger.info(f"Downloading YouTube video: {url}")
        result = download_as_video(url, temp_dir, quality)

        if not result.success:
            return result

        # Upload to WebDAV (use generic upload function)
        logger.info(f"Uploading video to WebDAV: {result.filename}")
        upload_result = upload_video_to_webdav(
            url=config['url'],
            username=config['username'],
            password=config['password'],
            local_file_path=result.local_path,
            remote_filename=result.filename,
            subfolder=subfolder
        )

        if upload_result['success']:
            result.webdav_path = upload_result['path']
            logger.info(f"Successfully uploaded: {result.webdav_path}")
        else:
            result.success = False
            result.error = f"Upload failed: {upload_result.get('error', 'Unknown error')}"

        return result

    finally:
        # Cleanup temp directory
        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"Failed to cleanup temp dir: {e}")


def upload_video_to_webdav(url: str, username: str, password: str,
                          local_file_path: str, remote_filename: str,
                          subfolder: str = "Downloads") -> Dict[str, Any]:
    """
    Upload a video file to WebDAV (similar to upload_to_webdav but for videos).

    Args:
        url: Base WebDAV URL
        username: WebDAV username
        password: WebDAV password
        local_file_path: Path to local file to upload
        remote_filename: Filename to use on remote
        subfolder: Subfolder within WebDAV (default: "Downloads")

    Returns:
        Dict with 'success', 'path', and optionally 'error'
    """
    import os
    from urllib.parse import urlparse, quote
    import requests
    from requests.auth import HTTPBasicAuth

    try:
        # Ensure subfolder exists (create if needed)
        base_url = url.rstrip('/')
        folder_url = f"{base_url}/{subfolder}"

        # Try MKCOL to create folder (ignore if exists)
        try:
            mkcol_resp = requests.request(
                'MKCOL', folder_url,
                auth=HTTPBasicAuth(username, password),
                timeout=30
            )
            # 201 = created, 405 = already exists (both are OK)
            if mkcol_resp.status_code not in (201, 405, 301):
                logger.warning(f"MKCOL response: {mkcol_resp.status_code}")
        except Exception as e:
            logger.warning(f"MKCOL failed (folder may exist): {e}")

        # Build full remote path - sanitize filename but preserve extension
        video_extensions = ('.mp4', '.webm', '.mkv', '.flv', '.avi')
        base_name, ext = os.path.splitext(remote_filename)
        # Sanitize base name only
        safe_base = "".join(c for c in base_name if c.isalnum() or c in ' -_.').strip()
        # Preserve original extension if valid, otherwise default to .mp4
        if ext.lower() in video_extensions:
            safe_filename = safe_base + ext
        else:
            safe_filename = safe_base + '.mp4'
        remote_path = f"{folder_url}/{quote(safe_filename, safe='')}"

        # Read local file
        if not os.path.exists(local_file_path):
            return {'success': False, 'error': f"Local file not found: {local_file_path}"}

        with open(local_file_path, 'rb') as f:
            file_data = f.read()

        # Determine content type based on extension
        ext = os.path.splitext(safe_filename)[1].lower()
        content_types = {
            '.mp4': 'video/mp4',
            '.webm': 'video/webm',
            '.mkv': 'video/x-matroska',
            '.flv': 'video/x-flv',
            '.avi': 'video/x-msvideo'
        }
        content_type = content_types.get(ext, 'video/mp4')

        # Upload via PUT
        headers = {
            'Content-Type': content_type,
            'Content-Length': str(len(file_data))
        }

        resp = requests.put(
            remote_path,
            auth=HTTPBasicAuth(username, password),
            headers=headers,
            data=file_data,
            timeout=600  # 10 min timeout for large video files
        )

        if resp.status_code in (200, 201, 204):
            # Build the path relative to base
            parsed = urlparse(url)
            relative_path = f"{parsed.path.rstrip('/')}/{subfolder}/{safe_filename}"
            logger.info(f"Successfully uploaded video to WebDAV: {relative_path}")
            return {'success': True, 'path': relative_path, 'filename': safe_filename}
        else:
            logger.error(f"WebDAV upload failed: {resp.status_code} - {resp.text[:200]}")
            return {'success': False, 'error': f"Upload failed: HTTP {resp.status_code}"}

    except Exception as e:
        logger.error(f"WebDAV video upload error: {e}")
        return {'success': False, 'error': str(e)}


def format_download_result(result: DownloadResult) -> str:
    """Format download result for display."""
    if not result.success:
        return f"## ❌ Download Failed\n\n{result.error}"

    lines = ["## ✅ Download Complete\n"]

    if result.title:
        lines.append(f"**Title:** {result.title}")
    if result.artist:
        lines.append(f"**Artist:** {result.artist}")
    if result.duration:
        mins = result.duration // 60
        secs = result.duration % 60
        lines.append(f"**Duration:** {mins}:{secs:02d}")

    if result.webdav_path:
        lines.append(f"\n**Saved to:** `{result.webdav_path}`")
        # Add play button
        lines.append(f"\n[▶ Browse Downloads](cmd:music browse YouTube)")

    return "\n".join(lines)
