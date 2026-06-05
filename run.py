#!/usr/bin/env python3
import argparse
import os
import sys
import uvicorn


def check_ipex_environment():
    """Check if IPEX backend is configured and oneAPI environment is available."""
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), "posterchanai.db")
        if not os.path.exists(db_path):
            return  # Fresh install, no DB yet

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'llm_backend'")
        row = cursor.fetchone()
        conn.close()

        if row and row[0] == "ipex":
            # Check if oneAPI is in environment
            ld_path = os.environ.get("LD_LIBRARY_PATH", "")
            if "/intel/oneapi" not in ld_path and "/opt/intel" not in ld_path:
                print("=" * 60, file=sys.stderr)
                print("WARNING: IPEX backend selected but oneAPI environment not found!", file=sys.stderr)
                print("", file=sys.stderr)
                print("For Intel GPU acceleration, start with:", file=sys.stderr)
                print("  ./run-intel.sh --port 3051", file=sys.stderr)
                print("", file=sys.stderr)
                print("Or source oneAPI first:", file=sys.stderr)
                print("  source /opt/intel/oneapi/setvars.sh", file=sys.stderr)
                print("  python run.py --port 3051", file=sys.stderr)
                print("", file=sys.stderr)
                print("Continuing anyway (will fall back to CPU mode)...", file=sys.stderr)
                print("=" * 60, file=sys.stderr)
    except Exception as e:
        # Don't fail startup on check errors
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Posterchanai Server")
    parser.add_argument("--port", type=int, default=None, help="Port to run on (default: 3051)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    # Check IPEX environment before starting
    check_ipex_environment()

    # Port priority: CLI arg > env var > default
    port = args.port or int(os.environ.get("POSTERCHANAI_PORT", "3051"))

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=port,
        reload=False,
        # Chat uploads (images/PDFs/videos for compress/convert) are sent as
        # base64 over the WebSocket; the 16 MB default drops large frames and
        # the message never arrives. Raise to 64 MB.
        ws_max_size=64 * 1024 * 1024,
    )
