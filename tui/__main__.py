#!/usr/bin/env python3
"""
Posterchanai TUI - Terminal Chat Client

Usage:
    python -m tui [options]

Options:
    --server URL    Server URL (default: http://localhost:3051)
    --debug         Enable debug logging to tui.log
    --help          Show this help message
"""

import argparse
import sys
import os
import logging

# Add parent directory to path if running from within tui/
# This allows both `python -m tui` from parent and `python -m tui` from tui/
_this_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_this_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)


def setup_logging(debug: bool = False):
    """Configure logging to file."""
    log_file = os.path.join(_this_dir, "tui.log")
    level = logging.DEBUG if debug else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, mode="w"),
        ]
    )

    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    return log_file


def main():
    parser = argparse.ArgumentParser(
        description="Posterchanai TUI - Terminal Chat Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m tui                              # Connect to localhost:3051
    python -m tui --server http://myserver:3051  # Connect to custom server
    python -m tui --debug                      # Enable debug logging
        """
    )
    parser.add_argument(
        "--server", "-s",
        default=None,
        help="Server URL (default: http://localhost:3051)"
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug logging to tui.log"
    )

    args = parser.parse_args()

    # Setup logging
    log_file = setup_logging(args.debug)
    logging.getLogger("tui").info(f"Logging to {log_file}")

    # Import here to avoid slow startup for --help
    from tui.config import load_config
    from tui.app import ChatApp

    config = load_config()
    if args.server:
        config.server_url = args.server

    app = ChatApp(config=config)
    app.run()


if __name__ == "__main__":
    main()
