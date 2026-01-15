#!/bin/bash
# Hyprland Music Control Script for Posterchanai
# Controls the web music player via browser focus and keyboard shortcuts

# Default browser - Brave (adjust if needed)
# Try these patterns in order: brave, Brave, brave-browser, Brave-browser
BROWSER_CLASS="brave|Brave|brave-browser|Brave-browser"

ACTION="${1:-toggle}"

# Focus the browser window (try multiple Brave patterns)
FOCUSED=0
if command -v hyprctl &> /dev/null; then
    for pattern in "brave" "Brave" "brave-browser" "Brave-browser"; do
        if hyprctl dispatch focuswindow "class:.*$pattern" 2>/dev/null; then
            FOCUSED=1
            break
        fi
    done
    
    # Small delay to ensure focus
    if [ $FOCUSED -eq 1 ]; then
        sleep 0.2
    fi
fi

# Method 1: Use ydotool if available (Wayland-native, works globally)
if command -v ydotool &> /dev/null; then
    case "$ACTION" in
        play|pause|toggle)
            # Send Alt+P (play/pause)
            ydotool key 29:1 25:1 29:0 25:0 2>/dev/null
            ;;
        next)
            # Send Alt+F (next track)
            ydotool key 29:1 33:1 29:0 33:0 2>/dev/null
            ;;
        prev|previous)
            # Send Alt+R (previous track)
            ydotool key 29:1 19:1 29:0 19:0 2>/dev/null
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
            # Verify window still exists
            if xdotool getwindowname "$WINDOW_ID" &>/dev/null; then
                break
            else
                WINDOW_ID=""
            fi
        fi
    done
    
    if [ -z "$WINDOW_ID" ]; then
        echo "Browser window not found. Make sure Brave is running."
        exit 1
    fi
    
    case "$ACTION" in
        play|pause|toggle)
            xdotool key --window "$WINDOW_ID" alt+p 2>/dev/null || xdotool key alt+p
            ;;
        next)
            xdotool key --window "$WINDOW_ID" alt+f 2>/dev/null || xdotool key alt+f
            ;;
        prev|previous)
            xdotool key --window "$WINDOW_ID" alt+r 2>/dev/null || xdotool key alt+r
            ;;
        stop)
            xdotool key --window "$WINDOW_ID" alt+s 2>/dev/null || xdotool key alt+s
            ;;
        *)
            echo "Usage: $0 {play|pause|toggle|next|prev|previous|stop}"
            exit 1
            ;;
    esac
    exit 0
fi

# Method 3: Fallback - try to focus browser and use hyprctl to send keys
if command -v hyprctl &> /dev/null; then
    # Focus browser first
    for pattern in "brave" "Brave" "brave-browser" "Brave-browser"; do
        if hyprctl dispatch focuswindow "class:.*$pattern" &>/dev/null; then
            sleep 0.2  # Wait for focus
            break
        fi
    done
    
    case "$ACTION" in
        play|pause|toggle)
            # Use hyprctl to type Alt+P
            hyprctl dispatch exec "xdotool key alt+p" 2>/dev/null || echo "Browser focused. Press Alt+P to play/pause."
            ;;
        next)
            hyprctl dispatch exec "xdotool key alt+f" 2>/dev/null || echo "Browser focused. Press Alt+F for next track."
            ;;
        prev|previous)
            hyprctl dispatch exec "xdotool key alt+r" 2>/dev/null || echo "Browser focused. Press Alt+R for previous track."
            ;;
        stop)
            hyprctl dispatch exec "xdotool key alt+s" 2>/dev/null || echo "Browser focused. Press Alt+S to stop."
            ;;
        *)
            echo "Usage: $0 {play|pause|toggle|next|prev|previous|stop}"
            exit 1
            ;;
    esac
    exit 0
fi

# Final fallback - just focus browser
case "$ACTION" in
    play|pause|toggle|next|prev|previous|stop)
        echo "Browser focused. Use Alt+P (play/pause), Alt+F (next), Alt+R (prev), or Alt+S (stop) when browser is active."
        echo ""
        echo "Note: Install 'ydotool' or 'xdotool' for automatic keyboard control."
        echo "      Or use system media keys when browser tab is active."
        ;;
    *)
        echo "Usage: $0 {play|pause|toggle|next|prev|previous|stop}"
        echo ""
        echo "Note: Install 'ydotool' or 'xdotool' for full keyboard control."
        echo "      Or use system media keys when browser is focused."
        exit 1
        ;;
esac
