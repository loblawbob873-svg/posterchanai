"""
Thumbnail Service - Generates and manages image thumbnails.
Stores thumbnails in .thumbnails folder within user directories.
"""
import logging
from pathlib import Path
from typing import Optional, List, Tuple, Callable
from PIL import Image
import hashlib

logger = logging.getLogger(__name__)

# Supported image extensions
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif'}

# Default thumbnail size
DEFAULT_THUMBNAIL_SIZE = (200, 200)


def is_image_file(file_path: Path) -> bool:
    """Check if a file is an image based on its extension."""
    return file_path.suffix.lower() in IMAGE_EXTENSIONS


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
    
    # Create a safe filename for thumbnail (use hash to avoid path issues)
    # Use relative path to preserve directory structure
    path_str = str(relative_path).replace('/', '_').replace('\\', '_')
    # Create hash for very long paths
    if len(path_str) > 200:
        path_hash = hashlib.md5(path_str.encode()).hexdigest()
        path_str = path_hash + '_' + Path(relative_path).name
    
    # Change extension to .jpg for thumbnails
    thumbnail_name = Path(path_str).stem + '.jpg'
    
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
        
        # Verify image is valid before processing
        try:
            with Image.open(image_path) as verify_img:
                verify_img.verify()
        except Exception as e:
            logger.error(f"Invalid or corrupted image file: {image_path}: {e}")
            return False
        
        # Reopen for processing (verify() closes the file)
        # Open and process image
        with Image.open(image_path) as img:
            # Create thumbnail (maintains aspect ratio)
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            
            # Convert to RGB if necessary (for formats like PNG with transparency)
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Save thumbnail
            img.save(thumbnail_path, format='JPEG', quality=quality)
            
            logger.debug(f"Generated thumbnail: {thumbnail_path}")
            return True
            
    except Exception as e:
        logger.error(f"Error generating thumbnail for {image_path}: {e}", exc_info=True)
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


def generate_thumbnails_for_user(
    user_path: Path,
    max_size: Tuple[int, int] = DEFAULT_THUMBNAIL_SIZE,
    progress_callback: Optional[Callable[[int, int], None]] = None
) -> Tuple[int, int]:
    """
    Generate thumbnails for all images in a user's directory.
    
    Args:
        user_path: The user's root directory
        max_size: Maximum size for thumbnails
        progress_callback: Optional callback function(current, total) for progress updates
    
    Returns:
        Tuple of (successful_count, failed_count)
    """
    if not user_path.exists() or not user_path.is_dir():
        logger.warning(f"User path does not exist: {user_path}")
        return (0, 0)
    
    # Find all image files (single traversal)
    def _is_in_thumbnails_dir(path: Path, base_path: Path) -> bool:
        """Check if path is within .thumbnails directory."""
        try:
            relative = path.relative_to(base_path)
            return '.thumbnails' in relative.parts
        except ValueError:
            return False
    
    image_files = []
    for path in user_path.rglob('*'):
        if path.is_file() and is_image_file(path) and not _is_in_thumbnails_dir(path, user_path):
            image_files.append(path)
    
    total = len(image_files)
    successful = 0
    failed = 0
    
    logger.info(f"Generating thumbnails for {total} images in {user_path}")
    
    for i, image_path in enumerate(image_files):
        try:
            thumbnail_path = get_thumbnail_path(user_path, image_path)
            
            # Skip if thumbnail already exists and is newer than image
            if thumbnail_path.exists():
                if thumbnail_path.stat().st_mtime >= image_path.stat().st_mtime:
                    successful += 1
                    if progress_callback:
                        progress_callback(i + 1, total)
                    continue
            
            if generate_thumbnail_file(image_path, thumbnail_path, max_size):
                successful += 1
            else:
                failed += 1
                
        except Exception as e:
            logger.error(f"Error processing {image_path}: {e}")
            failed += 1
        
        if progress_callback:
            progress_callback(i + 1, total)
    
    logger.info(f"Thumbnail generation complete: {successful} successful, {failed} failed")
    return (successful, failed)


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
