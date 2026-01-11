#!/usr/bin/env python3
"""
Posterchanai TUI - Terminal Chat Client

Usage:
    python -m tui [options]

Options:
    --server URL    Server URL (default: http://localhost:3051)
    --help          Show this help message
"""

import argparse
import sys
import os

# Add parent directory to path if running from within tui/
# This allows both `python -m tui` from parent and `python -m tui` from tui/
_this_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_this_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)


def main():
    parser = argparse.ArgumentParser(
        description="Posterchanai TUI - Terminal Chat Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m tui                              # Connect to localhost:3051
    python -m tui --server http://myserver:3051  # Connect to custom server
        """
    )
    parser.add_argument(
        "--server", "-s",
        default=None,
        help="Server URL (default: http://localhost:3051)"
    )

    args = parser.parse_args()

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
