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
