# Testing Music Player Controls

This guide explains how to use the testing script to verify and troubleshoot music player controls.

## Quick Start

Run the interactive test menu:
```bash
./scripts/test-music-controls.sh
```

Or run all tests at once:
```bash
./scripts/test-music-controls.sh --all
```

## Configuration

Edit the variables at the top of `scripts/test-music-controls.sh` to tweak settings:

```bash
# Configuration (tweak these)
BROWSER_CLASS=".*[Bb]rowser.*"              # Browser window class pattern
POSTERCHANAI_URL="${POSTERCHANAI_URL:-http://localhost:8000}"  # Web interface URL
CONTROL_SCRIPT="${SCRIPT_DIR}/hyprland-music-control.sh"  # Control script path
TEST_DELAY=2  # Delay between tests (seconds)
```

## Test Options

### Command Line Options

```bash
# Run all tests
./scripts/test-music-controls.sh --all

# Check environment only
./scripts/test-music-controls.sh --env

# Test browser focus
./scripts/test-music-controls.sh --focus

# Test control script
./scripts/test-music-controls.sh --script

# Test keyboard shortcuts
./scripts/test-music-controls.sh --keys

# Interactive test mode
./scripts/test-music-controls.sh --interactive

# Show current configuration
./scripts/test-music-controls.sh --config

# Show help
./scripts/test-music-controls.sh --help
```

### Interactive Menu

Run without arguments for an interactive menu:
```bash
./scripts/test-music-controls.sh
```

## What Gets Tested

1. **Wayland Environment** - Checks if running in Wayland
2. **Hyprland Compositor** - Verifies Hyprland is running and accessible
3. **Control Tools** - Checks for ydotool, xdotool, playerctl
4. **Control Script** - Verifies script exists and is executable
5. **Browser Detection** - Finds browser window via hyprctl/xdotool
6. **Browser Focus** - Tests focusing browser window
7. **Control Script Execution** - Tests all actions (toggle, next, prev, stop)
8. **Keyboard Shortcuts** - Tests keyboard event sending
9. **Media Session API** - Provides info about Media Session API support
10. **Hyprland Keybinds** - Checks if keybinds are configured

## Troubleshooting

### Browser Not Found

If browser window isn't detected:
1. Make sure a browser is open
2. Adjust `BROWSER_CLASS` variable to match your browser
3. Check with: `hyprctl clients | grep -i browser`

### Keyboard Shortcuts Don't Work

1. Install `ydotool` (preferred) or `xdotool`
2. For `ydotool`, you may need to run as root or configure udev rules
3. Check if browser window is focused when sending keys

### Control Script Fails

1. Verify script is executable: `chmod +x scripts/hyprland-music-control.sh`
2. Check script path in configuration
3. Run script manually to see error messages

### Media Keys Don't Work

1. Make sure browser tab with Posterchanai is active
2. Check browser console for "Media Session API handlers registered"
3. Verify music is actually playing
4. Try system media keys when browser is focused

## Example Test Session

```bash
# 1. Check environment
./scripts/test-music-controls.sh --env

# 2. If browser is open, test focus
./scripts/test-music-controls.sh --focus

# 3. Test control script
./scripts/test-music-controls.sh --script

# 4. Interactive test (requires browser with music playing)
./scripts/test-music-controls.sh --interactive

# 5. Full test suite
./scripts/test-music-controls.sh --all
```

## Tweaking for Your Setup

### Different Browser

The scripts are **pre-configured for Brave browser**. If you use a different browser, adjust `BROWSER_CLASS`:

```bash
# Brave (default - already configured)
BROWSER_CLASS="brave|Brave|brave-browser|Brave-browser"

# Firefox
BROWSER_CLASS="firefox|Firefox"

# Chromium
BROWSER_CLASS="chromium|Chromium"

# Chrome
BROWSER_CLASS="google-chrome|Google-chrome"
```

**Note:** To find your browser's exact class name:
```bash
hyprctl clients | grep -i brave  # For Brave
hyprctl clients | grep -i firefox  # For Firefox
# etc.
```

### Different Test Delay

Adjust `TEST_DELAY` if tests run too fast or slow:

```bash
TEST_DELAY=1   # Faster (1 second)
TEST_DELAY=3   # Slower (3 seconds)
```

### Custom Control Script

Point to a different control script:

```bash
CONTROL_SCRIPT="/path/to/your/custom-control.sh"
```

## Integration with CI/CD

The script returns exit codes:
- `0` - All tests passed
- `1` - Some tests failed

Use in CI/CD:
```bash
if ./scripts/test-music-controls.sh --all; then
    echo "All music control tests passed"
else
    echo "Some tests failed"
    exit 1
fi
```

## Adding Custom Tests

To add your own tests, add functions to the script:

```bash
test_custom_feature() {
    print_test "Custom Feature"
    # Your test code here
    if [ condition ]; then
        print_success "Custom feature works"
    else
        print_failure "Custom feature failed"
    fi
}
```

Then call it in the appropriate test section.
