#!/usr/bin/env python3
"""
Database migration script for posterchanai.

This script ensures all required settings exist in the database.
It's safe to run multiple times - it only adds missing settings.

Usage:
    python scripts/migrate.py

Or from the installer:
    ./install.sh --migrate
"""

import sys
import os

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Database path
DB_PATH = os.environ.get("DATABASE_URL", "sqlite:///./data/posterchanai.db")

# All settings that should exist with their default values
# This should match the defaults in app/database.py
REQUIRED_SETTINGS = {
    # ComfyUI settings
    "comfyui_url": "http://localhost:8188",
    "comfyui_default_model": "",
    "comfyui_anime_model": "",
    "comfyui_timeout": "300000",
    # Native image generation settings
    "image_backend": "comfyui",
    "image_model_path": "",
    "image_anime_model_path": "",
    "image_model_type": "sdxl",
    "image_default_steps": "20",
    "image_default_cfg": "7.0",
    "image_default_width": "1024",
    "image_default_height": "1024",
    "image_gpu_device": "auto",
    "image_idle_timeout": "120",
    "image_subprocess_mode": "false",
    # VRAM management
    "vram_mode": "shared",
    "searxng_url": "",
    "torrent_site_url": "",
    # TTS settings
    "tts_voice": "en-GB-SoniaNeural",
    "tts_rate": "+5%",
    "tts_pitch": "+10Hz",
    "upload_path": "/var/lib/posterchanai",
    # Ollama settings
    "ollama_url": "http://localhost:11434",
    "ollama_api_format": "ollama",
    "ollama_model": "",
    "ollama_timeout": "120000",
    "ollama_max_concurrent": "1",
    "ollama_system_prompt": "",
    # Advanced model settings
    "ollama_temperature": "0.2",
    "ollama_top_p": "0.9",
    "ollama_top_k": "40",
    "ollama_repeat_penalty": "1.1",
    "ollama_num_ctx": "16384",
    "ollama_num_predict": "8192",
    "ollama_keep_alive": "-1",
    "ollama_stop": "",
    "ollama_seed": "",
    "ollama_mirostat": "0",
    "ollama_mirostat_eta": "0.1",
    "ollama_mirostat_tau": "5.0",
    "ollama_tfs_z": "1.0",
    # API settings
    "openai_api_key": "",
    # Registration settings
    "allow_registration": "false",
    # Load balancing
    "chat_server_urls": "",
    "image_server_urls": "",
    # Native LLM settings
    "llm_backend": "ollama",
    "llm_model_path": "",
    "llm_gpu_layers": "-1",
    "llm_n_threads": "0",
    "llm_n_batch": "1024",
    "llm_max_concurrent": "1",
    # CPU optimization settings
    "llm_cpu_mode": "false",
    "llm_use_mmap": "true",
    "llm_use_mlock": "true",
    "llm_idle_timeout": "0",
    # LLM health check
    "ollama_ping_enabled": "false",
    "ollama_restart_command": "sudo systemctl restart ollama",
    "ollama_ping_interval": "90",
    "ollama_restart_after_failures": "2",
    # GPU memory monitoring
    "gpu_memory_check_enabled": "false",
    "gpu_memory_threshold": "99",
    "gpu_type": "nvidia",
    # Email settings (SMTP)
    "smtp_enabled": "false",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_username": "",
    "smtp_password": "",
    "smtp_from_email": "",
    "smtp_from_name": "Posterchanai",
    "smtp_use_tls": "true",
    "smtp_use_ssl": "false",
    # Email settings (IMAP)
    "imap_enabled": "false",
    "imap_host": "",
    "imap_port": "993",
    "imap_username": "",
    "imap_password": "",
    "imap_use_ssl": "true",
    "imap_sent_folder": "Sent",
    # News sources
    "news_sources": "",
    # RAG settings
    "rag_enabled": "true",
    "rag_embedding_model": "all-MiniLM-L6-v2",
    "rag_chunk_size": "2000",
    "rag_chunk_overlap": "100",
    "rag_top_k": "5",
    "rag_min_similarity": "0.3",
    "rag_max_file_size": "1",
    "rag_max_log_size": "100",
    "rag_embedding_batch_size": "64",
    "rag_num_threads": "0",
    "rag_chromadb_path": "./data/chromadb",
    "rag_auto_context": "true",
    "rag_auto_warmup": "true",
    "rag_max_context_chars": "32000",
    "rag_max_chunk_display": "8000",
    "rag_max_chunk_index": "10000",
    "rag_api_url": "",
    # RAG cache settings
    "rag_query_cache_max": "100000",
    "rag_query_cache_ttl": "600",
    "rag_embedding_cache_max": "250000",
    # ChromaDB HNSW tuning
    "rag_hnsw_ef_search": "100",
    "rag_hnsw_ef_construction": "200",
    "rag_hnsw_m": "16",
    # MCP Server settings
    "mcp_enabled": "true",
    "mcp_port": "8808",
    "mcp_host": "0.0.0.0",
    "mcp_warmup": "true",
    # Native RSS settings
    "rss_enabled": "false",
}


def get_db_path():
    """Get the database path, checking common locations."""
    # Check environment variable first
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]

    # Check common locations
    locations = [
        "./data/posterchanai.db",
        "../data/posterchanai.db",
        "/var/lib/posterchanai/posterchanai.db",
    ]

    for loc in locations:
        if os.path.exists(loc):
            return f"sqlite:///{loc}"

    # Default
    return "sqlite:///./data/posterchanai.db"


def run_schema_migration(session, verbose=True):
    """Add new columns to existing tables."""
    schema_migrations = [
        # (table, column, type, default)
        ("users", "rss_enabled", "BOOLEAN", "0"),
    ]

    for table, column, col_type, default in schema_migrations:
        try:
            # Check if column exists
            result = session.execute(text(f"PRAGMA table_info({table})"))
            columns = [row[1] for row in result.fetchall()]
            if column not in columns:
                session.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type} DEFAULT {default}"
                ))
                if verbose:
                    print(f"  + Added column: {table}.{column}")
        except Exception as e:
            if verbose:
                print(f"  ! Could not add {table}.{column}: {e}")


def run_table_migration(session, verbose=True):
    """Create new tables if they don't exist."""
    # Check if rss_feeds table exists
    result = session.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='rss_feeds'"
    ))
    if not result.fetchone():
        if verbose:
            print("  + Creating rss_feeds table")
        session.execute(text("""
            CREATE TABLE rss_feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                url VARCHAR(500) NOT NULL,
                title VARCHAR(255),
                custom_name VARCHAR(255),
                enabled BOOLEAN DEFAULT 1,
                last_fetched_at DATETIME,
                last_error TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """))

    # Check if rss_entries table exists
    result = session.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='rss_entries'"
    ))
    if not result.fetchone():
        if verbose:
            print("  + Creating rss_entries table")
        session.execute(text("""
            CREATE TABLE rss_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER NOT NULL,
                guid VARCHAR(500) NOT NULL,
                title VARCHAR(500) NOT NULL,
                url VARCHAR(500),
                content TEXT,
                summary TEXT,
                published_at DATETIME,
                is_read BOOLEAN DEFAULT 0,
                is_summarized BOOLEAN DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (feed_id) REFERENCES rss_feeds(id) ON DELETE CASCADE
            )
        """))
        session.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_rss_entries_guid ON rss_entries(feed_id, guid)"
        ))


def run_migration(db_url=None, verbose=True):
    """
    Run database migration to add missing settings.

    Args:
        db_url: SQLAlchemy database URL (optional, auto-detected if not provided)
        verbose: Print progress messages

    Returns:
        Tuple of (added_count, skipped_count)
    """
    if db_url is None:
        db_url = get_db_path()

    if verbose:
        print(f"Connecting to database: {db_url}")

    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    added = 0
    skipped = 0

    try:
        # Check if settings table exists
        result = session.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
        ))
        if not result.fetchone():
            if verbose:
                print("Settings table does not exist. Run the app first to create tables.")
            return (0, 0)

        # Run schema migrations (new columns)
        if verbose:
            print("Checking schema...")
        run_schema_migration(session, verbose)

        # Run table migrations (new tables)
        if verbose:
            print("Checking tables...")
        run_table_migration(session, verbose)

        # Get existing settings
        result = session.execute(text("SELECT key FROM settings"))
        existing_keys = {row[0] for row in result.fetchall()}

        if verbose:
            print(f"Found {len(existing_keys)} existing settings")

        # Add missing settings
        for key, default_value in REQUIRED_SETTINGS.items():
            if key not in existing_keys:
                session.execute(
                    text("INSERT INTO settings (key, value) VALUES (:key, :value)"),
                    {"key": key, "value": default_value}
                )
                if verbose:
                    print(f"  + Added: {key}")
                added += 1
            else:
                skipped += 1

        session.commit()

        if verbose:
            print(f"\nMigration complete: {added} settings added, {skipped} already existed")

        return (added, skipped)

    except Exception as e:
        session.rollback()
        if verbose:
            print(f"Error during migration: {e}")
        raise
    finally:
        session.close()


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Migrate posterchanai database to add missing settings"
    )
    parser.add_argument(
        "--db",
        help="Database URL (default: auto-detect)",
        default=None
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress output"
    )

    args = parser.parse_args()

    try:
        added, skipped = run_migration(args.db, verbose=not args.quiet)
        if added > 0:
            print(f"\nSuccess! Added {added} new settings.")
            print("Restart the service to apply changes:")
            print("  sudo systemctl restart posterchanai")
        sys.exit(0)
    except Exception as e:
        print(f"Migration failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
