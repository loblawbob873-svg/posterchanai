#!/usr/bin/env python3
"""
Storage Migration Script
Syncs files from a local directory to posterchanai user storage.

Usage:
    python scripts/migrate_storage.py <source_directory> <username> [options]

Options:
    --overwrite    Overwrite existing files (default: skip)
    --dry-run      Show what would be copied without actually copying
    --verbose      Show detailed progress
"""

import os
import sys
import shutil
import argparse
from pathlib import Path
from typing import Optional

# Add parent directory to path to import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal, init_db
from app.services.storage_service import StorageService, _sanitize_path_component, _validate_path_within_base
from app.models import User


def get_user_path(username: str, db) -> Path:
    """Get the storage path for a user."""
    storage_service = StorageService(db)
    return storage_service.get_user_path(username)


def sync_file(source: Path, dest: Path, overwrite: bool = False, dry_run: bool = False, verbose: bool = False) -> bool:
    """Copy a single file, returning True if copied, False if skipped."""
    if dest.exists() and not overwrite:
        if verbose:
            print(f"  SKIP (exists): {dest}")
        return False
    
    if dry_run:
        print(f"  WOULD COPY: {source} -> {dest}")
        return True
    
    try:
        # Ensure destination directory exists
        dest.parent.mkdir(parents=True, exist_ok=True)
        
        # Copy file
        shutil.copy2(source, dest)
        if verbose:
            print(f"  COPIED: {source.name} -> {dest}")
        return True
    except Exception as e:
        print(f"  ERROR copying {source}: {e}")
        return False


def sync_directory(
    source_dir: Path,
    dest_dir: Path,
    overwrite: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    relative_path: str = ""
) -> tuple[int, int, int]:
    """
    Recursively sync a directory.
    Returns: (files_copied, files_skipped, errors)
    """
    files_copied = 0
    files_skipped = 0
    errors = 0
    
    if not source_dir.exists():
        print(f"ERROR: Source directory does not exist: {source_dir}")
        return (0, 0, 1)
    
    if not source_dir.is_dir():
        print(f"ERROR: Source path is not a directory: {source_dir}")
        return (0, 0, 1)
    
    # Process all items in source directory
    for item in source_dir.iterdir():
        # Skip hidden files and directories (optional - can be made configurable)
        if item.name.startswith('.'):
            if verbose:
                print(f"  SKIP (hidden): {item.name}")
            continue
        
        try:
            # Sanitize the path component
            safe_name = _sanitize_path_component(item.name)
            dest_item = dest_dir / safe_name
            
            # Validate destination is within base directory
            if not _validate_path_within_base(dest_item, dest_dir):
                print(f"  ERROR: Invalid path component: {item.name}")
                errors += 1
                continue
            
            if item.is_file():
                # Copy file
                if sync_file(item, dest_item, overwrite, dry_run, verbose):
                    files_copied += 1
                else:
                    files_skipped += 1
            elif item.is_dir():
                # Recursively sync directory
                if verbose:
                    print(f"Entering directory: {relative_path}/{item.name}")
                sub_copied, sub_skipped, sub_errors = sync_directory(
                    item, dest_item, overwrite, dry_run, verbose, f"{relative_path}/{item.name}"
                )
                files_copied += sub_copied
                files_skipped += sub_skipped
                errors += sub_errors
        except ValueError as e:
            # Path sanitization error
            print(f"  ERROR: {item.name}: {e}")
            errors += 1
        except Exception as e:
            print(f"  ERROR processing {item.name}: {e}")
            errors += 1
    
    return (files_copied, files_skipped, errors)


def main():
    parser = argparse.ArgumentParser(
        description="Sync files from a local directory to posterchanai user storage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Sync files to user 'john' (skip existing files)
  python scripts/migrate_storage.py /home/john/documents john

  # Overwrite existing files
  python scripts/migrate_storage.py /home/john/documents john --overwrite

  # Dry run to see what would be copied
  python scripts/migrate_storage.py /home/john/documents john --dry-run --verbose
        """
    )
    parser.add_argument('source', type=str, help='Source directory to sync from')
    parser.add_argument('username', type=str, help='Posterchanai username to sync to')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite existing files (default: skip)')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be copied without actually copying')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show detailed progress')
    
    args = parser.parse_args()
    
    # Validate source directory
    source_path = Path(args.source).resolve()
    if not source_path.exists():
        print(f"ERROR: Source directory does not exist: {source_path}")
        sys.exit(1)
    
    if not source_path.is_dir():
        print(f"ERROR: Source path is not a directory: {source_path}")
        sys.exit(1)
    
    # Initialize database
    print("Initializing database...")
    try:
        init_db()
    except Exception as e:
        print(f"ERROR: Failed to initialize database: {e}")
        sys.exit(1)
    
    # Get database session
    db = SessionLocal()
    try:
        # Check if user exists
        user = db.query(User).filter(User.username == args.username).first()
        if not user:
            print(f"ERROR: User '{args.username}' not found in database")
            print("Available users:")
            users = db.query(User).all()
            for u in users:
                print(f"  - {u.username}")
            sys.exit(1)
        
        # Get user storage path
        print(f"Getting storage path for user '{args.username}'...")
        try:
            user_path = get_user_path(args.username, db)
            print(f"User storage path: {user_path}")
        except Exception as e:
            print(f"ERROR: Failed to get user storage path: {e}")
            sys.exit(1)
        
        # Confirm operation
        if args.dry_run:
            print(f"\nDRY RUN: Would sync files from:")
            print(f"  Source: {source_path}")
            print(f"  Destination: {user_path}")
        else:
            print(f"\nSyncing files from:")
            print(f"  Source: {source_path}")
            print(f"  Destination: {user_path}")
            if args.overwrite:
                print("  Mode: Overwrite existing files")
            else:
                print("  Mode: Skip existing files")
        
        response = input("\nContinue? (y/N): ").strip().lower()
        if response != 'y':
            print("Cancelled.")
            sys.exit(0)
        
        # Perform sync
        print("\nStarting sync...")
        files_copied, files_skipped, errors = sync_directory(
            source_path,
            user_path,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            verbose=args.verbose
        )
        
        # Print summary
        print("\n" + "="*60)
        print("Sync Summary:")
        print(f"  Files copied: {files_copied}")
        print(f"  Files skipped: {files_skipped}")
        print(f"  Errors: {errors}")
        print("="*60)
        
        if errors > 0:
            sys.exit(1)
        
    finally:
        db.close()


if __name__ == "__main__":
    main()
