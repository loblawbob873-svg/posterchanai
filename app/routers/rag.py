"""
RAG API Router
Endpoints for managing RAG collections, indexing, and querying.
"""
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, BackgroundTasks, Form
from sqlalchemy.orm import Session
from typing import List, Optional
import zipfile
import io
import logging
from pathlib import Path
from fnmatch import fnmatch

from app.database import get_db, SessionLocal
from app.models import User, RAGCollection, RAGWatcher, RAGDocument, Setting
from app.auth import get_current_user
from app.schemas import (
    RAGCollectionCreate, RAGCollectionUpdate, RAGCollectionResponse, RAGQueryRequest, RAGQueryResult,
    RAGWatcherCreate, RAGWatcherResponse, RAGFileEvent, RAGGitCloneRequest,
    RAGStatusResponse
)
from app.services.rag_service import get_rag_service
from app.services.rag_git_service import get_git_indexer
from app.services.rag_folder_service import get_folder_indexer
from app.services.rag_watcher_service import get_watcher_service, validate_watcher_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rag", tags=["rag"])


# ----- Collection Management -----

@router.get("/collections", response_model=List[RAGCollectionResponse])
def list_collections(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all RAG collections for the current user."""
    return db.query(RAGCollection).filter(
        RAGCollection.user_id == current_user.id
    ).order_by(RAGCollection.created_at.desc()).all()


@router.post("/collections", response_model=RAGCollectionResponse, status_code=status.HTTP_201_CREATED)
def create_collection(
    data: RAGCollectionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new RAG collection."""
    collection = RAGCollection(
        user_id=current_user.id,
        name=data.name,
        description=data.description,
        collection_type=data.collection_type,
        source_path=data.source_path,
        git_branch=data.git_branch,
        file_patterns=data.file_patterns or "*.py,*.js,*.ts,*.md,*.txt"
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return collection


@router.get("/collections/{collection_id}", response_model=RAGCollectionResponse)
def get_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific collection."""
    collection = db.query(RAGCollection).filter(
        RAGCollection.id == collection_id,
        RAGCollection.user_id == current_user.id
    ).first()

    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    return collection


@router.put("/collections/{collection_id}", response_model=RAGCollectionResponse)
def update_collection(
    collection_id: int,
    data: RAGCollectionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a collection's settings."""
    collection = db.query(RAGCollection).filter(
        RAGCollection.id == collection_id,
        RAGCollection.user_id == current_user.id
    ).first()

    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Update fields if provided
    if data.name is not None:
        collection.name = data.name
    if data.description is not None:
        collection.description = data.description
    if data.source_path is not None:
        collection.source_path = data.source_path
    if data.git_branch is not None:
        collection.git_branch = data.git_branch
    if data.file_patterns is not None:
        collection.file_patterns = data.file_patterns

    db.commit()
    db.refresh(collection)
    return collection


@router.delete("/collections/{collection_id}")
def delete_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a collection and all its indexed data."""
    collection = db.query(RAGCollection).filter(
        RAGCollection.id == collection_id,
        RAGCollection.user_id == current_user.id
    ).first()

    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Clean up uploaded zip file if it exists
    if collection.collection_type == "upload" and collection.source_path:
        zip_path = Path(collection.source_path)
        if zip_path.exists():
            try:
                zip_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete upload file {zip_path}: {e}")

    rag_service = get_rag_service(db, current_user.id)
    rag_service.delete_collection(collection_id)

    return {"message": "Collection deleted"}


@router.get("/collections/{collection_id}/stats")
def get_collection_stats(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get statistics for a collection."""
    collection = db.query(RAGCollection).filter(
        RAGCollection.id == collection_id,
        RAGCollection.user_id == current_user.id
    ).first()

    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    rag_service = get_rag_service(db, current_user.id)
    return rag_service.get_collection_stats(collection_id)


# ----- Git Repository Indexing -----

@router.post("/collections/git", response_model=RAGCollectionResponse)
async def clone_git_repository(
    data: RAGGitCloneRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Clone and index a git repository (runs in background)."""
    # Create collection
    collection = RAGCollection(
        user_id=current_user.id,
        name=data.name,
        collection_type="git",
        source_path=data.git_url,
        git_branch=data.branch or "main",
        file_patterns=data.file_patterns or "*.py,*.js,*.ts,*.md,*.txt"
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)

    # Start indexing in background
    background_tasks.add_task(
        _index_git_repository_task,
        user_id=current_user.id,
        collection_id=collection.id
    )

    return collection


def _index_git_repository_task(user_id: int, collection_id: int):
    """Background task to index git repository."""
    db = SessionLocal()
    try:
        collection = db.query(RAGCollection).filter(RAGCollection.id == collection_id).first()
        if collection:
            indexer = get_git_indexer(db, user_id)
            indexer.clone_and_index(collection)
    except Exception as e:
        logger.error(f"Git indexing failed for collection {collection_id}: {e}")
    finally:
        db.close()


@router.post("/collections/{collection_id}/reindex")
async def reindex_collection(
    collection_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Re-index a collection (runs in background)."""
    collection = db.query(RAGCollection).filter(
        RAGCollection.id == collection_id,
        RAGCollection.user_id == current_user.id
    ).first()

    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    if collection.collection_type == "git":
        background_tasks.add_task(
            _index_git_repository_task,
            user_id=current_user.id,
            collection_id=collection_id
        )
    elif collection.collection_type == "folder" and collection.source_path:
        background_tasks.add_task(
            _index_folder_task,
            user_id=current_user.id,
            collection_id=collection_id
        )
    elif collection.collection_type == "upload" and collection.source_path:
        # Re-index uploaded zip file
        zip_path = Path(collection.source_path)
        if not zip_path.exists():
            raise HTTPException(status_code=400, detail="Upload file not found, cannot re-index")
        background_tasks.add_task(
            _index_upload_task,
            user_id=current_user.id,
            collection_id=collection_id
        )
    else:
        raise HTTPException(status_code=400, detail="Collection type does not support re-indexing")

    return {"message": "Re-indexing started"}


def _index_folder_task(user_id: int, collection_id: int):
    """Background task to index folder."""
    db = SessionLocal()
    try:
        collection = db.query(RAGCollection).filter(RAGCollection.id == collection_id).first()
        if collection and collection.source_path:
            indexer = get_folder_indexer(db, user_id)
            indexer.index_folder(collection)
    except Exception as e:
        logger.error(f"Folder indexing failed for collection {collection_id}: {e}")
    finally:
        db.close()


def _index_upload_task(user_id: int, collection_id: int):
    """Background task to re-index an uploaded zip file."""
    from datetime import datetime
    import traceback

    logger.info(f"[RAG-INDEX] Starting upload re-index for collection {collection_id}, user {user_id}")

    db = SessionLocal()
    try:
        collection = db.query(RAGCollection).filter(RAGCollection.id == collection_id).first()
        if not collection or not collection.source_path:
            logger.warning(f"[RAG-INDEX] Collection {collection_id} not found or no source_path")
            return

        zip_path = Path(collection.source_path)
        if not zip_path.exists():
            logger.error(f"Upload file not found for collection {collection_id}: {zip_path}")
            return

        # Load file size limits from settings
        settings = {s.key: s.value for s in db.query(Setting).all()}
        max_file_size = int(settings.get("rag_max_file_size", "1")) * 1_000_000
        max_log_size = int(settings.get("rag_max_log_size", "100")) * 1_000_000

        # Clear existing documents for this collection
        logger.info(f"[RAG-INDEX] Clearing existing documents for collection {collection_id}")
        rag_service = get_rag_service(db, user_id)
        rag_service.delete_collection_documents(collection_id)

        # Re-read and index the zip file
        logger.info(f"[RAG-INDEX] Reading zip file: {zip_path}")
        file_patterns = collection.file_patterns or "*.py,*.js,*.ts,*.md,*.txt"
        patterns = [p.strip() for p in file_patterns.split(",")]

        skip_dirs = {
            '.git', 'node_modules', '__pycache__', 'venv', '.venv',
            'dist', 'build', '.next', '.nuxt', 'target', 'vendor'
        }

        files_to_index = []
        with zipfile.ZipFile(zip_path, 'r') as zip_file:
            for zip_info in zip_file.infolist():
                if zip_info.is_dir():
                    continue

                file_path = Path(zip_info.filename)

                # Skip hidden files and common non-code directories
                parts = file_path.parts
                if any(part.startswith('.') for part in parts):
                    continue
                if any(part in skip_dirs for part in parts):
                    continue

                # Check if matches any pattern
                for pattern in patterns:
                    if fnmatch(file_path.name, pattern):
                        try:
                            file_content = zip_file.read(zip_info.filename).decode('utf-8', errors='ignore')
                            # Use configurable file size limits
                            max_size = max_log_size if file_path.suffix == '.log' else max_file_size
                            if len(file_content) > max_size:
                                logger.warning(f"Skipping large file {file_path}: {len(file_content)} bytes (limit: {max_size})")
                                continue
                            files_to_index.append({
                                "filename": str(file_path),
                                "content": file_content
                            })
                        except Exception:
                            pass
                        break

        # Index files
        logger.info(f"[RAG-INDEX] Found {len(files_to_index)} files to index")
        if files_to_index:
            for f in files_to_index:
                logger.info(f"[RAG-INDEX]   - {f['filename']}: {len(f['content'])} bytes")

        indexer = get_folder_indexer(db, user_id)
        logger.info(f"[RAG-INDEX] Starting indexer.index_uploaded_files...")
        indexer.index_uploaded_files(collection, files_to_index)

        logger.info(f"[RAG-INDEX] Completed re-indexing {len(files_to_index)} files for collection {collection_id}")
    except Exception as e:
        logger.error(f"[RAG-INDEX] Upload re-indexing failed for collection {collection_id}: {e}")
        logger.error(f"[RAG-INDEX] Traceback: {traceback.format_exc()}")
    finally:
        db.close()


# ----- Folder Upload Indexing -----

def _get_uploads_dir() -> Path:
    """Get or create the uploads directory for RAG zip files."""
    uploads_dir = Path("./data/rag_uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    return uploads_dir


@router.post("/collections/upload", response_model=RAGCollectionResponse)
async def upload_folder(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: Optional[str] = Form(None),
    file_patterns: str = Form("*.py,*.js,*.ts,*.md,*.txt"),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Upload and index a zip file containing code/documents (indexing runs in background)."""
    if not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail="Only zip files are supported")

    # Create collection first to get an ID
    collection = RAGCollection(
        user_id=current_user.id,
        name=name,
        description=description,
        collection_type="upload",
        file_patterns=file_patterns
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)

    # Save the zip file for re-indexing later
    uploads_dir = _get_uploads_dir()
    zip_path = uploads_dir / f"{collection.id}.zip"

    try:
        # Read and save the zip file (this is fast)
        content = await file.read()
        zip_path.write_bytes(content)
        collection.source_path = str(zip_path.absolute())
        db.commit()

        # Validate it's a valid zip file
        try:
            with zipfile.ZipFile(io.BytesIO(content), 'r') as zf:
                # Just test that we can read the file list
                zf.namelist()
        except zipfile.BadZipFile:
            # Cleanup on failure
            if zip_path.exists():
                zip_path.unlink()
            db.delete(collection)
            db.commit()
            raise HTTPException(status_code=400, detail="Invalid zip file")

        # Start indexing in background (non-blocking)
        background_tasks.add_task(
            _index_upload_task,
            user_id=current_user.id,
            collection_id=collection.id
        )

        db.refresh(collection)
        return collection

    except HTTPException:
        raise
    except Exception as e:
        # Cleanup on failure
        if zip_path.exists():
            zip_path.unlink()
        db.delete(collection)
        db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to process upload: {str(e)}")


# ----- Local Folder Indexing -----

@router.post("/collections/folder", response_model=RAGCollectionResponse)
async def index_local_folder(
    data: RAGCollectionCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Index a local folder on the server."""
    if not data.source_path:
        raise HTTPException(status_code=400, detail="source_path is required for folder indexing")

    folder_path = Path(data.source_path)
    if not folder_path.exists():
        raise HTTPException(status_code=400, detail=f"Folder not found: {data.source_path}")

    # Create collection
    collection = RAGCollection(
        user_id=current_user.id,
        name=data.name,
        description=data.description,
        collection_type="folder",
        source_path=data.source_path,
        file_patterns=data.file_patterns or "*.py,*.js,*.ts,*.md,*.txt"
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)

    # Start indexing in background
    background_tasks.add_task(
        _index_folder_task,
        user_id=current_user.id,
        collection_id=collection.id
    )

    return collection


# ----- File Watcher API (VS Code Integration) -----

@router.post("/watchers", response_model=RAGWatcherResponse)
def create_watcher(
    data: RAGWatcherCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a file watcher for VS Code integration."""
    # Verify collection exists and belongs to user
    collection = db.query(RAGCollection).filter(
        RAGCollection.id == data.collection_id,
        RAGCollection.user_id == current_user.id
    ).first()

    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    watcher_service = get_watcher_service(db, current_user.id)
    watcher = watcher_service.create_watcher(data.collection_id, data.watch_path)
    return watcher


@router.get("/watchers", response_model=List[RAGWatcherResponse])
def list_watchers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all file watchers."""
    return db.query(RAGWatcher).filter(
        RAGWatcher.user_id == current_user.id
    ).all()


@router.delete("/watchers/{watcher_id}")
def delete_watcher(
    watcher_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a file watcher."""
    watcher = db.query(RAGWatcher).filter(
        RAGWatcher.id == watcher_id,
        RAGWatcher.user_id == current_user.id
    ).first()

    if not watcher:
        raise HTTPException(status_code=404, detail="Watcher not found")

    db.delete(watcher)
    db.commit()
    return {"message": "Watcher deleted"}


@router.post("/watcher-event")
async def handle_watcher_event(
    event: RAGFileEvent,
    api_key: str,
    db: Session = Depends(get_db)
):
    """
    Handle file event from VS Code extension.
    Authenticates via watcher API key (not user session).
    """
    # Validate API key
    watcher = validate_watcher_api_key(db, api_key)

    if not watcher:
        raise HTTPException(status_code=401, detail="Invalid API key")

    watcher_service = get_watcher_service(db, watcher.user_id)
    watcher_service.handle_file_event(
        watcher=watcher,
        event_type=event.event_type,
        file_path=event.file_path,
        content=event.content
    )

    return {"message": "Event processed"}


# ----- RAG Query -----

@router.post("/query", response_model=List[RAGQueryResult])
def query_rag(
    data: RAGQueryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Query the RAG index and return relevant chunks."""
    rag_service = get_rag_service(db, current_user.id)
    results = rag_service.query(
        query_text=data.query,
        collection_ids=data.collection_ids,
        top_k=data.top_k
    )
    return results


# ----- Status -----

@router.get("/status", response_model=RAGStatusResponse)
def get_rag_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get RAG system status."""
    settings = {s.key: s.value for s in db.query(Setting).all()}

    collections_count = db.query(RAGCollection).filter(
        RAGCollection.user_id == current_user.id
    ).count()

    total_documents = db.query(RAGDocument).join(RAGCollection).filter(
        RAGCollection.user_id == current_user.id
    ).count()

    return RAGStatusResponse(
        enabled=settings.get("rag_enabled", "true").lower() == "true",
        embedding_model=settings.get("rag_embedding_model", "all-MiniLM-L6-v2"),
        collections_count=collections_count,
        total_documents=total_documents
    )
