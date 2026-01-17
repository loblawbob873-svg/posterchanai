"""Utility functions for EXIF metadata handling."""
import logging
import subprocess
from pathlib import Path
from datetime import datetime
import os

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
            tags_to_try = ['DateTimeOriginal', 'CreateDate', 'DateCreated']
        elif is_video_file(file_path):
            tags_to_try = ['CreationDate', 'CreateDate', 'DateTimeOriginal', 'MediaCreateDate']
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
            logger.debug(f"[EXIF] No valid EXIF date found for {file_path.name}")
            return False
        
        # Get current file modification time
        current_mtime = file_path.stat().st_mtime
        current_dt = datetime.fromtimestamp(current_mtime)
        
        # Only update if EXIF date is different (to avoid unnecessary writes)
        if abs((exif_date - current_dt).total_seconds()) > 1:
            # Convert datetime to Unix timestamp
            timestamp = exif_date.timestamp()
            
            # Set both access and modification time
            os.utime(str(file_path), (timestamp, timestamp))
            
            logger.info(f"[EXIF] Restored timestamp for {file_path.name}: {exif_date}")
            return True
        else:
            logger.debug(f"[EXIF] Timestamp already correct for {file_path.name}")
            return False
            
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
    
    # Remove timezone info for simplicity (just extract the date/time part)
    date_str = date_str.split('+')[0].split('-')[0].strip()
    
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


def batch_restore_timestamps(directory: Path, file_extensions: list[str] = None) -> dict:
    """
    Batch restore EXIF timestamps for all media files in a directory.
    
    Args:
        directory: Directory to scan
        file_extensions: List of file extensions to process (None = all images/videos)
    
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
    
    # Get all media files
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
        
        stats['processed'] += 1
        
        try:
            if restore_exif_timestamp(file_path):
                stats['restored'] += 1
            else:
                stats['skipped'] += 1
        except Exception as e:
            logger.error(f"[EXIF] Error processing {file_path}: {e}")
            stats['errors'] += 1
    
    logger.info(
        f"[EXIF] Batch complete: {stats['processed']} processed, "
        f"{stats['restored']} restored, {stats['skipped']} skipped, "
        f"{stats['errors']} errors"
    )
    
    return stats
