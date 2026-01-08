#!/usr/bin/env python3
import argparse
import os
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Posterchanai Server")
    parser.add_argument("--port", type=int, default=None, help="Port to run on (default: 3051)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    # Port priority: CLI arg > env var > default
    port = args.port or int(os.environ.get("POSTERCHANAI_PORT", "3051"))

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=port,
        reload=False
    )
