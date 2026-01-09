"""
RAG Service - Core RAG functionality with ChromaDB.
Handles document indexing, code-aware chunking, and retrieval.
"""
import os
import re
import time
import hashlib
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from sqlalchemy.orm import Session

from app.models import Setting, RAGCollection, RAGDocument
from app.services.embedding_service import get_embedding_service

logger = logging.getLogger(__name__)

# Singleton ChromaDB client
_chroma_client = None

# Global settings cache for RAG service
_rag_settings_cache = {}
_rag_settings_cache_time = 0
_RAG_SETTINGS_TTL = 3600  # 1 hour (settings rarely change)

# Collection metadata cache (collection_id -> {name, count, last_check})
_collection_cache = {}
_COLLECTION_CACHE_TTL = 3600  # 1 hour

# ChromaDB collection object cache (avoid recreating collection handles)
_chroma_collection_cache = {}

# Full query results cache - caches entire search results
# Key: hash(user_id, query, top_k, collection_ids)
_query_results_cache = {}
_QUERY_RESULTS_CACHE_MAX = 100000  # 100k cached queries (default)
_QUERY_RESULTS_TTL = 600  # 10 minutes for query results


class RAGService:
    """Core RAG service with ChromaDB vector store."""

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id
        self._load_settings()
        self._ensure_chroma_client()

    def _load_settings(self):
        """Load RAG settings from database with caching."""
        global _rag_settings_cache, _rag_settings_cache_time

        current_time = time.time()
        if _rag_settings_cache and (current_time - _rag_settings_cache_time) < _RAG_SETTINGS_TTL:
            settings = _rag_settings_cache
        else:
            settings = {s.key: s.value for s in self.db.query(Setting).all()}
            _rag_settings_cache = settings
            _rag_settings_cache_time = current_time

        self.chromadb_path = settings.get("rag_chromadb_path", "./data/chromadb")
        self.chunk_size = int(settings.get("rag_chunk_size", "1000"))
        self.chunk_overlap = int(settings.get("rag_chunk_overlap", "200"))
        self.top_k = int(settings.get("rag_top_k", "5"))
        self.min_similarity = float(settings.get("rag_min_similarity", "0.3"))
        # Context and chunk size limits
        self.max_context_chars = int(settings.get("rag_max_context_chars", "32000"))
        self.max_chunk_display = int(settings.get("rag_max_chunk_display", "8000"))
        self.max_chunk_index = int(settings.get("rag_max_chunk_index", "10000"))

        # Cache settings (tunable via admin UI) - aggressive defaults
        self.query_cache_max = int(settings.get("rag_query_cache_max", "100000"))
        self.query_cache_ttl = int(settings.get("rag_query_cache_ttl", "600"))
        self.embedding_cache_max = int(settings.get("rag_embedding_cache_max", "250000"))

        # ChromaDB HNSW tuning parameters
        self.hnsw_ef_search = int(settings.get("rag_hnsw_ef_search", "100"))
        self.hnsw_ef_construction = int(settings.get("rag_hnsw_ef_construction", "200"))
        self.hnsw_m = int(settings.get("rag_hnsw_m", "16"))

    def _ensure_chroma_client(self):
        """Get or create ChromaDB client."""
        global _chroma_client

        if _chroma_client is None:
            try:
                import chromadb
                from chromadb.config import Settings as ChromaSettings

                os.makedirs(self.chromadb_path, exist_ok=True)
                _chroma_client = chromadb.PersistentClient(
                    path=self.chromadb_path,
                    settings=ChromaSettings(anonymized_telemetry=False)
                )
                logger.info(f"ChromaDB initialized at {self.chromadb_path}")
            except ImportError:
                logger.error("chromadb not installed. Run: pip install chromadb")
                raise RuntimeError("chromadb not installed")

        self.client = _chroma_client

    def _get_collection_name(self, collection_id: int) -> str:
        """Generate unique ChromaDB collection name."""
        return f"user_{self.user_id}_collection_{collection_id}"

    def _get_or_create_chroma_collection(self, collection_id: int):
        """Get or create a ChromaDB collection with caching and HNSW tuning."""
        global _chroma_collection_cache

        cache_key = f"{self.user_id}_{collection_id}"
        if cache_key in _chroma_collection_cache:
            return _chroma_collection_cache[cache_key]

        name = self._get_collection_name(collection_id)
        collection = self.client.get_or_create_collection(
            name=name,
            metadata={
                "hnsw:space": "cosine",
                "hnsw:construction_ef": self.hnsw_ef_construction,
                "hnsw:search_ef": self.hnsw_ef_search,
                "hnsw:M": self.hnsw_m,
            }
        )

        _chroma_collection_cache[cache_key] = collection
        return collection

    # ----- Code-Aware Chunking -----

    def _chunk_content(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """
        Chunk content based on file type.
        Respects language boundaries for code files.
        """
        ext = Path(file_path).suffix.lower()

        # Language-specific chunking
        if ext == '.py':
            return self._chunk_python(content, file_path)
        elif ext in ['.js', '.jsx']:
            return self._chunk_javascript(content, file_path)
        elif ext in ['.ts', '.tsx']:
            return self._chunk_typescript(content, file_path)
        elif ext == '.go':
            return self._chunk_go(content, file_path)
        elif ext == '.rs':
            return self._chunk_rust(content, file_path)
        elif ext in ['.java', '.kt']:
            return self._chunk_java(content, file_path)
        elif ext in ['.md', '.markdown']:
            return self._chunk_markdown(content, file_path)
        else:
            return self._chunk_generic(content, file_path)

    def _chunk_python(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk Python code by class/function definitions."""
        chunks = []

        # Pattern matches class and function definitions with their bodies
        # Uses indentation to determine block boundaries
        lines = content.split('\n')
        current_chunk = []
        current_type = None
        indent_level = 0

        for i, line in enumerate(lines):
            stripped = line.lstrip()

            # Check for new definition
            if stripped.startswith('class ') or stripped.startswith('def ') or stripped.startswith('async def '):
                # Save previous chunk if exists
                if current_chunk and len('\n'.join(current_chunk).strip()) > 50:
                    chunks.append({
                        "content": '\n'.join(current_chunk).strip(),
                        "metadata": {
                            "file_path": file_path,
                            "chunk_type": "code_block",
                            "language": "python"
                        }
                    })

                current_chunk = [line]
                current_type = 'class' if stripped.startswith('class ') else 'function'
                indent_level = len(line) - len(stripped)
            elif current_chunk:
                # Continue current block or end it
                if stripped and not line.startswith(' ' * (indent_level + 1)) and not stripped.startswith('#'):
                    # New top-level code, save chunk and start new
                    if len('\n'.join(current_chunk).strip()) > 50:
                        chunks.append({
                            "content": '\n'.join(current_chunk).strip(),
                            "metadata": {
                                "file_path": file_path,
                                "chunk_type": "code_block",
                                "language": "python"
                            }
                        })
                    current_chunk = [line]
                else:
                    current_chunk.append(line)
            else:
                current_chunk.append(line)

        # Don't forget last chunk
        if current_chunk and len('\n'.join(current_chunk).strip()) > 50:
            chunks.append({
                "content": '\n'.join(current_chunk).strip(),
                "metadata": {
                    "file_path": file_path,
                    "chunk_type": "code_block",
                    "language": "python"
                }
            })

        # If we got very few chunks, fall back to generic
        if len(chunks) < 2:
            return self._chunk_generic(content, file_path)

        return chunks

    def _chunk_javascript(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk JavaScript by function/class boundaries."""
        return self._chunk_c_style(content, file_path, "javascript")

    def _chunk_typescript(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk TypeScript by function/class/interface boundaries."""
        return self._chunk_c_style(content, file_path, "typescript")

    def _chunk_go(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk Go by func/type boundaries."""
        return self._chunk_c_style(content, file_path, "go")

    def _chunk_rust(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk Rust by fn/impl/struct boundaries."""
        return self._chunk_c_style(content, file_path, "rust")

    def _chunk_java(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk Java by class/method boundaries."""
        return self._chunk_c_style(content, file_path, "java")

    def _chunk_c_style(self, content: str, file_path: str, language: str) -> List[Dict[str, Any]]:
        """Generic chunking for C-style languages using brace matching."""
        chunks = []

        # Split by top-level braces (simplified approach)
        lines = content.split('\n')
        current_chunk = []
        brace_depth = 0
        in_block = False

        for line in lines:
            current_chunk.append(line)
            brace_depth += line.count('{') - line.count('}')

            if brace_depth == 0 and in_block:
                # End of a top-level block
                chunk_text = '\n'.join(current_chunk).strip()
                if len(chunk_text) > 50:
                    chunks.append({
                        "content": chunk_text,
                        "metadata": {
                            "file_path": file_path,
                            "chunk_type": "code_block",
                            "language": language
                        }
                    })
                current_chunk = []
                in_block = False
            elif brace_depth > 0:
                in_block = True

        # Handle remaining content
        if current_chunk:
            chunk_text = '\n'.join(current_chunk).strip()
            if len(chunk_text) > 50:
                chunks.append({
                    "content": chunk_text,
                    "metadata": {
                        "file_path": file_path,
                        "chunk_type": "code_block",
                        "language": language
                    }
                })

        if len(chunks) < 2:
            return self._chunk_generic(content, file_path)

        return chunks

    def _chunk_markdown(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk markdown by headers."""
        chunks = []

        # Split by headers
        sections = re.split(r'\n(?=#{1,3}\s)', content)

        for section in sections:
            section = section.strip()
            if len(section) > 50:
                chunks.append({
                    "content": section,
                    "metadata": {
                        "file_path": file_path,
                        "chunk_type": "section",
                        "language": "markdown"
                    }
                })

        if len(chunks) < 2:
            return self._chunk_generic(content, file_path)

        return chunks

    def _chunk_generic(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Generic chunking with overlap for any text."""
        chunks = []

        # Try splitting by double newlines first (paragraphs)
        paragraphs = content.split('\n\n')

        # If we only got one big paragraph (common in log files), split by single newlines
        if len(paragraphs) <= 1 or (len(paragraphs) > 0 and len(paragraphs[0]) > self.chunk_size * 2):
            # Split by single newlines for log-style files
            lines = content.split('\n')

            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line) + 1 < self.chunk_size:
                    current_chunk += line + "\n"
                else:
                    if current_chunk.strip():
                        chunks.append({
                            "content": current_chunk.strip(),
                            "metadata": {
                                "file_path": file_path,
                                "chunk_type": "text"
                            }
                        })
                    # Start new chunk with overlap from previous lines
                    if self.chunk_overlap > 0:
                        overlap_lines = current_chunk.split('\n')
                        overlap_text = ""
                        for ol in reversed(overlap_lines):
                            if len(overlap_text) + len(ol) < self.chunk_overlap:
                                overlap_text = ol + "\n" + overlap_text
                            else:
                                break
                        current_chunk = overlap_text + line + "\n"
                    else:
                        current_chunk = line + "\n"

            # Add remaining content
            if current_chunk.strip():
                chunks.append({
                    "content": current_chunk.strip(),
                    "metadata": {
                        "file_path": file_path,
                        "chunk_type": "text"
                    }
                })

            return chunks

        # Original paragraph-based chunking for files with double newlines
        current_chunk = ""
        for para in paragraphs:
            if len(current_chunk) + len(para) < self.chunk_size:
                current_chunk += para + "\n\n"
            else:
                if current_chunk.strip():
                    chunks.append({
                        "content": current_chunk.strip(),
                        "metadata": {
                            "file_path": file_path,
                            "chunk_type": "text"
                        }
                    })
                # Start new chunk with overlap
                overlap_start = max(0, len(current_chunk) - self.chunk_overlap)
                current_chunk = current_chunk[overlap_start:] + para + "\n\n"

        # Add remaining content
        if current_chunk.strip():
            chunks.append({
                "content": current_chunk.strip(),
                "metadata": {
                    "file_path": file_path,
                    "chunk_type": "text"
                }
            })

        return chunks

    # ----- Document Indexing -----

    def _compute_file_hash(self, content: str) -> str:
        """Compute SHA256 hash of file content."""
        return hashlib.sha256(content.encode()).hexdigest()

    def index_file(self, collection_id: int, file_path: str, content: str) -> int:
        """
        Index a single file into a collection.
        Returns number of chunks indexed.
        """
        file_hash = self._compute_file_hash(content)

        # Check if file already indexed with same hash
        existing = self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id,
            RAGDocument.file_path == file_path
        ).first()

        if existing and existing.file_hash == file_hash:
            logger.debug(f"File unchanged, skipping: {file_path}")
            return 0

        # Get ChromaDB collection
        chroma_collection = self._get_or_create_chroma_collection(collection_id)

        # Remove old chunks if file was previously indexed
        if existing:
            try:
                chroma_collection.delete(where={"file_path": file_path})
            except Exception:
                pass  # Collection might be empty
            self.db.delete(existing)
            self.db.flush()

        # Chunk the content
        chunks = self._chunk_content(content, file_path)

        if not chunks:
            return 0

        # Enforce maximum chunk size to prevent massive chunks from being stored
        filtered_chunks = []
        for chunk in chunks:
            if len(chunk["content"]) > self.max_chunk_index:
                # Split oversized chunk into smaller pieces
                content_text = chunk["content"]
                for i in range(0, len(content_text), self.max_chunk_index):
                    piece = content_text[i:i + self.max_chunk_index]
                    if piece.strip():
                        filtered_chunks.append({
                            "content": piece,
                            "metadata": chunk["metadata"].copy()
                        })
            else:
                filtered_chunks.append(chunk)
        chunks = filtered_chunks

        if not chunks:
            return 0

        # Generate embeddings
        logger.info(f"[RAG-INDEX] Generating embeddings for {len(chunks)} chunks from {file_path}")
        embedding_service = get_embedding_service(self.db)
        texts = [c["content"] for c in chunks]
        embeddings = embedding_service.embed(texts)
        logger.info(f"[RAG-INDEX] Embeddings generated, writing to ChromaDB...")

        # Add to ChromaDB
        ids = [f"{file_path}_{i}" for i in range(len(chunks))]
        metadatas = [c["metadata"] for c in chunks]

        chroma_collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas
        )

        # Track in database
        doc = RAGDocument(
            collection_id=collection_id,
            file_path=file_path,
            file_hash=file_hash,
            chunk_count=len(chunks)
        )
        self.db.add(doc)
        self.db.commit()

        logger.info(f"Indexed {len(chunks)} chunks from {file_path}")
        return len(chunks)

    def delete_file(self, collection_id: int, file_path: str):
        """Remove a file from the index."""
        chroma_collection = self._get_or_create_chroma_collection(collection_id)

        try:
            chroma_collection.delete(where={"file_path": file_path})
        except Exception:
            pass

        doc = self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id,
            RAGDocument.file_path == file_path
        ).first()

        if doc:
            self.db.delete(doc)
            self.db.commit()

    # ----- Query/Retrieval -----

    def _get_collection_name_cached(self, collection_id: int) -> str:
        """Get collection name with caching."""
        global _collection_cache

        current_time = time.time()
        cache_key = f"name_{collection_id}"

        if cache_key in _collection_cache:
            cached = _collection_cache[cache_key]
            if current_time - cached["time"] < _COLLECTION_CACHE_TTL:
                return cached["name"]

        db_collection = self.db.query(RAGCollection).filter(
            RAGCollection.id == collection_id
        ).first()
        name = db_collection.name if db_collection else "Unknown"

        _collection_cache[cache_key] = {"name": name, "time": current_time}
        return name

    def _query_single_collection(
        self,
        collection_id: int,
        query_embedding: List[float],
        top_k: int,
        min_similarity: float
    ) -> List[Dict[str, Any]]:
        """Query a single collection - designed for parallel execution."""
        results = []
        try:
            chroma_collection = self._get_or_create_chroma_collection(collection_id)
            collection_name = self._get_collection_name_cached(collection_id)

            # Query without calling count() - just request top_k results
            # ChromaDB handles the case where fewer results exist
            query_results = chroma_collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                include=["documents", "metadatas", "distances"]
            )

            if query_results["documents"] and query_results["documents"][0]:
                for i, doc in enumerate(query_results["documents"][0]):
                    distance = query_results["distances"][0][i] if query_results["distances"] else 0
                    similarity = 1 - distance

                    if similarity >= min_similarity:
                        results.append({
                            "content": doc,
                            "file_path": query_results["metadatas"][0][i].get("file_path", ""),
                            "similarity": similarity,
                            "collection_name": collection_name,
                            "metadata": query_results["metadatas"][0][i]
                        })
        except Exception as e:
            logger.error(f"Error querying collection {collection_id}: {e}")

        return results

    def _get_query_cache_key(self, query_text: str, collection_ids: List[int], top_k: int) -> str:
        """Generate cache key for query results."""
        key_str = f"{self.user_id}:{query_text}:{sorted(collection_ids)}:{top_k}"
        return hashlib.md5(key_str.encode()).hexdigest()

    def _get_cached_query(self, cache_key: str) -> Optional[List[Dict[str, Any]]]:
        """Get cached query results if still valid."""
        global _query_results_cache
        if cache_key in _query_results_cache:
            result, timestamp = _query_results_cache[cache_key]
            if time.time() - timestamp < self.query_cache_ttl:
                return result
            else:
                del _query_results_cache[cache_key]
        return None

    def _cache_query_result(self, cache_key: str, results: List[Dict[str, Any]]):
        """Cache query results."""
        global _query_results_cache
        # Enforce max size
        while len(_query_results_cache) >= self.query_cache_max:
            # Remove oldest entry
            oldest_key = next(iter(_query_results_cache))
            del _query_results_cache[oldest_key]
        _query_results_cache[cache_key] = (results, time.time())

    def query(
        self,
        query_text: str,
        collection_ids: Optional[List[int]] = None,
        top_k: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Query the RAG index and return relevant chunks.
        Uses parallel execution for multiple collections.
        Results are cached in RAM for fast repeated queries.
        """
        if top_k is None:
            top_k = self.top_k

        # Get user's collections if not specified
        if collection_ids is None:
            collections = self.db.query(RAGCollection).filter(
                RAGCollection.user_id == self.user_id
            ).all()
            collection_ids = [c.id for c in collections]

        if not collection_ids:
            return []

        # Check query results cache first
        cache_key = self._get_query_cache_key(query_text, collection_ids, top_k)
        cached_results = self._get_cached_query(cache_key)
        if cached_results is not None:
            logger.debug(f"[RAG] Query cache hit for: {query_text[:50]}...")
            return cached_results

        # Generate query embedding (also cached in embedding service)
        embedding_service = get_embedding_service(self.db)
        query_embedding = embedding_service.embed_single(query_text)

        all_results = []

        # For single collection, query directly (no thread overhead)
        if len(collection_ids) == 1:
            all_results = self._query_single_collection(
                collection_ids[0], query_embedding, top_k, self.min_similarity
            )
        else:
            # Parallel query for multiple collections
            with ThreadPoolExecutor(max_workers=min(4, len(collection_ids))) as executor:
                futures = {
                    executor.submit(
                        self._query_single_collection,
                        cid, query_embedding, top_k, self.min_similarity
                    ): cid for cid in collection_ids
                }

                for future in as_completed(futures):
                    try:
                        results = future.result()
                        all_results.extend(results)
                    except Exception as e:
                        logger.error(f"Collection query failed: {e}")

        # Sort by similarity and return top_k
        all_results.sort(key=lambda x: x["similarity"], reverse=True)
        final_results = all_results[:top_k]

        # Cache the results
        self._cache_query_result(cache_key, final_results)

        return final_results

    def format_context(self, results: List[Dict[str, Any]], max_context_chars: int = None) -> str:
        """Format query results as context for the LLM.

        Args:
            results: Query results from RAG
            max_context_chars: Maximum total characters for context (uses setting if not specified)
        """
        if not results:
            return ""

        # Use instance settings if not overridden
        if max_context_chars is None:
            max_context_chars = self.max_context_chars
        max_chunk_chars = self.max_chunk_display

        context_parts = ["## Relevant Code/Documentation Context\n"]
        total_chars = len(context_parts[0])

        for i, result in enumerate(results, 1):
            file_path = result['file_path']
            lang = result.get('metadata', {}).get('language', '')
            content = result['content']

            # Truncate individual chunks that are too large
            if len(content) > max_chunk_chars:
                content = content[:max_chunk_chars] + "\n... [truncated - chunk too large]"

            header = f"### Source {i}: `{file_path}`\n"
            header += f"Collection: {result['collection_name']} | Relevance: {result['similarity']:.0%}\n"

            # Add code block with language hint
            if lang:
                chunk_text = f"{header}```{lang}\n{content}\n```\n\n"
            else:
                chunk_text = f"{header}```\n{content}\n```\n\n"

            # Check if adding this chunk would exceed max context
            if total_chars + len(chunk_text) > max_context_chars:
                if i == 1:
                    # At least include first result (truncated)
                    remaining = max_context_chars - total_chars - 100
                    if remaining > 500:
                        truncated_content = content[:remaining] + "\n... [truncated to fit context]"
                        if lang:
                            chunk_text = f"{header}```{lang}\n{truncated_content}\n```\n\n"
                        else:
                            chunk_text = f"{header}```\n{truncated_content}\n```\n\n"
                        context_parts.append(chunk_text)
                context_parts.append(f"\n[{len(results) - i + 1} more results omitted to fit context window]")
                break

            context_parts.append(chunk_text)
            total_chars += len(chunk_text)

        return "\n".join(context_parts)

    # ----- Collection Management -----

    def update_collection_document_count(self, collection_id: int):
        """Update the document_count field to match actual RAGDocument count."""
        doc_count = self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id
        ).count()

        collection = self.db.query(RAGCollection).filter(
            RAGCollection.id == collection_id
        ).first()

        if collection:
            collection.document_count = doc_count

    def delete_collection_documents(self, collection_id: int):
        """Delete all documents in a collection but keep the collection itself."""
        name = self._get_collection_name(collection_id)
        try:
            self.client.delete_collection(name)
        except Exception:
            pass  # Collection may not exist

        # Delete documents from database but keep the collection
        self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id
        ).delete()

        # Reset document count
        collection = self.db.query(RAGCollection).filter(
            RAGCollection.id == collection_id
        ).first()
        if collection:
            collection.document_count = 0
        self.db.commit()

    def delete_collection(self, collection_id: int):
        """Delete a collection and all its data."""
        name = self._get_collection_name(collection_id)
        try:
            self.client.delete_collection(name)
        except Exception:
            pass  # Collection may not exist

        # Delete from database
        self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id
        ).delete()
        self.db.query(RAGCollection).filter(
            RAGCollection.id == collection_id
        ).delete()
        self.db.commit()

    def get_collection_stats(self, collection_id: int) -> Dict[str, Any]:
        """Get statistics for a collection."""
        chroma_collection = self._get_or_create_chroma_collection(collection_id)

        db_collection = self.db.query(RAGCollection).filter(
            RAGCollection.id == collection_id
        ).first()

        doc_count = self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id
        ).count()

        return {
            "name": db_collection.name if db_collection else "Unknown",
            "document_count": doc_count,
            "chunk_count": chroma_collection.count(),
            "last_indexed": db_collection.last_indexed_at.isoformat() if db_collection and db_collection.last_indexed_at else None
        }


def get_rag_service(db: Session, user_id: int) -> RAGService:
    """Get RAG service instance for a user."""
    return RAGService(db, user_id)


def get_cache_stats() -> dict:
    """Get RAG cache statistics."""
    return {
        "settings_cache_age_seconds": time.time() - _rag_settings_cache_time if _rag_settings_cache_time else None,
        "collection_cache_size": len(_collection_cache),
        "chroma_collection_cache_size": len(_chroma_collection_cache),
        "query_results_cache_size": len(_query_results_cache),
        "query_results_cache_max": _QUERY_RESULTS_CACHE_MAX,
    }


def clear_all_caches():
    """Clear all RAG caches."""
    global _rag_settings_cache, _rag_settings_cache_time, _collection_cache
    global _chroma_collection_cache, _query_results_cache

    _rag_settings_cache = {}
    _rag_settings_cache_time = 0
    _collection_cache = {}
    _chroma_collection_cache = {}
    _query_results_cache = {}
    logger.info("All RAG caches cleared")


def clear_query_cache():
    """Clear only the query results cache."""
    global _query_results_cache
    _query_results_cache = {}
    logger.info("RAG query cache cleared")
