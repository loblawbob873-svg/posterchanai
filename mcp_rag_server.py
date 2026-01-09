#!/usr/bin/env python3
"""
MCP Server for Posterchanai RAG.
Exposes the existing RAG service to MCP clients like Continue.dev, Claude Desktop, etc.

Usage:
    python mcp_rag_server.py
    python mcp_rag_server.py --warmup  # Pre-load model on startup
    python mcp_rag_server.py --sse --port 8808

Configure in Continue.dev config.json:
{
  "mcpServers": {
    "posterchanai-rag": {
      "command": "python",
      "args": ["/home/verita84/posterchanai/mcp_rag_server.py", "--warmup"]
    }
  }
}
"""
import sys
import os
import asyncio
import logging
import time
import hashlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Bounded thread pool for embedding operations (prevents CPU overload)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rag_worker")

# Query result cache with TTL - large cache for aggressive RAM usage
_query_cache = OrderedDict()
_QUERY_CACHE_MAX = 50000  # 50k cached MCP queries
_QUERY_CACHE_TTL = 600  # 10 minutes TTL for search results


def _get_cached_result(cache_key: str):
    """Get cached result if still valid."""
    if cache_key in _query_cache:
        result, timestamp = _query_cache[cache_key]
        if time.time() - timestamp < _QUERY_CACHE_TTL:
            # Move to end (LRU)
            _query_cache.move_to_end(cache_key)
            return result
        else:
            # Expired, remove it
            del _query_cache[cache_key]
    return None


def _cache_result(cache_key: str, result):
    """Cache a result with timestamp."""
    # Enforce max size
    while len(_query_cache) >= _QUERY_CACHE_MAX:
        _query_cache.popitem(last=False)  # Remove oldest
    _query_cache[cache_key] = (result, time.time())

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Import existing RAG infrastructure
from app.database import SessionLocal
from app.models import RAGCollection, RAGDocument
from app.services.rag_service import get_rag_service, get_cache_stats as get_rag_cache_stats
from app.services.rag_folder_service import get_folder_indexer
from app.services.embedding_service import get_embedding_service, get_cache_stats as get_embed_cache_stats

# Document chunk cache - stores all indexed content in RAM
_document_cache = {}  # collection_id -> {doc_id -> {content, metadata}}
_document_cache_loaded = False

# IMPORTANT: Use stderr for logging in stdio mode to avoid corrupting JSON-RPC stream
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# User ID from environment or default
DEFAULT_USER_ID = int(os.environ.get("RAG_USER_ID", "1"))

# Warmup state
_warmup_complete = False


def warmup_model(load_documents: bool = True):
    """
    Pre-load the embedding model and warm up all caches.

    Args:
        load_documents: If True, also load all document chunks into RAM
    """
    global _warmup_complete, _document_cache, _document_cache_loaded
    if _warmup_complete:
        logger.info("[WARMUP] Already warmed up, skipping")
        return {"status": "already_warm"}

    # Check if RAG is enabled before warming up
    from app.models import Setting
    db = SessionLocal()
    try:
        rag_enabled = db.query(Setting).filter(Setting.key == "rag_enabled").first()
        if not rag_enabled or rag_enabled.value != "true":
            logger.info("[WARMUP] RAG is disabled, skipping warmup")
            return {"status": "disabled", "reason": "rag_disabled"}
    finally:
        db.close()

    logger.info("[WARMUP] Starting full cache warmup...")
    start = time.time()
    stats = {
        "collections_loaded": 0,
        "documents_cached": 0,
        "chunks_cached": 0,
        "embeddings_precomputed": 0,
    }

    db = SessionLocal()
    try:
        # 1. Load embedding service and model
        logger.info("[WARMUP] Step 1/4: Loading embedding model...")
        embed_svc = get_embedding_service(db)
        embed_svc._ensure_model_loaded()
        _ = embed_svc.embed_single("warmup test query", use_cache=False)

        # 2. Initialize RAG service
        logger.info("[WARMUP] Step 2/4: Initializing RAG service...")
        rag_svc = get_rag_service(db, DEFAULT_USER_ID)

        # 3. Pre-load all ChromaDB collections into memory
        logger.info("[WARMUP] Step 3/4: Loading ChromaDB collections...")
        collections = db.query(RAGCollection).filter(
            RAGCollection.user_id == DEFAULT_USER_ID
        ).all()

        # Get a reference embedding for dummy queries
        ref_embedding = embed_svc.embed_single("test", use_cache=True)

        for col in collections:
            try:
                chroma_col = rag_svc._get_or_create_chroma_collection(col.id)
                # Force ChromaDB to load index into memory with a dummy query
                chroma_col.query(query_embeddings=[ref_embedding], n_results=1)
                stats["collections_loaded"] += 1
                logger.info(f"[WARMUP] Loaded collection: {col.name} ({col.document_count} docs)")
            except Exception as e:
                logger.warning(f"[WARMUP] Failed to load collection {col.name}: {e}")

        # 4. Cache all document chunks in RAM (optional but recommended)
        if load_documents:
            logger.info("[WARMUP] Step 4/4: Caching document chunks in RAM...")
            for col in collections:
                try:
                    chroma_col = rag_svc._get_or_create_chroma_collection(col.id)

                    # Get all documents from ChromaDB
                    all_data = chroma_col.get(
                        include=["documents", "metadatas", "embeddings"]
                    )

                    if all_data and all_data.get("ids"):
                        _document_cache[col.id] = {}
                        for i, doc_id in enumerate(all_data["ids"]):
                            _document_cache[col.id][doc_id] = {
                                "content": all_data["documents"][i] if all_data["documents"] else None,
                                "metadata": all_data["metadatas"][i] if all_data["metadatas"] else {},
                                "embedding": all_data["embeddings"][i] if all_data.get("embeddings") else None,
                            }
                            stats["chunks_cached"] += 1

                        stats["documents_cached"] += len(all_data["ids"])
                        logger.info(f"[WARMUP] Cached {len(all_data['ids'])} chunks from {col.name}")
                except Exception as e:
                    logger.warning(f"[WARMUP] Failed to cache documents for {col.name}: {e}")

            _document_cache_loaded = True

        elapsed = time.time() - start
        _warmup_complete = True

        logger.info(f"[WARMUP] Complete in {elapsed:.2f}s")
        logger.info(f"[WARMUP] Stats: {stats}")

        return {
            "status": "ok",
            "elapsed_seconds": round(elapsed, 2),
            "embedding_model": embed_svc.model_name,
            "embedding_dimension": embed_svc.dimension,
            **stats
        }
    except Exception as e:
        logger.error(f"[WARMUP] Failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


def get_status():
    """Get server status and cache statistics."""
    embed_stats = get_embed_cache_stats()
    rag_stats = get_rag_cache_stats()

    # Calculate document cache size
    doc_cache_chunks = sum(len(docs) for docs in _document_cache.values())
    doc_cache_collections = len(_document_cache)

    return {
        "warmup_complete": _warmup_complete,
        "mcp_query_cache_size": len(_query_cache),
        "mcp_query_cache_max": _QUERY_CACHE_MAX,
        "document_cache": {
            "loaded": _document_cache_loaded,
            "collections": doc_cache_collections,
            "chunks": doc_cache_chunks,
        },
        "embedding_cache": embed_stats,
        "rag_cache": rag_stats
    }

app = Server("posterchanai-rag")


@app.list_tools()
async def list_tools():
    """List available RAG tools."""
    return [
        Tool(
            name="search_codebase",
            description="Search the indexed codebase for relevant code snippets. Use this to find existing patterns, functions, classes, or implementations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language query describing what code you're looking for"
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5)",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="list_collections",
            description="List all available RAG collections (indexed codebases/documents)",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="reindex_collection",
            description="Re-index a RAG collection to pick up file changes. Use after git pull or code changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "integer",
                        "description": "Collection ID to re-index (use list_collections to find IDs)",
                        "default": 2
                    }
                }
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    """Handle tool calls."""

    if name == "search_codebase":
        return await search_codebase(
            query=arguments.get("query", ""),
            top_k=arguments.get("top_k", 5)
        )
    elif name == "list_collections":
        return await list_collections()
    elif name == "reindex_collection":
        return await reindex_collection(
            collection_id=arguments.get("collection_id", 2)
        )
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def search_codebase(query: str, top_k: int = 2):
    """Search the indexed codebase using existing RAG service."""
    if not query:
        return [TextContent(type="text", text="Error: query is required")]

    # Run blocking DB/embedding operations in bounded thread pool
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(_executor, _sync_search, query, top_k)

    if isinstance(results, str):  # Error message
        return [TextContent(type="text", text=results)]

    if not results:
        return [TextContent(
            type="text",
            text=f"No results found for: {query}\n\nMake sure you have indexed a codebase."
        )]

    # Format results
    output = f"## Found {len(results)} relevant code snippets\n\n"

    MAX_CHUNK_CHARS = 250  # Limit per chunk to avoid context overflow

    for i, result in enumerate(results, 1):
        file_path = result.get("file_path", "unknown")
        similarity = result.get("similarity", 0)
        content = result.get("content", "")
        collection = result.get("collection_name", "")

        # Truncate long chunks
        if len(content) > MAX_CHUNK_CHARS:
            content = content[:MAX_CHUNK_CHARS] + "\n... [truncated]"

        # Detect language from file extension
        ext = os.path.splitext(file_path)[1].lstrip('.')
        lang = ext if ext in ['py', 'js', 'ts', 'tsx', 'jsx', 'go', 'rs', 'java', 'md'] else ''

        output += f"### {i}. `{file_path}` ({similarity:.0%} match)\n"
        output += f"Collection: {collection}\n\n"
        output += f"```{lang}\n{content}\n```\n\n"

    return [TextContent(type="text", text=output)]


def _sync_search(query: str, top_k: int):
    """Synchronous search helper (runs in thread pool) with caching."""
    # Check cache first
    cache_key = hashlib.md5(f"{query}:{top_k}:{DEFAULT_USER_ID}".encode()).hexdigest()
    cached = _get_cached_result(cache_key)
    if cached is not None:
        logger.info(f"[MCP] Cache hit for query: {query[:50]}...")
        return cached

    db = SessionLocal()
    try:
        rag_service = get_rag_service(db, DEFAULT_USER_ID)
        results = rag_service.query(query, top_k=top_k)

        # Cache successful results
        _cache_result(cache_key, results)
        return results
    except Exception as e:
        logger.error(f"RAG search failed: {e}")
        return f"Error searching codebase: {str(e)}"
    finally:
        db.close()


async def list_collections():
    """List all RAG collections."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, _sync_list_collections)
    return [TextContent(type="text", text=result)]


async def reindex_collection(collection_id: int = 2):
    """Trigger re-indexing of a collection."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, _sync_reindex_collection, collection_id)
    return [TextContent(type="text", text=result)]


def _sync_reindex_collection(collection_id: int):
    """Synchronous reindex helper (runs in thread pool)."""
    db = SessionLocal()
    try:
        # Get collection
        collection = db.query(RAGCollection).filter(
            RAGCollection.id == collection_id,
            RAGCollection.user_id == DEFAULT_USER_ID
        ).first()

        if not collection:
            return f"Error: Collection {collection_id} not found"

        if collection.collection_type not in ("codebase", "folder"):
            return f"Error: Collection '{collection.name}' is type '{collection.collection_type}'. Only codebase/folder collections can be re-indexed."

        if not collection.source_path or not os.path.isdir(collection.source_path):
            return f"Error: Source path '{collection.source_path}' is not a valid directory"

        # Perform reindex using folder indexer
        indexer = get_folder_indexer(db, DEFAULT_USER_ID)
        indexer.index_folder(collection)

        # Refresh collection to get updated count
        db.refresh(collection)

        return f"Successfully re-indexed collection '{collection.name}' (id: {collection_id})\n- Source: {collection.source_path}\n- Documents: {collection.document_count}"

    except Exception as e:
        logger.error(f"Failed to reindex collection {collection_id}: {e}")
        return f"Error re-indexing: {str(e)}"
    finally:
        db.close()


def _sync_list_collections():
    """Synchronous list collections helper."""
    db = SessionLocal()
    try:
        collections = db.query(RAGCollection).filter(
            RAGCollection.user_id == DEFAULT_USER_ID
        ).all()

        if not collections:
            return "No RAG collections found. Index a codebase first."

        output = "## RAG Collections\n\n"
        for col in collections:
            output += f"- **{col.name}** (id: {col.id})\n"
            output += f"  - Type: {col.collection_type}\n"
            output += f"  - Source: {col.source_path}\n"
            output += f"  - Documents: {col.document_count}\n"
            output += f"  - Patterns: {col.file_patterns}\n\n"

        return output

    except Exception as e:
        logger.error(f"Failed to list collections: {e}")
        return f"Error: {str(e)}"
    finally:
        db.close()


async def main_stdio():
    """Run the MCP server over stdio (for local use)."""
    logger.info("Starting Posterchanai RAG MCP Server (stdio)...")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main_sse(host: str = "0.0.0.0", port: int = 8808):
    """Run the MCP server over SSE/HTTP (for network use)."""
    from mcp.server.sse import SseServerTransport
    import uvicorn
    import json

    sse = SseServerTransport("/messages/")

    async def asgi_app(scope, receive, send):
        """Pure ASGI app for MCP SSE server."""
        if scope["type"] != "http":
            return

        path = scope["path"]
        method = scope["method"]

        if path == "/sse" and method == "GET":
            # SSE connection endpoint
            async with sse.connect_sse(scope, receive, send) as streams:
                await app.run(streams[0], streams[1], app.create_initialization_options())

        elif path.startswith("/messages/") and method == "POST":
            # Message endpoint
            await sse.handle_post_message(scope, receive, send)

        elif path == "/reindex" and method == "POST":
            # Reindex endpoint - read JSON body
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break

            try:
                data = json.loads(body) if body else {}
                collection_id = data.get("collection_id", 2)
            except Exception:
                collection_id = 2

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_executor, _sync_reindex_collection, collection_id)

            response_body = json.dumps({"status": "ok", "message": result}).encode()
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            })
            await send({
                "type": "http.response.body",
                "body": response_body,
            })

        elif path == "/search" and method == "POST":
            # HTTP search endpoint for remote nodes
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break

            try:
                data = json.loads(body) if body else {}
                query = data.get("query", "")
                top_k = data.get("top_k", 3)
            except Exception:
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [[b"content-type", b"application/json"]],
                })
                await send({
                    "type": "http.response.body",
                    "body": json.dumps({"error": "Invalid JSON body"}).encode(),
                })
                return

            if not query:
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [[b"content-type", b"application/json"]],
                })
                await send({
                    "type": "http.response.body",
                    "body": json.dumps({"error": "query is required"}).encode(),
                })
                return

            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(_executor, _sync_search, query, top_k)

            if isinstance(results, str):  # Error message
                response_body = json.dumps({"error": results, "results": []}).encode()
            else:
                response_body = json.dumps({"results": results or []}).encode()

            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            })
            await send({
                "type": "http.response.body",
                "body": response_body,
            })

        elif path == "/status" and method == "GET":
            # Status endpoint - cache stats and health
            status = get_status()
            response_body = json.dumps(status).encode()
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            })
            await send({
                "type": "http.response.body",
                "body": response_body,
            })

        elif path == "/warmup" and method == "POST":
            # Warmup endpoint - pre-load model
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_executor, warmup_model)
            response_body = json.dumps(result).encode()
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            })
            await send({
                "type": "http.response.body",
                "body": response_body,
            })

        else:
            # 404 Not Found
            await send({
                "type": "http.response.start",
                "status": 404,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({
                "type": "http.response.body",
                "body": b"Not Found",
            })

    logger.info(f"Starting Posterchanai RAG MCP Server (SSE) on http://{host}:{port}")
    uvicorn.run(asgi_app, host=host, port=port)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Posterchanai RAG MCP Server")
    parser.add_argument("--sse", action="store_true", help="Run as SSE/HTTP server")
    parser.add_argument("--host", default="0.0.0.0", help="Host for SSE mode")
    parser.add_argument("--port", type=int, default=8808, help="Port for SSE mode")
    parser.add_argument("--warmup", action="store_true", help="Pre-load embedding model and cache all data")
    parser.add_argument("--no-docs", action="store_true", help="Skip loading documents into RAM (lighter warmup)")
    parser.add_argument("--workers", type=int, default=2, help="Max worker threads (default: 2)")
    args = parser.parse_args()

    # Update executor if custom worker count (module-level assignment)
    if args.workers != 2:
        _executor.shutdown(wait=False)  # Shutdown old executor
        _executor = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="rag_worker")
        logger.info(f"Using {args.workers} worker threads")

    # Warmup if requested
    if args.warmup:
        load_docs = not args.no_docs
        logger.info(f"Warming up (load_documents={load_docs})...")
        warmup_model(load_documents=load_docs)

    if args.sse:
        main_sse(args.host, args.port)
    else:
        asyncio.run(main_stdio())
