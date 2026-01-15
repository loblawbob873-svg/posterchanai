#!/bin/bash
# Setup script to add Hyprland keybinds for music controls
# This adds Alt+P, Alt+F, Alt+R, Alt+S shortcuts

HYPRLAND_CONFIG="${HOME}/.config/hypr/hyprland.conf"
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hyprland-music-control.sh"
KEYBINDS_MARKER="# Posterchanai Music Controls"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}Setting up Hyprland keybinds for music controls${NC}"
echo ""

# Check if Hyprland config exists
if [ ! -f "$HYPRLAND_CONFIG" ]; then
    echo -e "${RED}Error: Hyprland config not found at $HYPRLAND_CONFIG${NC}"
    echo "Create it first or specify a different path"
    exit 1
fi

# Check if keybinds already exist
if grep -q "$KEYBINDS_MARKER" "$HYPRLAND_CONFIG"; then
    echo -e "${YELLOW}Keybinds already exist in config${NC}"
    echo "Current keybinds:"
    grep -A 4 "$KEYBINDS_MARKER" "$HYPRLAND_CONFIG" || true
    echo ""
    read -p "Do you want to replace them? [y/N]: " replace
    if [[ ! "$replace" =~ ^[Yy]$ ]]; then
        echo "Keeping existing keybinds"
        exit 0
    fi
    # Remove old keybinds
    sed -i "/$KEYBINDS_MARKER/,/^$/d" "$HYPRLAND_CONFIG"
fi

# Add keybinds
echo "" >> "$HYPRLAND_CONFIG"
echo "$KEYBINDS_MARKER" >> "$HYPRLAND_CONFIG"
echo "bind = ALT, P, exec, $SCRIPT_PATH toggle" >> "$HYPRLAND_CONFIG"
echo "bind = ALT, F, exec, $SCRIPT_PATH next" >> "$HYPRLAND_CONFIG"
echo "bind = ALT, R, exec, $SCRIPT_PATH prev" >> "$HYPRLAND_CONFIG"
echo "bind = ALT, S, exec, $SCRIPT_PATH stop" >> "$HYPRLAND_CONFIG"
echo "" >> "$HYPRLAND_CONFIG"

echo -e "${GREEN}✓ Keybinds added to $HYPRLAND_CONFIG${NC}"
echo ""
echo "Added shortcuts:"
echo "  Alt+P - Play/Pause"
echo "  Alt+F - Skip forward (next track)"
echo "  Alt+R - Skip back (previous track)"
echo "  Alt+S - Stop"
echo ""
echo "To apply, reload Hyprland:"
echo "  hyprctl reload"
echo ""
echo "Or restart Hyprland"
