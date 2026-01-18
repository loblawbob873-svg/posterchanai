#!/usr/bin/env python3
"""
Migrate CalDAV/CardDAV data from local filesystem to storage server.
This script copies all .ics and .vcf files from local storage to the storage server.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from app.database import SessionLocal
from app.models import User, Setting
from app.services.storage_service import get_storage_service
import httpx
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def migrate_user_dav_data(username: str, dav_type: str, db: SessionLocal):
    """Migrate a user's DAV data to storage server."""
    # Get storage server config
    storage_url_setting = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    storage_token_setting = db.query(Setting).filter(Setting.key == "storage_server_token").first()
    
    if not storage_url_setting or not storage_url_setting.value:
        logger.error("Storage server URL not configured")
        return False
    
    storage_url = storage_url_setting.value
    storage_token = storage_token_setting.value if storage_token_setting else None
    
    # Get local storage path
    storage = get_storage_service(db)
    user_path = storage.get_user_path(username)
    dav_path = user_path / dav_type
    
    if not dav_path.exists():
        logger.info(f"No {dav_type} directory found for {username}")
        return True
    
    logger.info(f"Migrating {dav_type} data for {username} from {dav_path} to {storage_url}")
    
    # Get auth headers
    headers = {}
    if storage_token:
        headers["Authorization"] = f"Bearer {storage_token}"
    
    # Count files to migrate
    total_files = 0
    migrated = 0
    errors = 0
    
    # Walk through all files and directories
    for root, dirs, files in os.walk(dav_path):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        for file in files:
            # Only migrate .ics (CalDAV) or .vcf (CardDAV) files
            if dav_type == 'caldav' and not file.endswith('.ics'):
                continue
            if dav_type == 'cardav' and not file.endswith('.vcf'):
                continue
            
            file_path = Path(root) / file
            # Get relative path from dav_path
            rel_path = file_path.relative_to(dav_path)
            
            # Build storage server path: caldav/subdir/file.ics or cardav/subdir/file.vcf
            storage_path = f"{dav_type}/{rel_path}"
            
            total_files += 1
            
            try:
                # Read file content
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Save to storage server
                save_url = f"{storage_url}/api/storage/save-text-file"
                payload = {
                    "username": username,
                    "path": storage_path,
                    "content": content
                }
                
                with httpx.Client(timeout=30.0) as client:
                    response = client.post(save_url, data=payload, headers=headers)
                    if response.status_code in (200, 201):
                        migrated += 1
                        if migrated % 100 == 0:
                            logger.info(f"Migrated {migrated}/{total_files} files...")
                    else:
                        logger.error(f"Failed to migrate {storage_path}: {response.status_code} - {response.text[:200]}")
                        errors += 1
            except Exception as e:
                logger.error(f"Error migrating {file_path}: {e}")
                errors += 1
    
    logger.info(f"Migration complete for {username} {dav_type}: {migrated} migrated, {errors} errors, {total_files} total")
    return errors == 0

def main():
    """Migrate all users' DAV data."""
    db = SessionLocal()
    
    try:
        # Get all users
        users = db.query(User).all()
        logger.info(f"Found {len(users)} users to migrate")
        
        for user in users:
            logger.info(f"\n=== Migrating {user.username} ===")
            
            # Migrate CalDAV
            migrate_user_dav_data(user.username, 'caldav', db)
            
            # Migrate CardDAV
            migrate_user_dav_data(user.username, 'cardav', db)
        
        logger.info("\n=== Migration complete ===")
    finally:
        db.close()

if __name__ == "__main__":
    main()
