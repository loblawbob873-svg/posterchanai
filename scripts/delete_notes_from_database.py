#!/usr/bin/env python3
"""
Delete all notes from the database only (files already deleted).
This is useful when files have been deleted from storage servers but database entries remain.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import Note, NoteFolder
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def delete_all_notes_from_database():
    """Delete all notes and folders from database only."""
    db: Session = SessionLocal()
    
    try:
        # Count notes before deletion
        total_notes = db.query(Note).count()
        total_folders = db.query(NoteFolder).count()
        logger.info(f"Found {total_notes} notes and {total_folders} folders in database")
        
        if total_notes == 0 and total_folders == 0:
            logger.info("No notes or folders to delete.")
            return
        
        # Delete all notes from database
        logger.info("Deleting all notes from database...")
        deleted_notes = db.query(Note).delete()
        db.commit()
        logger.info(f"Deleted {deleted_notes} notes from database")
        
        # Delete all note folders from database
        logger.info("Deleting all note folders from database...")
        deleted_folders = db.query(NoteFolder).delete()
        db.commit()
        logger.info(f"Deleted {deleted_folders} folders from database")
        
        # Verify deletion
        remaining_notes = db.query(Note).count()
        remaining_folders = db.query(NoteFolder).count()
        
        logger.info(f"\n{'='*60}")
        logger.info("DELETION SUMMARY")
        logger.info(f"{'='*60}")
        logger.info(f"Notes deleted from database: {deleted_notes}")
        logger.info(f"Folders deleted from database: {deleted_folders}")
        logger.info(f"\nRemaining notes in database: {remaining_notes}")
        logger.info(f"Remaining folders in database: {remaining_folders}")
        logger.info(f"{'='*60}")
        
        if remaining_notes == 0 and remaining_folders == 0:
            logger.info("✓ All notes and folders successfully deleted from database!")
        else:
            logger.warning(f"⚠ Warning: {remaining_notes} notes and {remaining_folders} folders still remain")
            
    except Exception as e:
        logger.error(f"Error during deletion: {e}", exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    print("WARNING: This will delete ALL notes and folders from the database!")
    print("Files should already be deleted from storage servers.")
    print("This operation cannot be undone!")
    response = input("Type 'DELETE FROM DATABASE' to confirm: ")
    
    if response == "DELETE FROM DATABASE":
        delete_all_notes_from_database()
    else:
        print("Deletion cancelled.")
