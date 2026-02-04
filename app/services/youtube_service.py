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


# Characters allowed in a URL (strip emojis and other paste garbage)
_URL_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~:/?#[]@!$&'()*+,;=%")


def _sanitize_youtube_url(url: str) -> str:
    """Remove trailing emojis/symbols from pasted URLs so only valid URL chars remain."""
    return "".join(c for c in url if c in _URL_SAFE_CHARS)


def extract_youtube_urls(text: str) -> list[str]:
    """Extract all YouTube URLs from text. Supports www/m subdomains and strips trailing emojis."""
    # Allow www. or m. (mobile) subdomain; capture URL then sanitize to drop emojis
    pattern = r'(https?://(?:www\.|m\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[a-zA-Z0-9_-]+[^\s]*)'
    raw = re.findall(pattern, text)
    return [u for u in (_sanitize_youtube_url(x) for x in raw) if u]


def _sanitize_url(url: str) -> str:
    """Remove trailing emojis/symbols from pasted URLs so only valid URL chars remain."""
    return "".join(c for c in url if c in _URL_SAFE_CHARS)


def extract_download_urls(text: str) -> list[str]:
    """Extract YouTube and X (Twitter) URLs from text for yt-dlp download. Supports ytdl/ytdlp for both."""
    urls = []
    # YouTube (same as extract_youtube_urls)
    urls.extend(extract_youtube_urls(text))
    # X.com / Twitter (status and i/status links; yt-dlp supports these)
    x_pattern = r'(https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/(?:\w+/status/|i/status/)[0-9]+[^\s]*)'
    raw_x = re.findall(x_pattern, text)
    for u in raw_x:
        sanitized = _sanitize_url(u)
        if sanitized and sanitized not in urls:
            urls.append(sanitized)
    return urls


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

# Retry with Android player client when YouTube returns 403 Forbidden (bot detection)
_YOUTUBE_403_EXTRACTOR_ARGS = {'youtube': {'player_client': ['android']}}


def _is_403_error(err_text: str) -> bool:
    """Return True if the error looks like YouTube 403 Forbidden."""
    if not err_text:
        return False
    t = str(err_text).lower()
    return '403' in t or 'forbidden' in t


@dataclass
class DownloadResult:
    """Result of a YouTube download operation."""
    success: bool
    title: Optional[str] = None
    artist: Optional[str] = None
    filename: Optional[str] = None
    local_path: Optional[str] = None
    storage_path: Optional[str] = None
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


def download_as_video(
    url: str,
    output_dir: Optional[str] = None,
    quality: str = "best",
    cookies_path: Optional[str] = None,
    no_ssl_verify: bool = False,
) -> DownloadResult:
    """
    Download YouTube video (not just audio).

    Args:
        url: YouTube video URL
        output_dir: Directory to save video (default: temp directory)
        quality: Video quality preference (default: "best", options: "best", "worst", "720p", "1080p", etc.)
        cookies_path: Optional path to Netscape-format cookies file (helps avoid YouTube 403).
        no_ssl_verify: If True, skip SSL certificate verification (for proxy/firewall hostname mismatch).

    Returns:
        DownloadResult with success status and file info
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix='ytdl_video_')

    try:
        import yt_dlp

        # Output template - sanitize filename
        output_template = os.path.join(output_dir, '%(title)s.%(ext)s')

        # Format selection: use single "best" by default (most compatible; some videos have no separate streams)
        if quality == "best":
            format_selector = "best"
        elif quality == "worst":
            format_selector = "worst"
        else:
            height_str = quality.replace('p', '').replace('P', '').strip()
            try:
                height = int(height_str)
                if height <= 0:
                    raise ValueError("Height must be positive")
                format_selector = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
            except (ValueError, AttributeError):
                logger.warning(f"Invalid quality '{quality}', using 'best'")
                format_selector = "best"

        ydl_opts = {
            'format': format_selector,
            'merge_output_format': 'mp4',
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'restrictfilenames': True,
            'nocheckcertificate': no_ssl_verify,
        }
        if cookies_path and os.path.isfile(cookies_path):
            ydl_opts['cookiefile'] = cookies_path
            logger.info(f"[ytdl] Using cookies file: {cookies_path}")

        def _do_download(opts: dict):
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

        try:
            info = _do_download(ydl_opts)
        except Exception as e:
            if _is_403_error(str(e)):
                logger.info("[ytdl] Got 403, retrying with Android player client")
                ydl_opts = {**ydl_opts, 'extractor_args': _YOUTUBE_403_EXTRACTOR_ARGS}
                try:
                    info = _do_download(ydl_opts)
                except Exception as retry_e:
                    logger.error(f"[ytdl] Retry after 403 failed: {retry_e}", exc_info=True)
                    return DownloadResult(success=False, error=str(retry_e))
            else:
                raise

        # Find the downloaded video file
        title = info.get('title', 'Unknown')
        artist = info.get('artist') or info.get('uploader') or info.get('channel')
        duration = info.get('duration')

        video_extensions = ('.mp4', '.webm', '.mkv', '.flv', '.avi')
        for f in os.listdir(output_dir):
            if f.endswith(video_extensions):
                video_path = os.path.join(output_dir, f)
                clean_title = title
                if artist:
                    clean_filename = f"{artist} - {clean_title}"
                else:
                    clean_filename = clean_title
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

        logger.warning("[ytdl] Video file not found in output_dir after yt-dlp run")
        return DownloadResult(success=False, error="Video file not found after download")

    except ImportError:
        # Fall back to binary
        logger.info("[ytdl] yt_dlp library not available, using yt-dlp binary")
        return _download_video_with_binary(url, output_dir, quality, cookies_path, no_ssl_verify)
    except Exception as e:
        logger.error(f"[ytdl] YouTube video download error: {e}", exc_info=True)
        return DownloadResult(success=False, error=str(e))


def _download_video_with_binary(
    url: str,
    output_dir: str,
    quality: str = "best",
    cookies_path: Optional[str] = None,
    no_ssl_verify: bool = False,
) -> DownloadResult:
    """Download video using yt-dlp binary."""
    try:
        output_template = os.path.join(output_dir, '%(title)s.%(ext)s')

        cmd = ['yt-dlp', '-o', output_template, '--restrict-filenames']
        if no_ssl_verify:
            cmd.append('--no-check-certificate')
        if cookies_path and os.path.isfile(cookies_path):
            cmd.extend(['--cookies', cookies_path])
            logger.info(f"[ytdl] Using cookies file: {cookies_path}")

        # Add quality/format options (single "best" = most compatible)
        if quality == "best":
            cmd.extend(['-f', 'best'])
        elif quality == "worst":
            cmd.extend(['-f', 'worst'])
        else:
            height_str = quality.replace('p', '').replace('P', '').strip()
            try:
                height = int(height_str)
                if height <= 0:
                    raise ValueError("Height must be positive")
                cmd.extend(['-f', f'bestvideo[height<={height}]+bestaudio/best[height<={height}]/best'])
            except (ValueError, AttributeError):
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
            err = result.stderr or result.stdout or "Download failed"
            if _is_403_error(err):
                logger.info("[ytdl] Got 403 from binary, retrying with Android player client")
                retry_cmd = cmd.copy()
                # Insert --extractor-args before URL (last element)
                retry_cmd.insert(-1, 'youtube:player_client=android')
                retry_cmd.insert(-1, '--extractor-args')
                result = subprocess.run(
                    retry_cmd,
                    capture_output=True,
                    text=True,
                    timeout=1800
                )
            if result.returncode != 0:
                err = result.stderr or result.stdout or "Download failed"
                logger.warning(f"[ytdl] yt-dlp binary failed exit={result.returncode} stderr={err[:500]}")
                return DownloadResult(success=False, error=err)

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


def download_as_mp3(
    url: str,
    output_dir: Optional[str] = None,
    cookies_path: Optional[str] = None,
    no_ssl_verify: bool = False,
) -> DownloadResult:
    """
    Download YouTube video and convert to MP3.

    Args:
        url: YouTube video URL
        output_dir: Directory to save MP3 (default: temp directory)
        cookies_path: Optional path to Netscape-format cookies file.
        no_ssl_verify: If True, skip SSL certificate verification.

    Returns:
        DownloadResult with success status and file info
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix='ytdl_')

    try:
        import yt_dlp

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
            'restrictfilenames': True,
            'nocheckcertificate': no_ssl_verify,
        }
        if cookies_path and os.path.isfile(cookies_path):
            ydl_opts['cookiefile'] = cookies_path
            logger.info(f"[ytdl] Using cookies file: {cookies_path}")

        def _do_mp3_download(opts: dict):
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

        try:
            info = _do_mp3_download(ydl_opts)
        except Exception as e:
            if _is_403_error(str(e)):
                logger.info("[ytdl] Got 403, retrying with Android player client")
                ydl_opts = {**ydl_opts, 'extractor_args': _YOUTUBE_403_EXTRACTOR_ARGS}
                try:
                    info = _do_mp3_download(ydl_opts)
                except Exception as retry_e:
                    logger.error(f"[ytdl] MP3 retry after 403 failed: {retry_e}", exc_info=True)
                    return DownloadResult(success=False, error=str(retry_e))
            else:
                raise

        # Find the downloaded MP3 file
        title = info.get('title', 'Unknown')
        artist = info.get('artist') or info.get('uploader') or info.get('channel')
        duration = info.get('duration')

        for f in os.listdir(output_dir):
            if f.endswith('.mp3'):
                mp3_path = os.path.join(output_dir, f)
                clean_title = title
                if artist:
                    clean_filename = f"{artist} - {clean_title}.mp3"
                else:
                    clean_filename = f"{clean_title}.mp3"
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
        return _download_with_binary(url, output_dir, cookies_path, no_ssl_verify)
    except Exception as e:
        logger.error(f"YouTube download error: {e}")
        return DownloadResult(success=False, error=str(e))


def _download_with_binary(
    url: str,
    output_dir: str,
    cookies_path: Optional[str] = None,
    no_ssl_verify: bool = False,
) -> DownloadResult:
    """Download as MP3 using yt-dlp binary."""
    try:
        output_template = os.path.join(output_dir, '%(title)s.%(ext)s')

        cmd = [
            'yt-dlp',
            '-x',
            '--audio-format', 'mp3',
            '--audio-quality', '192K',
            '-o', output_template,
            '--restrict-filenames',
        ]
        if no_ssl_verify:
            cmd.append('--no-check-certificate')
        if cookies_path and os.path.isfile(cookies_path):
            cmd.extend(['--cookies', cookies_path])
        cmd.append(url)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            err = result.stderr or result.stdout or "Download failed"
            if _is_403_error(err):
                logger.info("[ytdl] Got 403 from binary (MP3), retrying with Android player client")
                retry_cmd = cmd.copy()
                retry_cmd.insert(-1, 'youtube:player_client=android')
                retry_cmd.insert(-1, '--extractor-args')
                result = subprocess.run(retry_cmd, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                return DownloadResult(success=False, error=result.stderr or result.stdout or "Download failed")

        for f in os.listdir(output_dir):
            if f.endswith('.mp3'):
                mp3_path = os.path.join(output_dir, f)
                title = os.path.splitext(f)[0].replace('_', ' ')
                return DownloadResult(
                    success=True,
                    title=title,
                    filename=f"{title}.mp3",
                    local_path=mp3_path
                )

        return DownloadResult(success=False, error="MP3 file not found")

    except subprocess.TimeoutExpired:
        return DownloadResult(success=False, error="Download timed out")
    except Exception as e:
        return DownloadResult(success=False, error=str(e))


async def _upload_file_to_storage_proxy(
    storage_server_url: str,
    username: str,
    path: str,
    filename: str,
    content: bytes,
    content_type: str,
) -> str:
    """Upload file to storage server via proxy. Returns relative path on success."""
    import asyncio

    def _sync_upload():
        import requests
        url = f"{storage_server_url.rstrip('/')}/api/storage/upload-file"
        headers = {"X-Posterchanai-Load-Balanced": "true"}
        files = {"file": (filename, content, content_type)}
        data = {"username": username, "path": path}
        response = requests.post(url, headers=headers, files=files, data=data, timeout=300)
        if response.status_code != 200:
            try:
                err = response.json().get("detail", response.text)
            except Exception:
                err = response.text
            raise RuntimeError(f"Storage proxy upload failed: {err}")
        out = response.json()
        return out.get("path") or f"{path}/{filename}" if path else filename

    return await asyncio.to_thread(_sync_upload)


async def download_video_and_save_to_storage(
    url: str,
    user_id: int,
    db,
    subfolder: str = "YouTube Videos",
    quality: str = "best"
) -> DownloadResult:
    """
    Download YouTube video and save to user's storage.
    Uses storage proxy when storage_server_url is configured; otherwise saves to local upload_path.

    Args:
        url: YouTube video URL
        user_id: User ID for storage path lookup
        db: Database session
        subfolder: Subfolder within storage (default: "YouTube Videos")
        quality: Video quality preference (default: "best")

    Returns:
        DownloadResult with success status and storage path
    """
    from app.models import User
    from pathlib import Path
    from app.models import Setting
    import tempfile

    # Check yt-dlp available
    if not check_ytdlp_available():
        return DownloadResult(
            success=False,
            error="yt-dlp not installed. Install with: pip install yt-dlp"
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return DownloadResult(success=False, error="User not found")

    # Check if storage proxy is configured
    storage_server_setting = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    storage_server_url = storage_server_setting.value if storage_server_setting and storage_server_setting.value else None
    if storage_server_url and not storage_server_url.strip().startswith(("http://", "https://")):
        storage_server_url = None

    # Optional cookies file for YouTube 403 workaround (Netscape format from browser export)
    cookies_setting = db.query(Setting).filter(Setting.key == "ytdl_cookies_path").first()
    cookies_path = str(cookies_setting.value).strip() if cookies_setting and cookies_setting.value else None
    if cookies_path and not os.path.isfile(cookies_path):
        cookies_path = None

    # Skip SSL verification when proxy/firewall causes CERTIFICATE_VERIFY_FAILED / hostname mismatch
    ssl_setting = db.query(Setting).filter(Setting.key == "ytdl_no_ssl_verify").first()
    no_ssl_verify = (
        str(ssl_setting.value).strip().lower() in ("true", "1", "yes")
        if ssl_setting and ssl_setting.value else False
    )

    # Download to temp directory first (run in thread - yt-dlp can take minutes)
    temp_dir = tempfile.mkdtemp(prefix='ytdl_video_')
    
    try:
        logger.info(f"[ytdl] Starting download url={url!r} user={user.username!r} temp_dir={temp_dir}")
        import asyncio
        result = await asyncio.to_thread(
            download_as_video, url, temp_dir, quality, cookies_path, no_ssl_verify
        )

        if not result.success:
            logger.warning(f"[ytdl] Download failed: {result.error}")
            return result
        logger.info(f"[ytdl] Download finished title={result.title!r} file={result.filename!r}")

        if storage_server_url:
            # Save via storage proxy
            try:
                with open(result.local_path, "rb") as f:
                    content = f.read()
                ext = os.path.splitext(result.filename or "")[1].lower()
                content_type = "video/mp4" if ext in (".mp4",) else "video/x-matroska" if ext == ".mkv" else "application/octet-stream"
                relative_path = await _upload_file_to_storage_proxy(
                    storage_server_url=storage_server_url.strip(),
                    username=user.username,
                    path=subfolder,
                    filename=result.filename or "video.mp4",
                    content=content,
                    content_type=content_type,
                )
                result.storage_path = relative_path
                logger.info(f"[ytdl] Saved via storage proxy: {result.storage_path}")
            except Exception as e:
                logger.error(f"[ytdl] Storage proxy upload error: {e}", exc_info=True)
                return DownloadResult(success=False, error=f"Storage proxy upload failed: {e}")
        else:
            # Save to local storage
            upload_path_setting = db.query(Setting).filter(Setting.key == "upload_path").first()
            upload_path = upload_path_setting.value if upload_path_setting and upload_path_setting.value else "/var/lib/posterchanai"
            
            upload_base = Path(upload_path)
            if not upload_base.exists() or not upload_base.is_dir():
                return DownloadResult(
                    success=False,
                    error="Storage path does not exist or is not accessible."
                )
            
            user_storage = upload_base / user.username / subfolder
            user_storage.mkdir(parents=True, exist_ok=True)

            target_file = user_storage / result.filename
            shutil.copy2(result.local_path, target_file)
            
            result.storage_path = f"{subfolder}/{result.filename}"
            logger.info(f"[ytdl] Saved to local: {result.storage_path}")

        return result

    except Exception as e:
        logger.error(f"[ytdl] Download error: {e}", exc_info=True)
        return DownloadResult(success=False, error=str(e))
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


async def download_mp3_and_save_to_storage(
    url: str,
    user_id: int,
    db,
    subfolder: str = "Music",
) -> DownloadResult:
    """
    Download YouTube as MP3 and save to user's storage.
    Uses storage proxy when storage_server_url is configured; otherwise saves to local upload_path.
    """
    from app.models import User
    from pathlib import Path
    from app.models import Setting
    import tempfile

    if not check_ytdlp_available():
        return DownloadResult(
            success=False,
            error="yt-dlp not installed. Install with: pip install yt-dlp"
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return DownloadResult(success=False, error="User not found")

    storage_server_setting = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    storage_server_url = storage_server_setting.value if storage_server_setting and storage_server_setting.value else None
    if storage_server_url and not storage_server_url.strip().startswith(("http://", "https://")):
        storage_server_url = None

    cookies_setting = db.query(Setting).filter(Setting.key == "ytdl_cookies_path").first()
    cookies_path = str(cookies_setting.value).strip() if cookies_setting and cookies_setting.value else None
    if cookies_path and not os.path.isfile(cookies_path):
        cookies_path = None

    ssl_setting = db.query(Setting).filter(Setting.key == "ytdl_no_ssl_verify").first()
    no_ssl_verify = (
        str(ssl_setting.value).strip().lower() in ("true", "1", "yes")
        if ssl_setting and ssl_setting.value else False
    )

    temp_dir = tempfile.mkdtemp(prefix='ytdl_mp3_')
    try:
        logger.info(f"[ytdl] Starting MP3 download url={url!r} user={user.username!r} temp_dir={temp_dir}")
        import asyncio
        result = await asyncio.to_thread(
            download_as_mp3, url, temp_dir, cookies_path, no_ssl_verify
        )

        if not result.success:
            logger.warning(f"[ytdl] MP3 download failed: {result.error}")
            return result
        logger.info(f"[ytdl] MP3 download finished title={result.title!r} file={result.filename!r}")

        if storage_server_url:
            try:
                with open(result.local_path, "rb") as f:
                    content = f.read()
                relative_path = await _upload_file_to_storage_proxy(
                    storage_server_url=storage_server_url.strip(),
                    username=user.username,
                    path=subfolder,
                    filename=result.filename or "audio.mp3",
                    content=content,
                    content_type="audio/mpeg",
                )
                result.storage_path = relative_path
                logger.info(f"[ytdl] Saved MP3 via storage proxy: {result.storage_path}")
            except Exception as e:
                logger.error(f"[ytdl] Storage proxy upload error: {e}", exc_info=True)
                return DownloadResult(success=False, error=f"Storage proxy upload failed: {e}")
        else:
            upload_path_setting = db.query(Setting).filter(Setting.key == "upload_path").first()
            upload_path = upload_path_setting.value if upload_path_setting and upload_path_setting.value else "/var/lib/posterchanai"
            upload_base = Path(upload_path)
            if not upload_base.exists() or not upload_base.is_dir():
                return DownloadResult(
                    success=False,
                    error="Storage path does not exist or is not accessible."
                )
            user_storage = upload_base / user.username / subfolder
            user_storage.mkdir(parents=True, exist_ok=True)
            target_file = user_storage / result.filename
            shutil.copy2(result.local_path, target_file)
            result.storage_path = f"{subfolder}/{result.filename}"
            logger.info(f"[ytdl] Saved MP3 to local: {result.storage_path}")

        return result

    except Exception as e:
        logger.error(f"[ytdl] MP3 download error: {e}", exc_info=True)
        return DownloadResult(success=False, error=str(e))
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def format_download_result(result: DownloadResult) -> str:
    """Format download result for display."""
    if not result.success:
        return f"## ❌ Download Failed\n\n{result.error}"

    lines = ["## ✅ Download Complete\n"]

    if result.title:
        lines.append(f"**Title:** {result.title}")
    if result.artist:
        lines.append(f"**Artist:** {result.artist}")
    if result.duration is not None:
        duration_secs = int(result.duration)  # yt-dlp may return float
        mins = duration_secs // 60
        secs = duration_secs % 60
        lines.append(f"**Duration:** {mins}:{secs:02d}")

    if result.storage_path:
        lines.append(f"\n**Saved to:** `{result.storage_path}`")

    return "\n".join(lines)
