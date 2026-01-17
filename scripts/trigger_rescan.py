#!/usr/bin/env python3
"""Trigger file scan with EXIF restoration on storage server."""
import sys
import os
sys.path.insert(0, '/home/verita84/posterchanai')
os.chdir('/home/verita84/posterchanai')

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import User, Setting
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def trigger_rescan():
    """Trigger file rescan for all users."""
    from app.services.storage_service import get_storage_service
    from app.routers.files import get_file_cache
    from app.utils.exif_utils import batch_restore_timestamps
    
    # Create database session
    engine = create_engine('sqlite:///db.sqlite')
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    
    try:
        # Get all users
        users = db.query(User).all()
        logger.info(f"Found {len(users)} users to scan")
        
        # Scan each user
        for user in users:
            logger.info(f"Scanning files for user: {user.username}")
            
            # Define the scan function
            def _rescan_user_files_local(user):
                try:
                    storage = get_storage_service(db)
                    user_path = storage.get_user_path(user.username)
                    
                    # Invalidate file cache for this user
                    cache = get_file_cache(db)
                    cache.invalidate(f"{user.username}:")
                    
                    # Walk through all files
                    file_count = 0
                    dir_count = 0
                    exif_stats = {'restored': 0, 'processed': 0}
                    
                    if user_path.exists():
                        # First, restore EXIF timestamps for all media files
                        logger.info(f"[Storage Rescan] Restoring EXIF timestamps for user {user.username}")
                        exif_stats = batch_restore_timestamps(user_path)
                        logger.info(f"[EXIF] Stats: {exif_stats}")
                        
                        # Then count files
                        for item in user_path.rglob('*'):
                            try:
                                if item.is_file():
                                    file_count += 1
                                elif item.is_dir():
                                    dir_count += 1
                            except Exception as e:
                                logger.warning(f"Error processing {item} for user {user.username}: {e}")
                                continue
                    
                    logger.info(f"[Storage Rescan] User {user.username}: {file_count} files, {dir_count} directories")
                    return {
                        "user_id": user.id,
                        "username": user.username,
                        "files": file_count,
                        "directories": dir_count,
                        "exif_restored": exif_stats.get('restored', 0),
                        "exif_processed": exif_stats.get('processed', 0),
                        "status": "success"
                    }
                except Exception as e:
                    logger.error(f"[Storage Rescan] Error rescanning user {user.username}: {e}", exc_info=True)
                    return {
                        "user_id": user.id,
                        "username": user.username,
                        "status": "error",
                        "error": str(e)
                    }
            
            # Run in thread pool
            result = await asyncio.to_thread(_rescan_user_files_local, user)
            logger.info(f"Result: {result}")
        
        logger.info("File scan complete for all users!")
        
    finally:
        db.close()

if __name__ == "__main__":
    asyncio.run(trigger_rescan())
