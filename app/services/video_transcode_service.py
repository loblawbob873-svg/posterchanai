"""
Video Transcoding Service - Transcodes videos for faster web playback.
Stores transcoded videos in .transcoded folder within user directories.
Uses H.264 codec with web-optimized settings for fast streaming.
"""
import logging
import subprocess
from pathlib import Path
from typing import Optional, Tuple
import hashlib
import time

logger = logging.getLogger(__name__)

# Video codec settings for web playback
VIDEO_CODEC = 'libx264'
AUDIO_CODEC = 'aac'
VIDEO_PRESET = 'fast'  # Balance between speed and quality
CRF = 23  # Quality setting (18-28, lower = better quality but larger files)
MAX_RESOLUTION = (1920, 1080)  # Max 1080p for transcoded videos
AUDIO_BITRATE = '128k'


def get_transcoded_path(user_path: Path, video_path: Path) -> Path:
    """
    Get the path for a transcoded video.
    
    Args:
        user_path: The user's root directory
        video_path: The full path to the original video file
    
    Returns:
        Path to the transcoded video file in .transcoded folder
    """
    # Get relative path from user directory
    try:
        relative_path = video_path.relative_to(user_path)
    except ValueError:
        # If video is not under user_path, use just the filename
        relative_path = Path(video_path.name)
    
    # Create .transcoded directory structure matching original
    transcoded_dir = user_path / '.transcoded' / relative_path.parent
    transcoded_dir.mkdir(parents=True, exist_ok=True)
    
    # Use original filename with .mp4 extension for transcoded version
    transcoded_filename = relative_path.stem + '.mp4'
    return transcoded_dir / transcoded_filename


def is_video_already_optimized(video_path: Path) -> bool:
    """
    Check if a video is already in an optimized format (H.264 MP4).
    If so, transcoding may not be necessary.
    """
    try:
        # Use ffprobe to check codec
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name', '-of', 'default=noprint_wrappers=1:nokey=1',
             str(video_path)],
            capture_output=True,
            timeout=10,
            text=True
        )
        
        if result.returncode == 0:
            codec = result.stdout.strip().lower()
            # Check if it's H.264 and container is MP4
            if codec == 'h264' and video_path.suffix.lower() == '.mp4':
                # Check audio codec too
                audio_result = subprocess.run(
                    ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                     '-show_entries', 'stream=codec_name', '-of', 'default=noprint_wrappers=1:nokey=1',
                     str(video_path)],
                    capture_output=True,
                    timeout=10,
                    text=True
                )
                if audio_result.returncode == 0:
                    audio_codec = audio_result.stdout.strip().lower()
                    if audio_codec in ['aac', 'mp3']:
                        return True
        
        return False
    except Exception as e:
        logger.debug(f"Could not check if video is optimized: {e}")
        return False


def transcode_video(
    user_path: Path,
    video_path: Path,
    max_resolution: Tuple[int, int] = MAX_RESOLUTION,
    crf: int = CRF,
    preset: str = VIDEO_PRESET
) -> Optional[Path]:
    """
    Transcode a video file to H.264 MP4 format for fast web playback.
    
    Args:
        user_path: The user's root directory
        video_path: Path to the source video file
        max_resolution: Maximum resolution (width, height) for transcoded video
        crf: Constant Rate Factor for quality (18-28, lower = better quality)
        preset: Encoding preset (ultrafast, fast, medium, slow, etc.)
    
    Returns:
        Path to transcoded video file, or None if transcoding failed
    """
    if not video_path.exists():
        logger.error(f"Video file does not exist: {video_path}")
        return None
    
    transcoded_path = get_transcoded_path(user_path, video_path)
    
    # If transcoded version already exists and is newer than original, use it
    # Always transcode to ensure consistent format and web optimization
    if transcoded_path.exists():
        if transcoded_path.stat().st_mtime >= video_path.stat().st_mtime:
            logger.debug(f"Transcoded video already exists: {transcoded_path}")
            return transcoded_path
    
    # Check if ffmpeg is available
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5, check=True)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        logger.warning("ffmpeg not available, cannot transcode video")
        return None
    
    try:
        # Get video dimensions to determine if we need to scale down
        probe_result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0',
             str(video_path)],
            capture_output=True,
            timeout=10,
            text=True
        )
        
        scale_filter = ''
        if probe_result.returncode == 0:
            try:
                width, height = map(int, probe_result.stdout.strip().split('x'))
                max_width, max_height = max_resolution
                
                # Calculate scale to fit within max resolution while maintaining aspect ratio
                if width > max_width or height > max_height:
                    scale_ratio = min(max_width / width, max_height / height)
                    new_width = int(width * scale_ratio)
                    new_height = int(height * scale_ratio)
                    # Ensure dimensions are even (required for H.264)
                    new_width = new_width - (new_width % 2)
                    new_height = new_height - (new_height % 2)
                    scale_filter = f',scale={new_width}:{new_height}'
            except (ValueError, IndexError):
                logger.warning(f"Could not parse video dimensions, using original size")
        
        # Build ffmpeg command
        ffmpeg_cmd = [
            'ffmpeg',
            '-i', str(video_path),
            '-c:v', VIDEO_CODEC,
            '-preset', preset,
            '-crf', str(crf),
            '-c:a', AUDIO_CODEC,
            '-b:a', AUDIO_BITRATE,
            '-movflags', '+faststart',  # Enable fast start for web streaming
            '-y',  # Overwrite output file
        ]
        
        # Add scale filter if needed
        if scale_filter:
            ffmpeg_cmd.extend(['-vf', f'scale={scale_filter.lstrip(",")}'])
        
        ffmpeg_cmd.append(str(transcoded_path))
        
        logger.info(f"Transcoding video: {video_path.name} -> {transcoded_path.name}")
        start_time = time.time()
        
        # Run transcoding
        result = subprocess.run(
            ffmpeg_cmd,
            capture_output=True,
            timeout=3600,  # 1 hour timeout
            text=True
        )
        
        elapsed = time.time() - start_time
        
        if result.returncode == 0 and transcoded_path.exists():
            original_size = video_path.stat().st_size
            transcoded_size = transcoded_path.stat().st_size
            size_reduction = ((original_size - transcoded_size) / original_size) * 100 if original_size > 0 else 0
            
            logger.info(
                f"Video transcoded successfully: {video_path.name} "
                f"({elapsed:.1f}s, {size_reduction:.1f}% size reduction)"
            )
            return transcoded_path
        else:
            error_msg = result.stderr[-500:] if result.stderr else "Unknown error"
            logger.error(f"Video transcoding failed for {video_path.name}: {error_msg}")
            # Clean up failed transcoded file if it exists
            if transcoded_path.exists():
                transcoded_path.unlink()
            return None
            
    except subprocess.TimeoutExpired:
        logger.error(f"Video transcoding timed out for {video_path.name}")
        if transcoded_path.exists():
            transcoded_path.unlink()
        return None
    except Exception as e:
        logger.error(f"Error transcoding video {video_path.name}: {e}", exc_info=True)
        if transcoded_path.exists():
            transcoded_path.unlink()
        return None


def get_transcoded_video_if_exists(user_path: Path, video_path: Path) -> Optional[Path]:
    """
    Get transcoded video path if it exists and is up to date.
    
    Args:
        user_path: The user's root directory
        video_path: Path to the original video file
    
    Returns:
        Path to transcoded video if it exists and is newer than original, None otherwise
    """
    transcoded_path = get_transcoded_path(user_path, video_path)
    
    if transcoded_path.exists():
        # Check if transcoded version is newer than original
        if video_path.exists():
            if transcoded_path.stat().st_mtime >= video_path.stat().st_mtime:
                return transcoded_path
        else:
            # Original doesn't exist, but transcoded does - return it anyway
            return transcoded_path
    
    return None
