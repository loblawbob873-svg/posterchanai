#!/bin/bash
# Music control script for global Wayland shortcuts
# Works with Posterchanai TUI by writing to a control file
#
# Add to your Hyprland config (~/.config/hypr/hyprland.conf):
#   bind = SUPER, P, exec, /path/to/music-control.sh toggle
#   bind = SUPER, F, exec, /path/to/music-control.sh next
#   bind = SUPER, R, exec, /path/to/music-control.sh prev
#
# For Sway (~/.config/sway/config):
#   bindsym Mod4+p exec /path/to/music-control.sh toggle
#   bindsym Mod4+f exec /path/to/music-control.sh next
#   bindsym Mod4+r exec /path/to/music-control.sh prev

CONTROL_FILE="/tmp/posterchanai-music-control"

case "$1" in
    toggle|pause|play)
        echo "toggle" > "$CONTROL_FILE"
        ;;
    next|forward|skip)
        echo "next" > "$CONTROL_FILE"
        ;;
    prev|back|previous)
        echo "prev" > "$CONTROL_FILE"
        ;;
    *)
        echo "Posterchanai TUI Music Control"
        echo ""
        echo "Usage: $0 {toggle|next|prev}"
        echo ""
        echo "Commands:"
        echo "  toggle/pause/play - Toggle playback (Super+P)"
        echo "  next/forward/skip - Next track (Super+F)"
        echo "  prev/back/previous - Previous track (Super+R)"
        echo ""
        echo "Hyprland config example:"
        echo "  bind = SUPER, P, exec, $0 toggle"
        echo "  bind = SUPER, F, exec, $0 next"
        echo "  bind = SUPER, R, exec, $0 prev"
        exit 1
        ;;
esac
