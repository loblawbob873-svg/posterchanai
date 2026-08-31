"""Utility functions for EXIF metadata handling."""
import logging
import re
import subprocess
from pathlib import Path
from datetime import datetime
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

logger = logging.getLogger(__name__)


def is_image_file(file_path: Path) -> bool:
    """Check if file is an image."""
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.heic', '.heif', '.webp'}
    return file_path.suffix.lower() in image_extensions


def is_video_file(file_path: Path) -> bool:
    """Check if file is a video."""
    video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp', '.wmv', '.flv', '.webm', '.mpg', '.mpeg'}
    return file_path.suffix.lower() in video_extensions


def restore_exif_timestamp(file_path: Path) -> bool:
    """
    Restore file modification timestamp from EXIF metadata.
    
    For images: Uses DateTimeOriginal
    For videos: Uses CreationDate, CreateDate, or DateTimeOriginal
    
    Returns True if timestamp was successfully restored, False otherwise.
    """
    try:
        if not file_path.exists() or not file_path.is_file():
            return False
        
        # Check if exiftool is available
        if not _check_exiftool_available():
            logger.warning("[EXIF] exiftool not available, skipping timestamp restoration")
            return False
        
        # Determine what EXIF tags to try based on file type
        if is_image_file(file_path):
            tags_to_try = ['DateTimeOriginal', 'CreateDate', 'DateCreated', 'ModifyDate', 'FileModifyDate']
        elif is_video_file(file_path):
            # For videos, try more tags as different formats use different metadata
            tags_to_try = [
                'CreationDate', 'CreateDate', 'DateTimeOriginal', 'MediaCreateDate',
                'ModifyDate', 'FileModifyDate', 'TrackCreateDate', 'TrackModifyDate',
                'MediaModifyDate', 'QuickTime:CreateDate', 'QuickTime:ModifyDate'
            ]
        else:
            return False
        
        # Try each tag until we find one with a valid date
        exif_date = None
        for tag in tags_to_try:
            try:
                result = subprocess.run(
                    ['exiftool', '-s', '-s', '-s', f'-{tag}', str(file_path)],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    date_str = result.stdout.strip()
                    exif_date = _parse_exif_date(date_str)
                    if exif_date:
                        logger.debug(f"[EXIF] Found {tag} for {file_path.name}: {date_str}")
                        break
            except Exception as e:
                logger.debug(f"[EXIF] Error reading {tag} from {file_path.name}: {e}")
                continue
        
        if not exif_date:
            # For files without EXIF, check if file timestamp seems wrong (very recent, suggesting it's a copy date)
            # If file was modified in the last 24 hours and is an image/video, it might be a fresh copy
            # In that case, we can't fix it without EXIF, but we'll log it
            current_mtime = file_path.stat().st_mtime
            import time
            file_age_hours = (time.time() - current_mtime) / 3600
            if file_age_hours < 24:
                logger.debug(f"[EXIF] No EXIF date found for {file_path.name} (file is {file_age_hours:.1f} hours old - might be a recent copy)")
            else:
                logger.debug(f"[EXIF] No valid EXIF date found for {file_path.name}")
            return False
        
        # Get current file modification time
        current_mtime = file_path.stat().st_mtime
        current_dt = datetime.fromtimestamp(current_mtime)
        
        # Always update if EXIF date is available and different (even by 1 second)
        # This ensures files copied via rsync get their original dates restored
        time_diff_seconds = abs((exif_date - current_dt).total_seconds())
        if time_diff_seconds > 0.5:
            # Convert datetime to Unix timestamp
            timestamp = exif_date.timestamp()
            
            # Set both access and modification time
            os.utime(str(file_path), (timestamp, timestamp))
            
            # Verify the timestamp was actually updated
            verify_stat = file_path.stat()
            verify_mtime = datetime.fromtimestamp(verify_stat.st_mtime)
            if abs((verify_mtime - exif_date).total_seconds()) > 1.0:
                logger.warning(f"[EXIF] Timestamp update may have failed for {file_path.name}: set to {exif_date}, but file shows {verify_mtime}")
            else:
                logger.info(f"[EXIF] Restored timestamp for {file_path.name}: {exif_date} (was {current_dt}, diff={time_diff_seconds/86400:.1f} days)")
            return True
        else:
            logger.debug(f"[EXIF] Timestamp already correct for {file_path.name}: {exif_date} (diff={time_diff_seconds:.1f}s)")
            return True  # Return True even if already correct, to count as processed
            
    except Exception as e:
        logger.error(f"[EXIF] Error restoring timestamp for {file_path}: {e}")
        return False


def _check_exiftool_available() -> bool:
    """Check if exiftool is available on the system."""
    try:
        result = subprocess.run(
            ['exiftool', '-ver'],
            capture_output=True,
            timeout=2
        )
        return result.returncode == 0
    except Exception:
        return False


def _parse_exif_date(date_str: str) -> datetime | None:
    """
    Parse EXIF date string to datetime object.
    
    Handles various EXIF date formats:
    - 2024:01:15 14:23:45
    - 2024-01-15 14:23:45
    - 2024:01:15 14:23:45-08:00
    - 2024-01-15T14:23:45Z
    """
    if not date_str:
        return None
    
    # Strip a TRAILING TIMEZONE ONLY — never an arbitrary hyphen.
    #
    # This was `date_str.split('+')[0].split('-')[0]`, and the second split is the bug: a hyphen is
    # the DATE SEPARATOR in half the formats listed below. "2024-01-15 14:23:45" was truncated to
    # "2024", which nothing here can parse, so it returned None. Four of the eight format strings —
    # every dash-dated one, including the two the docstring above promises ("2024-01-15 14:23:45"
    # and "2024-01-15T14:23:45Z") — were unreachable.
    #
    # The failure is silent: restore_exif_timestamp gets None, gives up, and the photo keeps the
    # mtime of when it was downloaded instead of when it was taken. That is the entire feature.
    date_str = re.sub(r'(?:Z|[+-]\d{2}:?\d{2})\s*$', '', date_str.strip()).strip()
    
    # Try various date formats
    formats = [
        '%Y:%m:%d %H:%M:%S',
        '%Y-%m-%d %H:%M:%S',
        '%Y:%m:%d %H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y:%m:%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M:%S',
        '%Y:%m:%d',
        '%Y-%m-%d',
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    
    logger.debug(f"[EXIF] Could not parse date: {date_str}")
    return None


def _process_single_exif_file(
    file_path: Path,
    lock: threading.Lock,
    stats: dict
) -> None:
    """
    Process a single file for EXIF timestamp restoration.
    Thread-safe helper function for parallel processing.
    
    Args:
        file_path: Path to the file to process
        lock: Thread lock for stats updates
        stats: Dictionary with 'processed', 'restored', 'skipped', 'errors' counters
    """
    # Increment processed counter at the start to ensure all files are counted
    with lock:
        stats['processed'] += 1
    
    try:
        if restore_exif_timestamp(file_path):
            with lock:
                stats['restored'] += 1
        else:
            with lock:
                stats['skipped'] += 1
    except Exception as e:
        logger.error(f"[EXIF] Error processing {file_path}: {e}")
        with lock:
            stats['errors'] += 1


def batch_restore_timestamps(directory: Path, file_extensions: list[str] = None, max_workers: int = None) -> dict:
    """
    Batch restore EXIF timestamps for all media files in a directory.
    Uses multi-threading for parallel processing to improve performance.
    
    Args:
        directory: Directory to scan
        file_extensions: List of file extensions to process (None = all images/videos)
        max_workers: Maximum number of worker threads (default: min(32, CPU count * 2))
    
    Returns:
        dict with statistics: {
            'processed': int,
            'restored': int,
            'skipped': int,
            'errors': int
        }
    """
    stats = {
        'processed': 0,
        'restored': 0,
        'skipped': 0,
        'errors': 0
    }
    
    if not directory.exists() or not directory.is_dir():
        logger.error(f"[EXIF] Directory does not exist: {directory}")
        return stats
    
    logger.info(f"[EXIF] Starting batch timestamp restoration in: {directory}")
    
    # Collect all media files to process
    media_files = []
    for file_path in directory.rglob('*'):
        if not file_path.is_file():
            continue
        
        # Skip thumbnail directories
        if '.thumbnails' in file_path.parts or '.thumbnail' in file_path.parts:
            continue
        
        # Check if this is a media file we should process
        if file_extensions:
            if file_path.suffix.lower() not in file_extensions:
                continue
        else:
            if not (is_image_file(file_path) or is_video_file(file_path)):
                continue
        
        media_files.append(file_path)
    
    if not media_files:
        logger.info(f"[EXIF] No media files found in {directory}")
        return stats
    
    # Determine optimal number of workers
    if max_workers is None:
        import os as os_module
        cpu_count = os_module.cpu_count() or 4
        # Use more workers for I/O-bound operations (exiftool subprocess calls)
        # But cap at 32 to avoid too much overhead
        max_workers = min(32, cpu_count * 2)
    
    logger.info(f"[EXIF] Processing {len(media_files)} files using {max_workers} workers")
    
    # Thread-safe stats tracking
    lock = threading.Lock()
    
    # Process files in parallel using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        futures = {
            executor.submit(_process_single_exif_file, file_path, lock, stats): file_path
            for file_path in media_files
        }
        
        # Wait for all tasks to complete
        completed = 0
        for future in as_completed(futures):
            completed += 1
            # Log progress every 100 files or at milestones
            if completed % 100 == 0 or completed == len(media_files):
                with lock:
                    logger.info(f"[EXIF] Progress: {completed}/{len(media_files)} files processed "
                              f"({stats['restored']} restored, {stats['skipped']} skipped, {stats['errors']} errors)")
    
    logger.info(
        f"[EXIF] Batch complete: {stats['processed']} processed, "
        f"{stats['restored']} restored, {stats['skipped']} skipped, "
        f"{stats['errors']} errors"
    )
    
    return stats
