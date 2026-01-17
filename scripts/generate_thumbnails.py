#!/usr/bin/env python3
"""
Batch Thumbnail Generator for Posterchan AI
Generates thumbnails for all images and videos that don't have them yet.
Run this after rsync'ing files to generate previews for the Photo Gallery.
"""
import sys
import os
from pathlib import Path
import asyncio
import logging

# Add parent directory to path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.thumbnail_service import (
    generate_thumbnail_for_image,
    generate_thumbnail_for_video_file,
    get_thumbnail_if_exists,
    is_image_file,
    is_video_file,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def find_media_files(user_path: Path):
    """Find all image and video files in user directory."""
    media_files = []
    
    logger.info(f"Scanning for media files in: {user_path}")
    
    for item in user_path.rglob('*'):
        if item.is_file():
            # Skip hidden files and thumbnails directory
            if item.name.startswith('.') or '.thumbnails' in str(item):
                continue
            
            if is_image_file(item) or is_video_file(item):
                media_files.append(item)
    
    logger.info(f"Found {len(media_files)} media files")
    return media_files


def generate_thumbnails_batch(user_path: Path, media_files: list):
    """Generate thumbnails for media files that don't have them."""
    
    total = len(media_files)
    generated = 0
    skipped = 0
    failed = 0
    
    logger.info(f"Starting thumbnail generation for {total} files...")
    logger.info("=" * 60)
    
    for idx, media_file in enumerate(media_files, 1):
        # Check if thumbnail already exists
        thumbnail_path = get_thumbnail_if_exists(user_path, media_file)
        
        if thumbnail_path and thumbnail_path.exists():
            skipped += 1
            if idx % 100 == 0:
                logger.info(f"Progress: {idx}/{total} ({generated} generated, {skipped} skipped)")
            continue
        
        # Generate thumbnail
        try:
            if is_image_file(media_file):
                success = generate_thumbnail_for_image(user_path, media_file)
                media_type = "image"
            else:
                success = generate_thumbnail_for_video_file(user_path, media_file)
                media_type = "video"
            
            if success:
                generated += 1
                logger.info(f"✓ [{idx}/{total}] Generated thumbnail for {media_type}: {media_file.name}")
            else:
                failed += 1
                logger.warning(f"✗ [{idx}/{total}] Failed to generate thumbnail for {media_type}: {media_file.name}")
                
        except Exception as e:
            failed += 1
            logger.error(f"✗ [{idx}/{total}] Error generating thumbnail for {media_file.name}: {e}")
        
        # Progress update every 50 files
        if idx % 50 == 0:
            logger.info(f"Progress: {idx}/{total} ({generated} generated, {skipped} skipped, {failed} failed)")
    
    logger.info("=" * 60)
    logger.info("Thumbnail Generation Complete!")
    logger.info(f"Total files processed: {total}")
    logger.info(f"Thumbnails generated: {generated}")
    logger.info(f"Already existed (skipped): {skipped}")
    logger.info(f"Failed: {failed}")
    logger.info("=" * 60)


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python3 generate_thumbnails.py <user_directory>")
        print("Example: python3 generate_thumbnails.py /var/lib/posterchanai/verita84@poster.place")
        sys.exit(1)
    
    user_path = Path(sys.argv[1])
    
    if not user_path.exists():
        logger.error(f"Directory does not exist: {user_path}")
        sys.exit(1)
    
    if not user_path.is_dir():
        logger.error(f"Not a directory: {user_path}")
        sys.exit(1)
    
    logger.info("Posterchan AI - Batch Thumbnail Generator")
    logger.info(f"User directory: {user_path}")
    logger.info("")
    
    # Find all media files
    media_files = find_media_files(user_path)
    
    if not media_files:
        logger.warning("No media files found!")
        sys.exit(0)
    
    # Generate thumbnails
    generate_thumbnails_batch(user_path, media_files)
    
    logger.info("")
    logger.info("Done! Thumbnails are stored in .thumbnails/ subdirectories")
    logger.info("Refresh your browser to see the new previews in Photo Gallery")


if __name__ == "__main__":
    main()
