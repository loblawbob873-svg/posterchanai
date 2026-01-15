# Hyprland Music Player Controls

This guide explains how to configure Hyprland to control the Posterchanai web music player.

## Method 1: System Media Keys (Recommended - Automatic)

The music player uses the **Media Session API**, which automatically responds to system media keys. If your keyboard has media keys (Play/Pause, Next, Previous), they should work automatically when the browser tab is active.

**No configuration needed** - just use your keyboard's media keys!

## Method 2: Custom Global Shortcuts

If you want custom keyboard shortcuts that work globally (even when browser isn't focused), you have a few options:

### Quick Start (Using Provided Script)

Posterchanai includes a ready-to-use script at `scripts/hyprland-music-control.sh` that's **pre-configured for Brave browser**:

Add to `~/.config/hypr/hyprland.conf`:

```bash
# Custom keyboard shortcuts (Alt+P, Alt+F, Alt+R, Alt+S)
bind = ALT, P, exec, /path/to/posterchanai/scripts/hyprland-music-control.sh toggle
bind = ALT, F, exec, /path/to/posterchanai/scripts/hyprland-music-control.sh next
bind = ALT, R, exec, /path/to/posterchanai/scripts/hyprland-music-control.sh prev
bind = ALT, S, exec, /path/to/posterchanai/scripts/hyprland-music-control.sh stop

# Alternative: Use Super key instead of Alt
# bind = SUPER, P, exec, /path/to/posterchanai/scripts/hyprland-music-control.sh toggle
# bind = SUPER, F, exec, /path/to/posterchanai/scripts/hyprland-music-control.sh next
# bind = SUPER, R, exec, /path/to/posterchanai/scripts/hyprland-music-control.sh prev

# Optional: Also bind media keys if your keyboard has them
# bind = , XF86AudioPlay, exec, /path/to/posterchanai/scripts/hyprland-music-control.sh toggle
# bind = , XF86AudioNext, exec, /path/to/posterchanai/scripts/hyprland-music-control.sh next
# bind = , XF86AudioPrev, exec, /path/to/posterchanai/scripts/hyprland-music-control.sh prev
```

The script automatically:
- Detects Brave browser (tries multiple class patterns)
- Uses `ydotool` (preferred) or `xdotool` if available
- Focuses the browser window before sending keys

### Manual Setup Options

### Option A: Use `playerctl` (if browser supports MPRIS)

Some browsers (like Firefox with extensions, or Chromium with flags) support MPRIS, which allows `playerctl` to control them:

```bash
# Install playerctl
sudo emerge -av media-sound/playerctl  # Gentoo
# or
sudo pacman -S playerctl  # Arch
# or
sudo apt install playerctl  # Debian/Ubuntu
```

Then add to `~/.config/hypr/hyprland.conf`:

```bash
# Media controls via playerctl
bind = , XF86AudioPlay, exec, playerctl play-pause
bind = , XF86AudioNext, exec, playerctl next
bind = , XF86AudioPrev, exec, playerctl previous
bind = , XF86AudioStop, exec, playerctl stop

# Custom shortcuts (optional)
bind = SUPER, P, exec, playerctl play-pause
bind = SUPER, N, exec, playerctl next
bind = SUPER, B, exec, playerctl previous
```

### Option B: Browser Extension + Native Messaging

For true global control, you can use a browser extension that communicates with a native script:

1. **Create a control script** (`~/.local/bin/posterchanai-music-control.sh`):

```bash
#!/bin/bash
# Control Posterchanai music player via browser

BROWSER="firefox"  # or "chromium", "google-chrome", etc.
ACTION="$1"

# Focus browser window
hyprctl dispatch focuswindow "class:.*[Bb]rowser.*"

# Send keyboard shortcut to browser
case "$ACTION" in
    play|pause|toggle)
        # Send Space or Alt+P to browser
        xdotool key --window $(xdotool search --class "$BROWSER" | head -1) space
        ;;
    next)
        # Send Alt+F (next track)
        xdotool key --window $(xdotool search --class "$BROWSER" | head -1) alt+f
        ;;
    prev|previous)
        # Send Alt+R (previous track)
        xdotool key --window $(xdotool search --class "$BROWSER" | head -1) alt+r
        ;;
    stop)
        # Send Alt+S
        xdotool key --window $(xdotool search --class "$BROWSER" | head -1) alt+s
        ;;
esac
```

Make it executable:
```bash
chmod +x ~/.local/bin/posterchanai-music-control.sh
```

Install `xdotool`:
```bash
sudo emerge -av x11-apps/xdotool  # Gentoo
```

Add to `~/.config/hypr/hyprland.conf`:

```bash
bind = SUPER, P, exec, ~/.local/bin/posterchanai-music-control.sh toggle
bind = SUPER, N, exec, ~/.local/bin/posterchanai-music-control.sh next
bind = SUPER, B, exec, ~/.local/bin/posterchanai-music-control.sh prev
bind = SUPER, S, exec, ~/.local/bin/posterchanai-music-control.sh stop
```

### Option C: Use `ydotool` (Wayland-native, Recommended for Wayland)

`ydotool` is a Wayland-native tool that can send keyboard events:

```bash
# Install ydotool
sudo emerge -av app-misc/ydotool  # Gentoo
```

Create script (`~/.local/bin/posterchanai-music-control.sh`):

```bash
#!/bin/bash
# Control Posterchanai music player via browser (Wayland)

BROWSER="firefox"  # or "chromium"
ACTION="$1"

# Focus browser window
hyprctl dispatch focuswindow "class:.*[Bb]rowser.*"
sleep 0.1  # Wait for focus

# Send keyboard shortcut
case "$ACTION" in
    play|pause|toggle)
        ydotool key 57:1 57:0  # Space key
        ;;
    next)
        ydotool key 29:1 33:1 29:0 33:0  # Alt+F
        ;;
    prev|previous)
        ydotool key 29:1 19:1 29:0 19:0  # Alt+R
        ;;
    stop)
        ydotool key 29:1 31:1 29:0 31:0  # Alt+S
        ;;
esac
```

**Note:** You need to run `ydotool` as root or set up proper permissions. See `ydotool` documentation.

### Option D: Simple Browser Focus + Media Keys

The simplest approach - just focus the browser and let Media Session API handle it:

```bash
# ~/.local/bin/posterchanai-music-control.sh
#!/bin/bash
hyprctl dispatch focuswindow "class:.*[Bb]rowser.*"
# Media keys will be handled by browser's Media Session API
```

Then in Hyprland config:

```bash
# Focus browser and let Media Session API handle media keys
bind = , XF86AudioPlay, exec, hyprctl dispatch focuswindow "class:.*[Bb]rowser.*"
bind = , XF86AudioNext, exec, hyprctl dispatch focuswindow "class:.*[Bb]rowser.*"
bind = , XF86AudioPrev, exec, hyprctl dispatch focuswindow "class:.*[Bb]rowser.*"
```

## Method 3: Use Waybar Widget

You can add a Waybar widget that controls the music player. This requires a browser extension or API endpoint.

## Recommended Setup

For most users, **Method 1 (System Media Keys)** is the best option:

1. The Media Session API is already implemented
2. Works automatically with keyboard media keys
3. No additional configuration needed
4. Works when browser tab is active

If you need global shortcuts that work when browser isn't focused, use **Option C (ydotool)** or **Option D (Browser Focus)**.

## Testing

After configuring, test your shortcuts:

1. Open Posterchanai in your browser
2. Start playing music
3. Press your configured shortcuts
4. Music should respond accordingly

## Troubleshooting

- **Media keys don't work**: Make sure the browser tab with Posterchanai is active/focused
- **Custom shortcuts don't work**: Check that the script is executable and the browser window class matches
- **ydotool permission errors**: Run `sudo ydotool` or configure udev rules (see ydotool docs)

## Browser Window Class Names

To find your browser's window class for Hyprland:

```bash
hyprctl clients | grep -i brave
```

Common classes:
- **Brave**: `brave`, `Brave`, `brave-browser`, `Brave-browser`
- Firefox: `firefox`, `Firefox`
- Chromium: `chromium`, `Chromium`
- Chrome: `google-chrome`, `Google-chrome`

**For Brave users:** The scripts are pre-configured to detect Brave automatically. If you have issues, check which pattern your Brave uses:

```bash
hyprctl clients | grep -i brave
```

Then adjust `BROWSER_CLASS` in the scripts if needed.

Adjust the `focuswindow` command accordingly.
