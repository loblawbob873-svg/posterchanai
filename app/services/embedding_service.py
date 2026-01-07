"""
Embedding Service - Local sentence-transformers for RAG.
Provides text embeddings using HuggingFace sentence-transformers.
No external API dependencies - fully self-contained.
"""
import logging
from typing import List, Optional
from sqlalchemy.orm import Session
from app.models import Setting

logger = logging.getLogger(__name__)

# Singleton model instance (shared across all service instances)
_model = None
_model_name = None


class EmbeddingService:
    """Local sentence-transformers embedding service."""

    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        """Load embedding settings from database."""
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.model_name = settings.get("rag_embedding_model", "all-MiniLM-L6-v2")

    def _ensure_model_loaded(self):
        """Lazy load the model on first use."""
        global _model, _model_name

        # Check if we need to load/reload the model
        if _model is None or _model_name != self.model_name:
            try:
                from sentence_transformers import SentenceTransformer
                logger.info(f"Loading sentence-transformers model: {self.model_name}")
                _model = SentenceTransformer(self.model_name)
                _model_name = self.model_name
                logger.info(f"Model loaded, embedding dimension: {_model.get_sentence_embedding_dimension()}")
            except ImportError:
                logger.error("sentence-transformers not installed. Run: pip install sentence-transformers")
                raise RuntimeError("sentence-transformers not installed")
            except Exception as e:
                logger.error(f"Failed to load embedding model: {e}")
                raise

    def embed(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for a list of texts.

        Args:
            texts: List of strings to embed

        Returns:
            List of embedding vectors (each is a list of floats)
        """
        if not texts:
            return []

        self._ensure_model_loaded()

        try:
            embeddings = _model.encode(texts, convert_to_numpy=True)
            return embeddings.tolist()
        except Exception as e:
            logger.error(f"Embedding generation failed: {e}")
            raise

    def embed_single(self, text: str) -> List[float]:
        """
        Generate embedding for a single text.

        Args:
            text: String to embed

        Returns:
            Embedding vector as list of floats
        """
        return self.embed([text])[0]

    @property
    def dimension(self) -> int:
        """Get embedding dimension."""
        self._ensure_model_loaded()
        return _model.get_sentence_embedding_dimension()

    def get_status(self) -> dict:
        """Get embedding service status."""
        global _model, _model_name
        return {
            "model": self.model_name,
            "loaded": _model is not None and _model_name == self.model_name,
            "dimension": _model.get_sentence_embedding_dimension() if _model else None
        }


def get_embedding_service(db: Session) -> EmbeddingService:
    """Get embedding service instance."""
    return EmbeddingService(db)


def unload_model():
    """Unload the embedding model to free memory."""
    global _model, _model_name
    if _model is not None:
        del _model
        _model = None
        _model_name = None
        logger.info("Embedding model unloaded")
