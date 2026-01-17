"""
Utility functions for validating and cleaning image data dictionaries.
Used to ensure consistent validation across different endpoints.
"""
from typing import Optional, Dict, Any
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def validate_and_clean_image_data(img: Any, item_path: Any = None) -> Optional[Dict[str, Any]]:
    """
    Validate and clean a single image data dictionary.
    
    Args:
        img: Image data dictionary (or any type - will be validated)
        item_path: Optional path to the file item (for logging purposes)
    
    Returns:
        Cleaned image dictionary with guaranteed 'name' and 'path' fields as strings,
        or None if the image data is invalid.
    """
    # Ensure img is a dictionary
    if not isinstance(img, dict):
        logger.error(f"[VALIDATION] Filtering out non-dict image item (type: {type(img).__name__}): {str(img)[:100]}")
        return None
    
    # Ensure path exists - this is required
    if 'path' not in img:
        logger.error(f"[VALIDATION] Image missing 'path' key, keys present: {list(img.keys())}, item: {item_path}")
        return None
    
    # Get path value and validate
    path_value = img.get('path')
    if path_value is None:
        logger.error(f"[VALIDATION] Image has None path, item: {item_path}")
        return None
    
    # Convert path to string and validate
    path_str = str(path_value).strip()
    if path_str == '':
        logger.error(f"[VALIDATION] Image has empty path after strip, item: {item_path}, original: '{path_value}'")
        return None
    
    if path_str == 'undefined':
        logger.error(f"[VALIDATION] Image has 'undefined' path, item: {item_path}")
        return None
    
    # Path is valid - update img
    img['path'] = path_str
    
    # Ensure name exists, extract from path if missing
    if 'name' not in img or not img['name']:
        # Extract from path
        img['name'] = path_str.split('/')[-1] if '/' in path_str else path_str
        logger.debug(f"[VALIDATION] Extracted name from path: {img['name']}")
    else:
        # Validate and convert name to string
        name_str = str(img['name']).strip()
        if name_str == '' or name_str == 'undefined':
            # Use path fallback
            img['name'] = path_str.split('/')[-1] if '/' in path_str else path_str
            logger.debug(f"[VALIDATION] Name was invalid, extracted from path: {img['name']}")
        else:
            img['name'] = name_str
    
    # Ensure both are strings (redundant but safe)
    img['name'] = str(img['name'])
    img['path'] = str(img['path'])
    
    # Log successful validation for first few
    logger.debug(f"[VALIDATION] ✓ Valid image: name={img['name']}, path={img['path'][:50]}...")
    
    return img


def validate_and_filter_images(images: list, source: str = "unknown") -> list:
    """
    Validate and filter a list of image dictionaries.
    
    Args:
        images: List of image data dictionaries
        source: Source identifier for logging (e.g., "proxy", "local")
    
    Returns:
        List of validated and cleaned image dictionaries
    """
    if not images:
        return []
    
    valid_images = []
    original_count = len(images)
    
    for img in images:
        cleaned = validate_and_clean_image_data(img)
        if cleaned:
            valid_images.append(cleaned)
    
    filtered_count = original_count - len(valid_images)
    if filtered_count > 0:
        logger.warning(f"[{source.upper()}] Filtered {filtered_count} invalid images (kept {len(valid_images)}/{original_count})")
    
    if len(valid_images) == 0 and original_count > 0:
        logger.error(f"[{source.upper()}] WARNING: All {original_count} images were filtered out! This indicates a data format issue.")
        # Log first few invalid images for debugging
        for i, invalid_img in enumerate(images[:5]):
            logger.error(f"[{source.upper()}] Invalid image {i+1}: type={type(invalid_img).__name__}, value={str(invalid_img)[:200]}")
    
    return valid_images


def ensure_serializable_image(img: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure an image dictionary is fully JSON serializable with correct types.
    
    Args:
        img: Image dictionary to clean
    
    Returns:
        Cleaned image dictionary with all values as JSON-serializable types
    """
    serializable_img = {}
    
    # Convert all values to JSON-serializable types
    for key, value in img.items():
        if key == "thumbnail":
            # Skip thumbnails - they're loaded on-demand
            continue
        elif isinstance(value, bytes):
            # Convert bytes to string
            serializable_img[key] = value.decode('utf-8', errors='ignore')
        elif isinstance(value, (Path, type(None))):
            # Convert Path objects to string
            serializable_img[key] = str(value) if value else ""
        elif isinstance(value, (int, float)):
            # Numbers are fine
            serializable_img[key] = value
        elif isinstance(value, bool):
            # Booleans are fine
            serializable_img[key] = value
        elif isinstance(value, str):
            # Strings are fine
            serializable_img[key] = value
        else:
            # Convert anything else to string
            try:
                serializable_img[key] = str(value)
            except Exception:
                serializable_img[key] = ""
    
    # Ensure required fields exist with correct types
    if "name" not in serializable_img:
        serializable_img["name"] = ""
    if "path" not in serializable_img:
        serializable_img["path"] = ""
    if "size" not in serializable_img:
        serializable_img["size"] = 0
    if "modified" not in serializable_img:
        serializable_img["modified"] = 0.0
    if "modified_date" not in serializable_img:
        serializable_img["modified_date"] = ""
    if "type" not in serializable_img:
        serializable_img["type"] = "unknown"
    
    # Ensure types are correct with safe conversions
    try:
        serializable_img["name"] = str(serializable_img["name"]) if serializable_img["name"] is not None else ""
    except Exception:
        serializable_img["name"] = ""
    
    try:
        serializable_img["path"] = str(serializable_img["path"]) if serializable_img["path"] is not None else ""
    except Exception:
        serializable_img["path"] = ""
    
    try:
        size_val = serializable_img.get("size", 0)
        if size_val is None:
            serializable_img["size"] = 0
        else:
            serializable_img["size"] = int(float(size_val))  # Convert via float first to handle string numbers
    except (ValueError, TypeError):
        serializable_img["size"] = 0
    
    try:
        modified_val = serializable_img.get("modified", 0)
        if modified_val is None:
            serializable_img["modified"] = 0.0
        else:
            serializable_img["modified"] = float(modified_val)
    except (ValueError, TypeError):
        serializable_img["modified"] = 0.0
    
    try:
        serializable_img["modified_date"] = str(serializable_img["modified_date"]) if serializable_img["modified_date"] is not None else ""
    except Exception:
        serializable_img["modified_date"] = ""
    
    try:
        serializable_img["type"] = str(serializable_img["type"]) if serializable_img["type"] is not None else "unknown"
    except Exception:
        serializable_img["type"] = "unknown"
    
    return serializable_img
