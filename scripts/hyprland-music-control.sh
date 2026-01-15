#!/bin/bash
# Hyprland Music Control Script for Posterchanai
# Controls the web music player via browser focus and keyboard shortcuts

# Default browser - Brave (adjust if needed)
# Try these patterns in order: brave, Brave, brave-browser, Brave-browser
BROWSER_CLASS="brave|Brave|brave-browser|Brave-browser"

ACTION="${1:-toggle}"

# Focus the browser window (try multiple Brave patterns)
FOCUSED=0
for pattern in "brave" "Brave" "brave-browser" "Brave-browser"; do
    if hyprctl dispatch focuswindow "class:.*$pattern" 2>/dev/null; then
        FOCUSED=1
        break
    fi
done

# Small delay to ensure focus
sleep 0.1

# Method 1: Use ydotool if available (Wayland-native, works globally)
if command -v ydotool &> /dev/null; then
    case "$ACTION" in
        play|pause|toggle)
            # Send Space key (play/pause)
            ydotool key 57:1 57:0 2>/dev/null
            ;;
        next)
            # Send Alt+N (next track)
            ydotool key 29:1 49:1 29:0 49:0 2>/dev/null
            ;;
        prev|previous)
            # Send Alt+B (previous track)
            ydotool key 29:1 48:1 29:0 48:0 2>/dev/null
            ;;
        stop)
            # Send Alt+S (stop)
            ydotool key 29:1 31:1 29:0 31:0 2>/dev/null
            ;;
        *)
            echo "Usage: $0 {play|pause|toggle|next|prev|previous|stop}"
            exit 1
            ;;
    esac
    exit 0
fi

# Method 2: Use xdotool if available (X11 compatibility layer)
if command -v xdotool &> /dev/null; then
    # Find browser window (try multiple Brave patterns)
    WINDOW_ID=""
    for pattern in "brave" "Brave" "brave-browser" "Brave-browser"; do
        WINDOW_ID=$(xdotool search --class "$pattern" 2>/dev/null | head -1)
        if [ -n "$WINDOW_ID" ]; then
            break
        fi
    done
    
    if [ -z "$WINDOW_ID" ]; then
        echo "Browser window not found"
        exit 1
    fi
    
    case "$ACTION" in
        play|pause|toggle)
            xdotool key --window "$WINDOW_ID" space
            ;;
        next)
            xdotool key --window "$WINDOW_ID" alt+n
            ;;
        prev|previous)
            xdotool key --window "$WINDOW_ID" alt+b
            ;;
        stop)
            xdotool key --window "$WINDOW_ID" alt+s
            ;;
        *)
            echo "Usage: $0 {play|pause|toggle|next|prev|previous|stop}"
            exit 1
            ;;
    esac
    exit 0
fi

# Method 3: Fallback - just focus browser (Media Session API will handle media keys)
# This works if you have media keys and the browser tab is active
case "$ACTION" in
    play|pause|toggle|next|prev|previous|stop)
        # Browser is already focused, Media Session API should handle media keys
        # If you have physical media keys, they should work now
        echo "Browser focused. Use media keys or ensure browser tab is active."
        ;;
    *)
        echo "Usage: $0 {play|pause|toggle|next|prev|previous|stop}"
        echo ""
        echo "Note: Install 'ydotool' or 'xdotool' for full keyboard control."
        echo "      Or use system media keys when browser is focused."
        exit 1
        ;;
esac
