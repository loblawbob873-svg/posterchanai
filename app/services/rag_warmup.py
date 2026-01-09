"""
RAG Warmup Service - Pre-loads RAG data into RAM on startup.
"""
import time
import logging
from app.database import SessionLocal
from app.models import Setting, RAGCollection

logger = logging.getLogger(__name__)

# Warmup state
_warmup_complete = False
_warmup_stats = {}


def warmup_rag_cache(user_id: int = 1, load_documents: bool = True):
    """
    Pre-load RAG data into RAM for fast queries.

    This loads:
    1. Embedding model
    2. ChromaDB collections into memory
    3. All document chunks (optional)
    """
    global _warmup_complete, _warmup_stats

    if _warmup_complete:
        logger.info("[RAG WARMUP] Already complete, skipping")
        return _warmup_stats

    logger.info("[RAG WARMUP] Starting cache warmup...")
    start = time.time()

    stats = {
        "collections_loaded": 0,
        "chunks_cached": 0,
        "embedding_model": None,
    }

    db = SessionLocal()
    try:
        # Check if RAG is enabled
        rag_enabled = db.query(Setting).filter(Setting.key == "rag_enabled").first()
        if not rag_enabled or rag_enabled.value != "true":
            logger.info("[RAG WARMUP] RAG is disabled, skipping warmup")
            return {"status": "disabled", "reason": "rag_disabled"}

        # Check if warmup is enabled
        auto_warmup = db.query(Setting).filter(Setting.key == "rag_auto_warmup").first()
        if auto_warmup and auto_warmup.value == "false":
            logger.info("[RAG WARMUP] Disabled by setting, skipping")
            return {"status": "disabled", "reason": "warmup_disabled"}

        # 1. Load embedding service and model
        logger.info("[RAG WARMUP] Step 1/3: Loading embedding model...")
        from app.services.embedding_service import get_embedding_service
        embed_svc = get_embedding_service(db)
        embed_svc._ensure_model_loaded()

        # Do a test embedding to fully initialize
        ref_embedding = embed_svc.embed_single("warmup test query", use_cache=False)
        stats["embedding_model"] = embed_svc.model_name

        # 2. Initialize RAG service and load collections
        logger.info("[RAG WARMUP] Step 2/3: Loading ChromaDB collections...")
        from app.services.rag_service import get_rag_service
        rag_svc = get_rag_service(db, user_id)

        collections = db.query(RAGCollection).filter(
            RAGCollection.user_id == user_id
        ).all()

        for col in collections:
            try:
                chroma_col = rag_svc._get_or_create_chroma_collection(col.id)
                # Force ChromaDB to load index with a dummy query
                chroma_col.query(query_embeddings=[ref_embedding], n_results=1)
                stats["collections_loaded"] += 1
                logger.info(f"[RAG WARMUP] Loaded: {col.name} ({col.document_count} docs)")
            except Exception as e:
                logger.warning(f"[RAG WARMUP] Failed to load {col.name}: {e}")

        # 3. Cache document chunks if enabled
        if load_documents:
            logger.info("[RAG WARMUP] Step 3/3: Caching document chunks...")
            for col in collections:
                try:
                    chroma_col = rag_svc._get_or_create_chroma_collection(col.id)
                    all_data = chroma_col.get(include=["documents", "metadatas"])

                    if all_data and all_data.get("ids"):
                        stats["chunks_cached"] += len(all_data["ids"])
                        logger.info(f"[RAG WARMUP] Cached {len(all_data['ids'])} chunks from {col.name}")
                except Exception as e:
                    logger.warning(f"[RAG WARMUP] Failed to cache {col.name}: {e}")

        elapsed = time.time() - start
        _warmup_complete = True
        _warmup_stats = {
            "status": "ok",
            "elapsed_seconds": round(elapsed, 2),
            **stats
        }

        logger.info(f"[RAG WARMUP] Complete in {elapsed:.2f}s - {stats}")
        return _warmup_stats

    except Exception as e:
        logger.error(f"[RAG WARMUP] Failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


def get_warmup_status():
    """Get warmup status."""
    return {
        "complete": _warmup_complete,
        "stats": _warmup_stats
    }


def is_warmed_up():
    """Check if warmup is complete."""
    return _warmup_complete
