#!/bin/bash
# Hyprland Music Control Script for Posterchanai
# Controls the web music player via browser focus and keyboard shortcuts

# Default browser - Brave (adjust if needed)
# Try these patterns in order: brave, Brave, brave-browser, Brave-browser
BROWSER_CLASS="brave|Brave|brave-browser|Brave-browser"

ACTION="${1:-toggle}"

# Debug mode
if [ "$ACTION" = "--debug" ] || [ "$ACTION" = "-d" ]; then
    echo "=== Browser Window Detection Debug ==="
    echo ""
    
    if command -v hyprctl &> /dev/null; then
        echo "Hyprland clients:"
        hyprctl clients | grep -i "brave\|browser" | head -5
        echo ""
    fi
    
    if command -v xdotool &> /dev/null; then
        echo "xdotool search results:"
        for pattern in "brave" "Brave" "brave-browser" "Brave-browser"; do
            echo "  Pattern: $pattern"
            xdotool search --class "$pattern" 2>/dev/null | head -3
        done
        echo ""
    fi
    
    echo "Available tools:"
    command -v ydotool &> /dev/null && echo "  ✓ ydotool" || echo "  ✗ ydotool (not installed)"
    command -v xdotool &> /dev/null && echo "  ✓ xdotool" || echo "  ✗ xdotool (not installed)"
    command -v hyprctl &> /dev/null && echo "  ✓ hyprctl" || echo "  ✗ hyprctl (not installed)"
    echo ""
    echo "Usage: $0 {play|pause|toggle|next|prev|previous|stop|--debug}"
    exit 0
fi

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
# ydotool doesn't need window detection - it sends keys globally
if command -v ydotool &> /dev/null; then
    case "$ACTION" in
        play|pause|toggle)
            # Send Alt+P (play/pause)
            # Key codes: 29=Alt, 25=P
            if ydotool key 29:1 25:1 29:0 25:0 2>/dev/null; then
                exit 0
            else
                # Try with sudo if permission denied
                sudo ydotool key 29:1 25:1 29:0 25:0 2>/dev/null && exit 0 || true
            fi
            ;;
        next)
            # Send Alt+F (next track)
            # Key codes: 29=Alt, 33=F
            if ydotool key 29:1 33:1 29:0 33:0 2>/dev/null; then
                exit 0
            else
                sudo ydotool key 29:1 33:1 29:0 33:0 2>/dev/null && exit 0 || true
            fi
            ;;
        prev|previous)
            # Send Alt+R (previous track)
            # Key codes: 29=Alt, 19=R
            if ydotool key 29:1 19:1 29:0 19:0 2>/dev/null; then
                exit 0
            else
                sudo ydotool key 29:1 19:1 29:0 19:0 2>/dev/null && exit 0 || true
            fi
            ;;
        stop)
            # Send Alt+S (stop)
            # Key codes: 29=Alt, 31=S
            if ydotool key 29:1 31:1 29:0 31:0 2>/dev/null; then
                exit 0
            else
                sudo ydotool key 29:1 31:1 29:0 31:0 2>/dev/null && exit 0 || true
            fi
            ;;
        *)
            echo "Usage: $0 {play|pause|toggle|next|prev|previous|stop}"
            exit 1
            ;;
    esac
    # If we get here, ydotool failed
    echo "ydotool failed. Trying alternative methods..."
fi

# Method 2: Use xdotool if available (X11 compatibility layer)
# Note: xdotool may not work well in Wayland
if command -v xdotool &> /dev/null && [ -n "$DISPLAY" ]; then
    # Try to find browser window first
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
    
    # If window found, use it; otherwise send keys globally
    case "$ACTION" in
        play|pause|toggle)
            if [ -n "$WINDOW_ID" ]; then
                xdotool key --window "$WINDOW_ID" alt+p 2>/dev/null || xdotool key alt+p
            else
                xdotool key alt+p 2>/dev/null
            fi
            exit 0
            ;;
        next)
            if [ -n "$WINDOW_ID" ]; then
                xdotool key --window "$WINDOW_ID" alt+f 2>/dev/null || xdotool key alt+f
            else
                xdotool key alt+f 2>/dev/null
            fi
            exit 0
            ;;
        prev|previous)
            if [ -n "$WINDOW_ID" ]; then
                xdotool key --window "$WINDOW_ID" alt+r 2>/dev/null || xdotool key alt+r
            else
                xdotool key alt+r 2>/dev/null
            fi
            exit 0
            ;;
        stop)
            if [ -n "$WINDOW_ID" ]; then
                xdotool key --window "$WINDOW_ID" alt+s 2>/dev/null || xdotool key alt+s
            else
                xdotool key alt+s 2>/dev/null
            fi
            exit 0
            ;;
        *)
            echo "Usage: $0 {play|pause|toggle|next|prev|previous|stop}"
            exit 1
            ;;
    esac
fi

# Method 3: Final fallback - focus browser and inform user
# Browser is already focused from the beginning of the script
case "$ACTION" in
    play|pause|toggle)
        echo "Browser focused. Press Alt+P to play/pause."
        echo ""
        echo "To enable automatic control, install ydotool:"
        echo "  sudo emerge -av app-misc/ydotool"
        echo ""
        echo "Or use system media keys when browser tab is active."
        ;;
    next)
        echo "Browser focused. Press Alt+F for next track."
        echo ""
        echo "To enable automatic control, install ydotool:"
        echo "  sudo emerge -av app-misc/ydotool"
        ;;
    prev|previous)
        echo "Browser focused. Press Alt+R for previous track."
        echo ""
        echo "To enable automatic control, install ydotool:"
        echo "  sudo emerge -av app-misc/ydotool"
        ;;
    stop)
        echo "Browser focused. Press Alt+S to stop."
        echo ""
        echo "To enable automatic control, install ydotool:"
        echo "  sudo emerge -av app-misc/ydotool"
        ;;
    *)
        echo "Usage: $0 {play|pause|toggle|next|prev|previous|stop}"
        echo ""
        echo "Note: Install 'ydotool' for automatic keyboard control:"
        echo "  sudo emerge -av app-misc/ydotool"
        echo ""
        echo "Or use system media keys when browser tab is active."
        exit 1
        ;;
esac
