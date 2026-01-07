"""
File Watcher Service for RAG.
Handles events from VS Code extension or other file watchers.
"""
import secrets
import logging
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session

from app.models import RAGWatcher, RAGCollection
from app.services.rag_service import get_rag_service

logger = logging.getLogger(__name__)


class WatcherService:
    """Handles file watcher events for real-time indexing."""

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id
        self.rag_service = get_rag_service(db, user_id)

    def create_watcher(self, collection_id: int, watch_path: str) -> RAGWatcher:
        """
        Create a new file watcher configuration.

        Args:
            collection_id: ID of the collection to index into
            watch_path: Local path being watched (for reference)

        Returns:
            Created RAGWatcher with unique API key
        """
        # Verify collection belongs to user
        collection = self.db.query(RAGCollection).filter(
            RAGCollection.id == collection_id,
            RAGCollection.user_id == self.user_id
        ).first()

        if not collection:
            raise ValueError("Collection not found")

        # Generate unique API key for this watcher
        api_key = f"rag_{secrets.token_urlsafe(32)}"

        watcher = RAGWatcher(
            user_id=self.user_id,
            collection_id=collection_id,
            watch_path=watch_path,
            api_key=api_key,
            is_active=True
        )

        self.db.add(watcher)
        self.db.commit()
        self.db.refresh(watcher)

        logger.info(f"Created watcher for collection {collection_id} at {watch_path}")
        return watcher

    def handle_file_event(
        self,
        watcher: RAGWatcher,
        event_type: str,
        file_path: str,
        content: Optional[str] = None
    ):
        """
        Handle a file event from the watcher.

        Args:
            watcher: The RAGWatcher that received the event
            event_type: "created", "modified", or "deleted"
            file_path: Relative path to the file
            content: File content (required for create/modify)
        """
        logger.info(f"File event: {event_type} - {file_path}")

        if event_type == "deleted":
            self.rag_service.delete_file(watcher.collection_id, file_path)
        elif event_type in ["created", "modified"]:
            if content:
                # Skip very large files
                if len(content) > 1_000_000:
                    logger.warning(f"Skipping large file: {file_path}")
                    return

                self.rag_service.index_file(watcher.collection_id, file_path, content)
            else:
                logger.warning(f"No content provided for {event_type} event: {file_path}")

        # Update collection document count and last event timestamp
        self.rag_service.update_collection_document_count(watcher.collection_id)
        watcher.last_event_at = datetime.utcnow()
        self.db.commit()

    def deactivate_watcher(self, watcher_id: int):
        """Deactivate a watcher."""
        watcher = self.db.query(RAGWatcher).filter(
            RAGWatcher.id == watcher_id,
            RAGWatcher.user_id == self.user_id
        ).first()

        if watcher:
            watcher.is_active = False
            self.db.commit()

    def delete_watcher(self, watcher_id: int):
        """Delete a watcher."""
        watcher = self.db.query(RAGWatcher).filter(
            RAGWatcher.id == watcher_id,
            RAGWatcher.user_id == self.user_id
        ).first()

        if watcher:
            self.db.delete(watcher)
            self.db.commit()


def get_watcher_service(db: Session, user_id: int) -> WatcherService:
    """Get watcher service instance."""
    return WatcherService(db, user_id)


def validate_watcher_api_key(db: Session, api_key: str) -> Optional[RAGWatcher]:
    """
    Validate watcher API key and return watcher if valid.

    Args:
        db: Database session
        api_key: API key to validate

    Returns:
        RAGWatcher if valid, None otherwise
    """
    watcher = db.query(RAGWatcher).filter(
        RAGWatcher.api_key == api_key,
        RAGWatcher.is_active == True
    ).first()
    return watcher
