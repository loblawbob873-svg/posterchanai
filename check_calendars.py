#!/usr/bin/env python3
"""Check what calendars exist for a user."""
import sys
sys.path.insert(0, '/home/verita84/posterchanai')

from app.database import SessionLocal
from app.models import User
from app.services.storage_service import get_storage_service
from app.services.dav_storage_proxy import DAVStorageProxy
from pathlib import Path

db = SessionLocal()
username = 'verita84@poster.place'

# Get user
user = db.query(User).filter(User.username == username).first()
if not user:
    print(f"User {username} not found")
    sys.exit(1)

print(f"User: {user.username}")
print(f"User ID: {user.id}")

# Get storage service
storage = get_storage_service(db)
user_path = storage.get_user_path(username)
caldav_path = user_path / 'caldav'

print(f"\nUser path: {user_path}")
print(f"CalDAV path: {caldav_path}")
print(f"CalDAV path exists: {caldav_path.exists()}")

# Check local filesystem
if caldav_path.exists():
    items = list(caldav_path.iterdir())
    print(f"\nLocal filesystem - Total items: {len(items)}")
    dirs = [i for i in items if i.is_dir()]
    files = [i for i in items if i.is_file()]
    print(f"Directories: {[str(d.name) for d in dirs]}")
    print(f"Files: {[str(f.name) for f in files]}")
    
    # Check each directory
    for d in dirs:
        ics_files = list((caldav_path / d.name).glob("*.ics"))
        print(f"  {d.name}/: {len(ics_files)} .ics files")

# Check via storage proxy
print(f"\n--- Storage Proxy Check ---")
proxy = DAVStorageProxy(db, username, 'caldav')
file_items = proxy.list_files("")
print(f"Storage proxy returned {len(file_items)} items")

calendar_dirs = []
ics_files = []
for item in file_items:
    name = item.get('name', '')
    is_dir = item.get('is_directory', False)
    if is_dir and not name.startswith('.'):
        calendar_dirs.append(name)
        print(f"  Directory: {name}")
    elif name.endswith('.ics'):
        ics_files.append(name)
        print(f"  File: {name}")

print(f"\nSummary:")
print(f"  Calendar directories (via proxy): {calendar_dirs}")
print(f"  Loose .ics files (via proxy): {len(ics_files)}")

db.close()
