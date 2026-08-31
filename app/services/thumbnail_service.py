"""
Thumbnail Service - Generates and manages image and video thumbnails.
Stores thumbnails in .thumbnails folder within user directories.
"""
import logging
import subprocess
from pathlib import Path
from typing import Optional, List, Tuple, Callable
from PIL import Image
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

logger = logging.getLogger(__name__)

# Supported image extensions
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif'}

# Supported video extensions
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.3gp', '.ogv'}

# Default thumbnail size
DEFAULT_THUMBNAIL_SIZE = (200, 200)


def is_image_file(file_path: Path) -> bool:
    """Check if a file is an image based on its extension."""
    return file_path.suffix.lower() in IMAGE_EXTENSIONS


def is_video_file(file_path: Path) -> bool:
    """Check if a file is a video based on its extension."""
    return file_path.suffix.lower() in VIDEO_EXTENSIONS


def is_media_file(file_path: Path) -> bool:
    """Check if a file is an image or video."""
    return is_image_file(file_path) or is_video_file(file_path)


def get_thumbnail_path(user_path: Path, image_path: Path) -> Path:
    """
    Get the thumbnail path for an image.
    
    Args:
        user_path: The user's root directory (e.g., /var/lib/posterchanai/username)
        image_path: The full path to the image file
    
    Returns:
        Path to the thumbnail file in .thumbnails folder
    """
    # Get relative path from user directory
    try:
        relative_path = image_path.relative_to(user_path)
    except ValueError:
        # Image is not within user directory, use filename hash
        relative_path = Path(image_path.name)

    # A `..` here would escape .thumbnails — and, with enough of them, the user directory.
    # `relative_to` does NOT normalise, so an un-normalised caller path survives it intact:
    # `<user>/a/../../../etc/x.jpg` yields `a/../../../etc/x.jpg`, and since that parent is
    # appended to `.thumbnails/` below, the thumbnail would be written to `/srv/etc/`.
    # No current caller can reach it (both pass paths that came from the filesystem — a
    # server-generated save path, or an entry from a directory walk), so this is a guard rather
    # than a fix for a live bug. It is here because "no caller does that today" is a property of
    # the callers, not of this function, and this function is the one that builds the path.
    # Falls back to the bare filename, which is already the answer for a path outside the user
    # directory.
    if any(part == '..' for part in relative_path.parts):
        relative_path = Path(image_path.name)

    # Create a safe filename for thumbnail (use hash to avoid path issues)
    # Use relative path to preserve directory structure
    path_str = str(relative_path).replace('/', '_').replace('\\', '_')
    # Create hash for very long paths
    if len(path_str) > 200:
        path_hash = hashlib.md5(path_str.encode()).hexdigest()
        path_str = path_hash + '_' + Path(relative_path).name

    # Change extension to .jpg for thumbnails — but KEEP THE ORIGINAL ONE IN THE NAME.
    # Dropping it made `photo.jpg` and `photo.png` in one folder collide onto a single
    # `.thumbnails/photo.jpg`: whichever was generated last won, and the other file showed the
    # wrong picture in the browser. Same stem with different image extensions is an ordinary thing
    # to have (an export beside its original), so this was reachable with no help from anybody.
    # Thumbnails written under the old name are orphaned by this and regenerate on next view —
    # `.thumbnails` is a cache, and every reader, writer and deleter derives its path from here.
    source_suffix = Path(path_str).suffix.lstrip('.').lower()
    thumbnail_name = Path(path_str).stem + (f'_{source_suffix}' if source_suffix else '') + '.jpg'
    
    # Create .thumbnails directory path
    thumbnails_dir = user_path / '.thumbnails'
    
    # Preserve subdirectory structure in thumbnails
    if relative_path.parent != Path('.'):
        # Create subdirectory structure in .thumbnails
        thumbnails_dir = thumbnails_dir / relative_path.parent
    
    return thumbnails_dir / thumbnail_name


def generate_thumbnail_file(
    image_path: Path,
    thumbnail_path: Path,
    max_size: Tuple[int, int] = DEFAULT_THUMBNAIL_SIZE,
    quality: int = 85,
    max_image_size_mb: int = 100
) -> bool:
    """
    Generate a thumbnail file from an image.
    
    Args:
        image_path: Path to the source image
        thumbnail_path: Path where thumbnail should be saved
        max_size: Maximum size (width, height) for thumbnail
        quality: JPEG quality (1-100)
        max_image_size_mb: Maximum image size in MB to process (default 100MB)
    
    Returns:
        True if successful, False otherwise
    """
    try:
        # Check file size limit
        max_size_bytes = max_image_size_mb * 1024 * 1024
        file_size = image_path.stat().st_size
        if file_size > max_size_bytes:
            logger.warning(f"Image too large for thumbnail ({file_size / 1024 / 1024:.1f}MB > {max_image_size_mb}MB): {image_path}")
            return False
        
        # Create thumbnail directory if it doesn't exist
        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Check if file is readable and not empty
        try:
            if not image_path.exists():
                logger.debug(f"Image file does not exist: {image_path}")
                return False
            
            if image_path.stat().st_size == 0:
                logger.debug(f"Image file is empty: {image_path}")
                return False
        except OSError as e:
            logger.debug(f"Cannot access image file: {image_path}: {e}")
            return False
        
        # Verify image is valid before processing
        try:
            with Image.open(image_path) as verify_img:
                verify_img.verify()
        except Exception as e:
            # Log as debug instead of error - these are expected for corrupted files
            logger.debug(f"Invalid or corrupted image file (skipping): {image_path.name}: {e}")
            return False
        
        # Reopen for processing (verify() closes the file)
        # Open and process image
        with Image.open(image_path) as img:
            original_mode = img.mode
            logger.debug(f"Processing image {image_path.name} with mode: {original_mode}")
            
            # Try to load the image data to detect truncation early
            try:
                img.load()  # Load image data to detect truncation
            except OSError as load_error:
                # Handle truncated/corrupted images gracefully
                if "truncated" in str(load_error).lower() or "broken data stream" in str(load_error).lower():
                    logger.debug(f"Truncated or corrupted image file (skipping): {image_path.name}: {load_error}")
                    return False
                raise  # Re-raise if it's a different OSError
            
            # Convert to a mode that supports thumbnail operations first
            # Handle palette mode (P) - convert to RGBA first to preserve transparency
            if img.mode == 'P':
                # Check if image has transparency
                if 'transparency' in img.info:
                    img = img.convert('RGBA')
                else:
                    img = img.convert('RGB')
            # Handle grayscale with alpha (LA)
            elif img.mode == 'LA':
                img = img.convert('RGBA')
            # Handle other unsupported modes
            elif img.mode not in ('RGB', 'RGBA', 'L', 'CMYK'):
                # Try to convert to RGB, fallback to RGBA if that fails
                try:
                    img = img.convert('RGB')
                except Exception:
                    try:
                        img = img.convert('RGBA')
                    except Exception as e:
                        logger.warning(f"Could not convert mode {original_mode} to RGB/RGBA: {e}, trying L")
                        img = img.convert('L').convert('RGB')
            
            # Create thumbnail (maintains aspect ratio) - now safe to call
            try:
                img.thumbnail(max_size, Image.Resampling.LANCZOS)
            except OSError as thumb_error:
                # Handle truncated images that fail during thumbnail creation
                if "truncated" in str(thumb_error).lower() or "broken data stream" in str(thumb_error).lower():
                    logger.debug(f"Truncated image file (skipping): {image_path.name}: {thumb_error}")
                    return False
                raise  # Re-raise if it's a different OSError
            
            # Convert to RGB for JPEG saving (handle transparency)
            if img.mode in ('RGBA', 'LA'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'RGBA':
                    background.paste(img, mask=img.split()[-1])  # Use alpha channel as mask
                elif img.mode == 'LA':
                    background.paste(img, mask=img.split()[-1])  # Use alpha channel as mask
                img = background
            elif img.mode not in ('RGB', 'L'):
                # Convert any remaining non-RGB modes to RGB
                if img.mode == 'L':
                    img = img.convert('RGB')
                else:
                    img = img.convert('RGB')
            
            # Ensure we have RGB mode for JPEG
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Save thumbnail
            img.save(thumbnail_path, format='JPEG', quality=quality)
            
            logger.debug(f"Generated thumbnail: {thumbnail_path} (original mode: {original_mode}, final mode: {img.mode})")
            return True
            
    except OSError as e:
        # Handle truncated/corrupted images gracefully
        if "truncated" in str(e).lower() or "broken data stream" in str(e).lower() or "cannot identify" in str(e).lower():
            logger.debug(f"Corrupted or truncated image file (skipping): {image_path.name}: {e}")
            return False
        logger.error(f"OSError generating thumbnail for {image_path}: {e}", exc_info=True)
        return False
    except Exception as e:
        # Only log unexpected errors
        if "cannot identify" not in str(e).lower():
            logger.error(f"Error generating thumbnail for {image_path}: {e}", exc_info=True)
        else:
            logger.debug(f"Unsupported image format (skipping): {image_path.name}: {e}")
        return False


def generate_thumbnail_for_video(
    video_path: Path,
    thumbnail_path: Path,
    max_size: Tuple[int, int] = DEFAULT_THUMBNAIL_SIZE,
    time_offset: float = 1.0
) -> bool:
    """
    Generate a thumbnail from a video file using ffmpeg.
    
    Args:
        video_path: Path to the source video
        thumbnail_path: Path where thumbnail should be saved
        max_size: Maximum size (width, height) for thumbnail
        time_offset: Time offset in seconds to extract frame (default 1.0)
    
    Returns:
        True if successful, False otherwise
    """
    try:
        # Check if ffmpeg is available
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5, check=True)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            logger.warning("ffmpeg not available, cannot generate video thumbnail")
            return False
        
        # Create thumbnail directory if it doesn't exist
        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Use ffmpeg to extract frame and resize
        width, height = max_size
        ffmpeg_cmd = [
            'ffmpeg',
            '-i', str(video_path),
            '-ss', str(time_offset),  # Seek to time offset
            '-vframes', '1',  # Extract 1 frame
            '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease',  # Resize maintaining aspect ratio
            '-y',  # Overwrite output file
            str(thumbnail_path)
        ]
        
        result = subprocess.run(
            ffmpeg_cmd,
            capture_output=True,
            timeout=30,  # 30 second timeout
            check=False
        )
        
        if result.returncode == 0 and thumbnail_path.exists():
            logger.debug(f"Generated video thumbnail: {thumbnail_path}")
            return True
        else:
            error_msg = result.stderr.decode('utf-8', errors='ignore') if result.stderr else 'Unknown error'
            logger.warning(f"Failed to generate video thumbnail for {video_path}: {error_msg[:200]}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout generating video thumbnail for {video_path}")
        return False
    except Exception as e:
        logger.error(f"Error generating video thumbnail for {video_path}: {e}", exc_info=True)
        return False


def generate_thumbnail_for_image(
    user_path: Path,
    image_path: Path,
    max_size: Tuple[int, int] = DEFAULT_THUMBNAIL_SIZE
) -> Optional[Path]:
    """
    Generate thumbnail for a single image and return the thumbnail path.
    
    Args:
        user_path: The user's root directory
        image_path: Full path to the image file
        max_size: Maximum size for thumbnail
    
    Returns:
        Path to generated thumbnail, or None if failed
    """
    if not is_image_file(image_path):
        return None
    
    if not image_path.exists() or not image_path.is_file():
        return None
    
    thumbnail_path = get_thumbnail_path(user_path, image_path)
    
    if generate_thumbnail_file(image_path, thumbnail_path, max_size):
        return thumbnail_path
    
    return None


def generate_thumbnail_for_video_file(
    user_path: Path,
    video_path: Path,
    max_size: Tuple[int, int] = DEFAULT_THUMBNAIL_SIZE
) -> Optional[Path]:
    """
    Generate thumbnail for a single video and return the thumbnail path.
    
    Args:
        user_path: The user's root directory
        video_path: Full path to the video file
        max_size: Maximum size for thumbnail
    
    Returns:
        Path to generated thumbnail, or None if failed
    """
    if not is_video_file(video_path):
        return None
    
    if not video_path.exists() or not video_path.is_file():
        return None
    
    thumbnail_path = get_thumbnail_path(user_path, video_path)
    
    if generate_thumbnail_for_video(video_path, thumbnail_path, max_size):
        return thumbnail_path
    
    return None


def generate_thumbnail_for_media(
    user_path: Path,
    media_path: Path,
    max_size: Tuple[int, int] = DEFAULT_THUMBNAIL_SIZE
) -> Optional[Path]:
    """
    Generate thumbnail for an image or video file.
    
    Args:
        user_path: The user's root directory
        media_path: Full path to the media file (image or video)
        max_size: Maximum size for thumbnail
    
    Returns:
        Path to generated thumbnail, or None if failed
    """
    if is_image_file(media_path):
        return generate_thumbnail_for_image(user_path, media_path, max_size)
    elif is_video_file(media_path):
        return generate_thumbnail_for_video_file(user_path, media_path, max_size)
    return None


def _process_single_thumbnail(
    media_path: Path,
    user_path: Path,
    max_size: Tuple[int, int],
    lock: threading.Lock,
    stats: dict
) -> None:
    """
    Process a single media file to generate thumbnail.
    Thread-safe helper function for parallel processing.
    
    Args:
        media_path: Path to the media file
        user_path: The user's root directory
        max_size: Maximum size for thumbnails
        lock: Thread lock for stats updates
        stats: Dictionary with 'successful', 'failed', 'skipped' counters
    """
    try:
        # Quick check: skip if file doesn't exist or is empty
        try:
            if not media_path.exists() or media_path.stat().st_size == 0:
                with lock:
                    stats['failed'] += 1
                return
        except OSError:
            with lock:
                stats['failed'] += 1
            return
        
        thumbnail_path = get_thumbnail_path(user_path, media_path)
        
        # OPTIMIZATION: Skip if thumbnail already exists and is up-to-date
        # NOTE: After EXIF restoration, file timestamps may change, so we check carefully
        # IMPORTANT: Always regenerate thumbnails if source file is newer or equal to thumbnail
        # This ensures thumbnails are updated after EXIF restoration changes file timestamps
        if thumbnail_path.exists():
            try:
                thumbnail_mtime = thumbnail_path.stat().st_mtime
                media_mtime = media_path.stat().st_mtime
                # Only skip if thumbnail is significantly newer (more than 2 seconds) than source
                # This accounts for EXIF restoration which may make source file older
                # But we want to regenerate if source is newer or even slightly older (within 2 seconds)
                if thumbnail_mtime > media_mtime + 2:
                    with lock:
                        stats['successful'] += 1
                        stats['skipped'] += 1
                    logger.debug(f"[Thumbnail] Skipping {media_path.name} - thumbnail is up-to-date (thumb: {thumbnail_mtime}, media: {media_mtime})")
                    return
                else:
                    # Thumbnail exists but source is newer or equal (or within 2 seconds) - regenerate to be safe
                    logger.debug(f"[Thumbnail] Regenerating {media_path.name} - source file may have been updated (thumb: {thumbnail_mtime}, media: {media_mtime})")
            except OSError as e:
                # If we can't check thumbnail, regenerate it
                logger.debug(f"[Thumbnail] Cannot check thumbnail timestamp for {media_path.name}: {e}, regenerating")
                pass
        
        # Generate thumbnail based on file type
        generation_success = False
        if is_image_file(media_path):
            generation_success = generate_thumbnail_file(media_path, thumbnail_path, max_size)
        elif is_video_file(media_path):
            generation_success = generate_thumbnail_for_video(media_path, thumbnail_path, max_size)
        
        with lock:
            if generation_success:
                stats['successful'] += 1
                logger.debug(f"[Thumbnail] Successfully generated thumbnail for {media_path.name}")
            else:
                stats['failed'] += 1
                logger.warning(f"[Thumbnail] Failed to generate thumbnail for {media_path.name}")
                
    except Exception as e:
        # Log as debug for corrupted/truncated files, error for unexpected issues
        error_str = str(e).lower()
        if any(keyword in error_str for keyword in ["cannot identify", "truncated", "broken data stream", "corrupted"]):
            logger.debug(f"Skipping corrupted/truncated media: {media_path.name}")
        else:
            logger.error(f"Error processing {media_path}: {e}")
        with lock:
            stats['failed'] += 1


def generate_thumbnails_for_user(
    user_path: Path,
    max_size: Tuple[int, int] = DEFAULT_THUMBNAIL_SIZE,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    max_workers: Optional[int] = None
) -> Tuple[int, int]:
    """
    Generate thumbnails for all images and videos in a user's directory.
    OPTIMIZATION: Skips files that already have up-to-date thumbnails.
    Uses multi-threading for parallel processing to improve performance.
    
    Args:
        user_path: The user's root directory
        max_size: Maximum size for thumbnails
        progress_callback: Optional callback function(current, total) for progress updates
        max_workers: Maximum number of worker threads (default: min(32, CPU count * 2))
    
    Returns:
        Tuple of (successful_count, failed_count)
    """
    if not user_path.exists() or not user_path.is_dir():
        logger.warning(f"User path does not exist: {user_path}")
        return (0, 0)
    
    # Find all media files (images AND videos) in single traversal
    def _is_in_thumbnails_dir(path: Path, base_path: Path) -> bool:
        """Check if path is within .thumbnails directory."""
        try:
            relative = path.relative_to(base_path)
            return '.thumbnails' in relative.parts
        except ValueError:
            return False
    
    media_files = []
    for path in user_path.rglob('*'):
        if path.is_file() and is_media_file(path) and not _is_in_thumbnails_dir(path, user_path):
            media_files.append(path)
    
    total = len(media_files)
    
    if total == 0:
        logger.info(f"[Thumbnail] No media files found in {user_path}")
        return (0, 0)
    
    # Determine optimal number of workers
    if max_workers is None:
        import os
        cpu_count = os.cpu_count() or 4
        # Use more workers for I/O-bound operations (file reading, PIL operations)
        # But cap at 32 to avoid too much overhead
        max_workers = min(32, cpu_count * 2)
    
    logger.info(f"[Thumbnail] Processing {total} media files in {user_path} using {max_workers} workers")
    
    # Thread-safe stats tracking
    stats = {'successful': 0, 'failed': 0, 'skipped': 0}
    lock = threading.Lock()
    completed = 0
    
    # Process files in parallel using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        futures = {
            executor.submit(_process_single_thumbnail, media_path, user_path, max_size, lock, stats): media_path
            for media_path in media_files
        }
        
        # Process completed tasks and update progress
        for future in as_completed(futures):
            completed += 1
            if progress_callback:
                try:
                    progress_callback(completed, total)
                except Exception as e:
                    logger.warning(f"Progress callback error: {e}")
            
            # Log progress every 100 files or at milestones
            if completed % 100 == 0 or completed == total:
                with lock:
                    logger.info(f"[Thumbnail] Progress: {completed}/{total} files processed "
                              f"({stats['successful']} generated, {stats['skipped']} skipped (up-to-date), {stats['failed']} failed)")
    
    logger.info(f"[Thumbnail] Complete: {stats['successful']} successful "
              f"({stats['skipped']} skipped, already up-to-date), {stats['failed']} failed")
    return (stats['successful'], stats['failed'])


def get_thumbnail_if_exists(user_path: Path, image_path: Path) -> Optional[Path]:
    """
    Get thumbnail path if it exists and is up to date.
    
    Args:
        user_path: The user's root directory
        image_path: Full path to the image file
    
    Returns:
        Path to thumbnail if it exists and is current, None otherwise
    """
    thumbnail_path = get_thumbnail_path(user_path, image_path)
    
    if thumbnail_path.exists():
        # Check if thumbnail is newer than or equal to image modification time
        try:
            if thumbnail_path.stat().st_mtime >= image_path.stat().st_mtime:
                return thumbnail_path
        except OSError:
            pass
    
    return None


def delete_thumbnail(user_path: Path, image_path: Path) -> bool:
    """
    Delete thumbnail for an image.
    
    Args:
        user_path: The user's root directory
        image_path: Full path to the image file
    
    Returns:
        True if deleted or didn't exist, False on error
    """
    try:
        thumbnail_path = get_thumbnail_path(user_path, image_path)
        if thumbnail_path.exists():
            thumbnail_path.unlink()
            # Try to remove empty parent directories
            thumbnails_base = user_path / '.thumbnails'
            try:
                parent = thumbnail_path.parent
                while parent != thumbnails_base and parent.exists():
                    try:
                        if not any(parent.iterdir()):
                            parent.rmdir()
                            parent = parent.parent
                        else:
                            break  # Directory not empty
                    except OSError:
                        break  # Permission error or directory not empty
            except OSError:
                pass  # Ignore errors when removing directories
        return True
    except Exception as e:
        logger.error(f"Error deleting thumbnail for {image_path}: {e}", exc_info=True)
        return False
