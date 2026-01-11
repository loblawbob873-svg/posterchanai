#!/bin/bash
# Run Posterchanai Terminal UI
#
# Usage:
#   ./run-tui.sh                    # Connect to localhost:3051
#   ./run-tui.sh --server URL       # Connect to custom server
#   ./run-tui.sh --debug            # Enable debug logging to tui/tui.log
#
# Logs: tui/tui.log (use: tail -f tui/tui.log)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TUI_DIR="$SCRIPT_DIR/tui"

# Check if TUI venv exists
if [ ! -d "$TUI_DIR/.venv" ]; then
    echo "TUI not installed. Run ./install.sh and select TUI option."
    exit 1
fi

# Run TUI from parent directory with correct PYTHONPATH
cd "$SCRIPT_DIR"
exec "$TUI_DIR/.venv/bin/python" -m tui "$@"
