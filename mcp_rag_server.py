#!/usr/bin/env python3
"""
MCP Server for Posterchanai RAG.
Exposes the existing RAG service to MCP clients like Continue.dev, Claude Desktop, etc.

Usage:
    python mcp_rag_server.py

Configure in Continue.dev config.json:
{
  "mcpServers": {
    "posterchanai-rag": {
      "command": "python",
      "args": ["/home/verita84/posterchanai/mcp_rag_server.py"]
    }
  }
}
"""
import sys
import os
import asyncio
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Import existing RAG infrastructure
from app.database import SessionLocal
from app.models import RAGCollection
from app.services.rag_service import get_rag_service
from app.services.rag_folder_service import get_folder_indexer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# User ID from environment or default
DEFAULT_USER_ID = int(os.environ.get("RAG_USER_ID", "1"))

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

    # Run blocking DB/embedding operations in thread pool
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, _sync_search, query, top_k)

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
    """Synchronous search helper (runs in thread pool)."""
    db = SessionLocal()
    try:
        rag_service = get_rag_service(db, DEFAULT_USER_ID)
        return rag_service.query(query, top_k=top_k)
    except Exception as e:
        logger.error(f"RAG search failed: {e}")
        return f"Error searching codebase: {str(e)}"
    finally:
        db.close()


async def list_collections():
    """List all RAG collections."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _sync_list_collections)
    return [TextContent(type="text", text=result)]


async def reindex_collection(collection_id: int = 2):
    """Trigger re-indexing of a collection."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _sync_reindex_collection, collection_id)
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
            result = await loop.run_in_executor(None, _sync_reindex_collection, collection_id)

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
    args = parser.parse_args()

    if args.sse:
        main_sse(args.host, args.port)
    else:
        asyncio.run(main_stdio())
