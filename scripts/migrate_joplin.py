#!/usr/bin/env python3
"""
Joplin to Posterchanai Notes Migration Script

This script migrates notes from Joplin to Posterchanai's notes system.

Usage:
    python scripts/migrate_joplin.py --joplin-db /path/to/joplin/database.sqlite --user-id 1

Joplin database location:
    - Linux: ~/.config/joplin-desktop/database.sqlite
    - Windows: %APPDATA%\Joplin\database.sqlite
    - macOS: ~/Library/Application Support/Joplin/database.sqlite
"""
import argparse
import sqlite3
import sys
import os
import json
import shutil
from pathlib import Path
from datetime import datetime
import re

# Add parent directory to path to import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal, init_db
from app.models import User, Note, NoteFolder, Setting
from app.services.storage_service import StorageService


def find_joplin_db():
    """Try to find Joplin database in common locations."""
    possible_paths = [
        Path.home() / ".config" / "joplin-desktop" / "database.sqlite",
        Path.home() / ".config" / "joplin" / "database.sqlite",
        Path.home() / "Library" / "Application Support" / "Joplin" / "database.sqlite",
        Path(os.environ.get("APPDATA", "")) / "Joplin" / "database.sqlite",
    ]
    
    for path in possible_paths:
        if path.exists():
            return str(path)
    
    return None


def find_joplin_resources_dir(joplin_db_path):
    """Find Joplin resources directory (usually next to database.sqlite)."""
    db_path = Path(joplin_db_path)
    # Resources are usually in the same directory as database.sqlite
    possible_dirs = [
        db_path.parent / "resources",
        db_path.parent.parent / "resources",
    ]
    
    for res_dir in possible_dirs:
        if res_dir.exists() and res_dir.is_dir():
            return res_dir
    
    return None


def sanitize_title(title):
    """Sanitize note title (remove markdown headers if present)."""
    if not title:
        return "Untitled"
    # Remove markdown headers
    title = re.sub(r'^#+\s+', '', title)
    # Remove leading/trailing whitespace
    title = title.strip()
    return title or "Untitled"


def parse_joplin_timestamp(timestamp):
    """Convert Joplin timestamp (milliseconds) to datetime."""
    if not timestamp:
        return datetime.utcnow()
    try:
        # Joplin uses milliseconds since epoch
        return datetime.fromtimestamp(timestamp / 1000.0)
    except (ValueError, TypeError):
        return datetime.utcnow()


def migrate_joplin(joplin_db_path, user_id, dry_run=False):
    """Migrate notes from Joplin database to Posterchanai."""
    
    # Check if Joplin database exists
    if not os.path.exists(joplin_db_path):
        print(f"Error: Joplin database not found at {joplin_db_path}")
        return False
    
    # Initialize Posterchanai database
    init_db()
    db = SessionLocal()
    
    try:
        # Verify user exists
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            print(f"Error: User with ID {user_id} not found")
            return False
        
        print(f"Migrating Joplin notes for user: {user.username} (ID: {user_id})")
        if dry_run:
            print("DRY RUN MODE - No changes will be made")
        
        # Connect to Joplin database
        joplin_conn = sqlite3.connect(joplin_db_path)
        joplin_cursor = joplin_conn.cursor()
        
        # Create folder mapping (Joplin folder_id -> Posterchanai folder_id)
        folder_map = {}
        
        # Migrate folders first
        print("\nMigrating folders...")
        joplin_cursor.execute("""
            SELECT id, parent_id, title, created_time, updated_time
            FROM folders
            ORDER BY parent_id NULLS FIRST, id
        """)
        
        folders = joplin_cursor.fetchall()
        root_folders = [f for f in folders if not f[1]]  # parent_id is None
        child_folders = [f for f in folders if f[1]]  # has parent
        
        # Migrate root folders first
        for folder_id, parent_id, title, created_time, updated_time in root_folders:
            if not title:
                title = "Unnamed Folder"
            
            if not dry_run:
                folder = NoteFolder(
                    user_id=user_id,
                    name=title,
                    parent_id=None,
                    created_at=parse_joplin_timestamp(created_time),
                    updated_at=parse_joplin_timestamp(updated_time)
                )
                db.add(folder)
                db.flush()  # Get the ID
                folder_map[folder_id] = folder.id
                print(f"  Created folder: {title} (ID: {folder.id})")
            else:
                print(f"  Would create folder: {title}")
        
        # Migrate child folders (process in order of parent depth)
        processed = set()
        max_iterations = len(child_folders) * 2  # Safety limit
        iteration = 0
        
        while child_folders and iteration < max_iterations:
            iteration += 1
            remaining = []
            
            for folder_id, parent_id, title, created_time, updated_time in child_folders:
                if parent_id in folder_map:
                    if not title:
                        title = "Unnamed Folder"
                    
                    if not dry_run:
                        folder = NoteFolder(
                            user_id=user_id,
                            name=title,
                            parent_id=folder_map[parent_id],
                            created_at=parse_joplin_timestamp(created_time),
                            updated_at=parse_joplin_timestamp(updated_time)
                        )
                        db.add(folder)
                        db.flush()
                        folder_map[folder_id] = folder.id
                        print(f"  Created folder: {title} (ID: {folder.id}, parent: {folder_map[parent_id]})")
                    else:
                        print(f"  Would create folder: {title} (parent: {parent_id})")
                    
                    processed.add(folder_id)
                else:
                    remaining.append((folder_id, parent_id, title, created_time, updated_time))
            
            child_folders = remaining
            if len(processed) == 0:
                # Circular reference or missing parent - create as root
                for folder_id, parent_id, title, created_time, updated_time in child_folders:
                    if not title:
                        title = "Unnamed Folder"
                    
                    if not dry_run:
                        folder = NoteFolder(
                            user_id=user_id,
                            name=title,
                            parent_id=None,
                            created_at=parse_joplin_timestamp(created_time),
                            updated_at=parse_joplin_timestamp(updated_time)
                        )
                        db.add(folder)
                        db.flush()
                        folder_map[folder_id] = folder.id
                        print(f"  Created folder (orphaned): {title} (ID: {folder.id})")
                    else:
                        print(f"  Would create folder (orphaned): {title}")
                break
        
        if not dry_run:
            db.commit()
        
        # Load resources metadata from Joplin
        print("\nLoading resources (all file types)...")
        joplin_cursor.execute("""
            SELECT id, title, mime, file_extension
            FROM resources
        """)
        resources = {row[0]: {'title': row[1], 'mime': row[2], 'ext': row[3]} 
                    for row in joplin_cursor.fetchall()}
        print(f"  Found {len(resources)} resources in database")
        
        # Count by type
        type_counts = {}
        for res in resources.values():
            mime = res['mime'] or 'unknown'
            mime_type = mime.split('/')[0] if '/' in mime else 'unknown'
            type_counts[mime_type] = type_counts.get(mime_type, 0) + 1
        if type_counts:
            type_str = ', '.join([f"{count} {typ}" for typ, count in type_counts.items()])
            print(f"  Types: {type_str}")
        
        # Find Joplin resources directory
        joplin_resources_dir = find_joplin_resources_dir(joplin_db_path)
        if joplin_resources_dir:
            print(f"  Found resources directory: {joplin_resources_dir}")
            # Verify it exists and is readable
            if not os.path.exists(joplin_resources_dir):
                print(f"  WARNING: Resources directory does not exist: {joplin_resources_dir}")
                joplin_resources_dir = None
            elif not os.access(joplin_resources_dir, os.R_OK):
                print(f"  WARNING: Resources directory is not readable: {joplin_resources_dir}")
                joplin_resources_dir = None
            else:
                # Count files in resources directory
                resource_count = len(list(Path(joplin_resources_dir).iterdir())) if Path(joplin_resources_dir).exists() else 0
                print(f"  Resources directory contains {resource_count} files")
        else:
            print(f"  WARNING: Could not find Joplin resources directory")
        else:
            print(f"  WARNING: Resources directory not found. Attachments may not be migrated.")
        
        # Initialize storage service for saving attachments
        storage_service = None
        if not dry_run:
            storage_service = StorageService(db)
        
        # Migrate notes
        print("\nMigrating notes...")
        joplin_cursor.execute("""
            SELECT id, parent_id, title, body, created_time, updated_time, is_todo, todo_completed
            FROM notes
            ORDER BY created_time
        """)
        
        notes = joplin_cursor.fetchall()
        notes_migrated = 0
        notes_skipped = 0
        attachments_migrated = 0
        
        for note_id, parent_id, title, body, created_time, updated_time, is_todo, todo_completed in notes:
            # Skip if it's a todo item (optional - you can change this)
            if is_todo:
                notes_skipped += 1
                continue
            
            # Get tags for this note
            joplin_cursor.execute("""
                SELECT t.title
                FROM tags t
                JOIN note_tags nt ON t.id = nt.tag_id
                WHERE nt.note_id = ?
            """, (note_id,))
            tags = [row[0] for row in joplin_cursor.fetchall()]
            tags_str = ", ".join(tags) if tags else None
            
            # Determine folder
            folder_id = None
            if parent_id and parent_id in folder_map:
                folder_id = folder_map[parent_id]
            elif parent_id:
                # Parent folder not found, skip folder assignment
                pass
            
            # Sanitize title
            clean_title = sanitize_title(title)
            
            # Extract and migrate attachments
            note_content = body or ""
            attachment_filenames = []
            resource_id_pattern = r'\]\(:/([a-f0-9]{32})\)'
            
            # Create note first (needed to get ID for attachment storage)
            if not dry_run:
                note = Note(
                    user_id=user_id,
                    folder_id=folder_id,
                    title=clean_title,
                    content=note_content,  # Will update after migrating attachments
                    tags=tags_str,
                    is_pinned=False,
                    created_at=parse_joplin_timestamp(created_time),
                    updated_at=parse_joplin_timestamp(updated_time)
                )
                db.add(note)
                db.flush()  # Get the note ID
            else:
                note = None  # Placeholder for dry-run
            
            # Process attachments if resources directory exists
            if note_content and joplin_resources_dir:
                # Find all resource references in note content
                resource_ids = re.findall(resource_id_pattern, note_content)
                
                if resource_ids:
                    print(f"  Note '{clean_title}': Found {len(resource_ids)} attachment(s)")
                    print(f"    Resource IDs: {resource_ids[:5]}{'...' if len(resource_ids) > 5 else ''}")
                else:
                    print(f"  Note '{clean_title}': No attachments found in content")
            elif not joplin_resources_dir:
                print(f"  Note '{clean_title}': WARNING - Joplin resources directory not found, skipping attachments")
                    
                    updated_content = note_content
                    for resource_id in resource_ids:
                        if resource_id in resources:
                            resource_info = resources[resource_id]
                            # Determine file extension
                            ext = resource_info['ext'] or ''
                            if not ext and resource_info['mime']:
                                # Guess extension from mime type (comprehensive list)
                                mime_to_ext = {
                                    # Images
                                    'image/png': '.png',
                                    'image/jpeg': '.jpg',
                                    'image/jpg': '.jpg',
                                    'image/gif': '.gif',
                                    'image/webp': '.webp',
                                    'image/svg+xml': '.svg',
                                    'image/bmp': '.bmp',
                                    'image/tiff': '.tiff',
                                    'image/x-icon': '.ico',
                                    # Documents
                                    'application/pdf': '.pdf',
                                    'application/msword': '.doc',
                                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
                                    'application/vnd.ms-excel': '.xls',
                                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
                                    'application/vnd.ms-powerpoint': '.ppt',
                                    'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
                                    'application/vnd.oasis.opendocument.text': '.odt',
                                    'application/vnd.oasis.opendocument.spreadsheet': '.ods',
                                    'application/vnd.oasis.opendocument.presentation': '.odp',
                                    # Text
                                    'text/plain': '.txt',
                                    'text/markdown': '.md',
                                    'text/html': '.html',
                                    'text/css': '.css',
                                    'text/javascript': '.js',
                                    'application/json': '.json',
                                    'text/xml': '.xml',
                                    # Archives
                                    'application/zip': '.zip',
                                    'application/x-rar-compressed': '.rar',
                                    'application/x-tar': '.tar',
                                    'application/gzip': '.gz',
                                    'application/x-7z-compressed': '.7z',
                                    # Audio
                                    'audio/mpeg': '.mp3',
                                    'audio/wav': '.wav',
                                    'audio/ogg': '.ogg',
                                    'audio/mp4': '.m4a',
                                    'audio/x-m4a': '.m4a',
                                    'audio/webm': '.webm',
                                    # Video
                                    'video/mp4': '.mp4',
                                    'video/mpeg': '.mpeg',
                                    'video/quicktime': '.mov',
                                    'video/x-msvideo': '.avi',
                                    'video/webm': '.webm',
                                    'video/x-matroska': '.mkv',
                                    # Code
                                    'text/x-python': '.py',
                                    'text/x-java': '.java',
                                    'text/x-c': '.c',
                                    'text/x-c++': '.cpp',
                                    'text/x-csharp': '.cs',
                                    'application/x-sh': '.sh',
                                    # Other
                                    'application/octet-stream': '.bin',
                                }
                                ext = mime_to_ext.get(resource_info['mime'], '')
                                # If still no extension, try to extract from mime type
                                if not ext and '/' in resource_info['mime']:
                                    mime_parts = resource_info['mime'].split('/')
                                    if len(mime_parts) == 2:
                                        # Use subtype as fallback (e.g., 'vnd.ms-excel' -> '.xls')
                                        subtype = mime_parts[1]
                                        if subtype.startswith('vnd.'):
                                            # Try common office formats
                                            if 'word' in subtype or 'document' in subtype:
                                                ext = '.docx'
                                            elif 'excel' in subtype or 'spreadsheet' in subtype:
                                                ext = '.xlsx'
                                            elif 'powerpoint' in subtype or 'presentation' in subtype:
                                                ext = '.pptx'
                                        else:
                                            ext = f'.{subtype.split("+")[0]}'  # Remove +xml, etc.
                            
                            # Find resource file - try multiple possible locations/extensions
                            resource_file = None
                            possible_paths = [
                                joplin_resources_dir / resource_id,  # No extension
                                joplin_resources_dir / f"{resource_id}{ext}",  # With extension from DB
                            ]
                            
                            # Also try common extensions if we have a mime type
                            if resource_info['mime']:
                                mime_base = resource_info['mime'].split('/')[1].split('+')[0]
                                possible_paths.append(joplin_resources_dir / f"{resource_id}.{mime_base}")
                            
                            # Try all possible paths
                            for path in possible_paths:
                                if path.exists():
                                    resource_file = path
                                    break
                            
                            if resource_file and resource_file.exists():
                                if not dry_run:
                                    try:
                                        # Copy resource file to posterchanai storage
                                        with open(resource_file, 'rb') as f:
                                            file_data = f.read()
                                        
                                        # Determine original filename with proper extension
                                        original_name = resource_info['title'] or f"resource_{resource_id[:8]}"
                                        
                                        # Ensure original name has extension
                                        if not Path(original_name).suffix and ext:
                                            original_name = f"{original_name}{ext}"
                                        elif not Path(original_name).suffix:
                                            # Try to get extension from actual file
                                            file_ext = resource_file.suffix
                                            if file_ext:
                                                original_name = f"{original_name}{file_ext}"
                                        
                                        # Save attachment
                                        print(f"    Saving attachment to: {user.username}/notes/{note.id}/")
                                        try:
                                            filename = storage_service.save_note_attachment(
                                                user.username, note.id, file_data, original_name
                                            )
                                            attachment_filenames.append(filename)
                                            attachments_migrated += 1
                                            
                                            # Update note content to reference new file
                                            # Replace ](:/resource_id) with ](/api/notes/files/username/note_id/filename)
                                            old_ref = f"](:/{resource_id})"
                                            new_ref = f"](/api/notes/files/{user.username}/{note.id}/{filename})"
                                            updated_content = updated_content.replace(old_ref, new_ref)
                                            
                                            # Get file size for display
                                            file_size = len(file_data)
                                            size_str = f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024 else f"{file_size / (1024 * 1024):.1f} MB"
                                            
                                            # Verify file was actually written
                                            note_path = storage_service.get_note_path(user.username, note.id)
                                            saved_file = note_path / filename
                                            if saved_file.exists():
                                                print(f"    ✓ Migrated: {original_name} ({size_str}) -> {saved_file}")
                                            else:
                                                print(f"    ⚠ WARNING: File saved but not found at {saved_file}")
                                        except Exception as save_error:
                                            print(f"    ERROR saving attachment: {save_error}")
                                            import traceback
                                            traceback.print_exc()
                                            raise
                                    except Exception as e:
                                        print(f"    ERROR migrating attachment {resource_id}: {e}")
                                        # Keep original reference if migration fails
                                else:
                                    print(f"    Would migrate attachment: {resource_info['title'] or resource_id}")
                                    attachment_filenames.append(f"resource_{resource_id[:8]}{ext}")
                            else:
                                print(f"    WARNING: Resource file not found: {resource_file}")
                        else:
                            print(f"    WARNING: Resource {resource_id} not found in resources table")
                    
                    # Update note content with migrated attachment references
                    if not dry_run and updated_content != note_content:
                        note.content = updated_content
                else:
                    if dry_run:
                        print(f"  Would create note: {clean_title}")
            
            # Store attachment list if any
            if attachment_filenames and not dry_run:
                note.attachments = json.dumps(attachment_filenames)
            elif dry_run and not resource_ids:
                print(f"  Would create note: {clean_title}")
            
            notes_migrated += 1
        
        if not dry_run:
            db.commit()
            print(f"\nMigration complete!")
            print(f"  Folders migrated: {len(folder_map)}")
            print(f"  Notes migrated: {notes_migrated}")
            print(f"  Notes skipped (todos): {notes_skipped}")
            print(f"  Attachments migrated: {attachments_migrated}")
        else:
            print(f"\nDry run complete!")
            print(f"  Would migrate folders: {len(folder_map)}")
            print(f"  Would migrate notes: {notes_migrated}")
            print(f"  Would skip notes (todos): {notes_skipped}")
            print(f"  Would migrate attachments: {attachments_migrated}")
        
        joplin_conn.close()
        return True
        
    except Exception as e:
        db.rollback()
        print(f"Error during migration: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate notes from Joplin to Posterchanai")
    parser.add_argument(
        "--joplin-db",
        type=str,
        help="Path to Joplin database.sqlite file"
    )
    parser.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Posterchanai user ID to migrate notes to"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a dry run without making changes"
    )
    
    args = parser.parse_args()
    
    # Find Joplin database if not provided
    joplin_db_path = args.joplin_db
    if not joplin_db_path:
        found_path = find_joplin_db()
        if found_path:
            print(f"Found Joplin database at: {found_path}")
            joplin_db_path = found_path
        else:
            print("Error: Joplin database not found. Please specify --joplin-db")
            print("\nCommon locations:")
            print("  Linux: ~/.config/joplin-desktop/database.sqlite")
            print("  Windows: %APPDATA%\\Joplin\\database.sqlite")
            print("  macOS: ~/Library/Application Support/Joplin/database.sqlite")
            return 1
    
    if not os.path.exists(joplin_db_path):
        print(f"Error: Joplin database not found at {joplin_db_path}")
        return 1
    
    success = migrate_joplin(joplin_db_path, args.user_id, dry_run=args.dry_run)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
