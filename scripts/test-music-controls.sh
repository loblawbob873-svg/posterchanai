#!/bin/bash
# Music Player Controls Testing Script
# Test and tweak music player controls for Posterchanai

# Don't exit on error - we want to continue testing even if some tests fail
# set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration (tweak these)
BROWSER_CLASS="brave|Brave|brave-browser|Brave-browser"  # Brave browser patterns
POSTERCHANAI_URL="${POSTERCHANAI_URL:-http://localhost:8000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_SCRIPT="${SCRIPT_DIR}/hyprland-music-control.sh"
TEST_DELAY=2  # Delay between tests (seconds)

# Test results
TESTS_PASSED=0
TESTS_FAILED=0
TESTS_TOTAL=0

# Functions
print_header() {
    echo -e "\n${BLUE}=== $1 ===${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
    ((TESTS_PASSED++))
    ((TESTS_TOTAL++))
}

print_failure() {
    echo -e "${RED}✗ $1${NC}"
    ((TESTS_FAILED++))
    ((TESTS_TOTAL++))
}

print_info() {
    echo -e "${YELLOW}ℹ $1${NC}"
}

print_test() {
    echo -e "\n${BLUE}Testing: $1${NC}"
}

# Check if running in Wayland
check_wayland() {
    print_test "Wayland Environment"
    if [ -n "$WAYLAND_DISPLAY" ] || [ -n "$XDG_SESSION_TYPE" ] && [ "$XDG_SESSION_TYPE" = "wayland" ]; then
        print_success "Running in Wayland"
        return 0
    else
        print_failure "Not running in Wayland (or WAYLAND_DISPLAY not set)"
        return 1
    fi
}

# Check if Hyprland is running
check_hyprland() {
    print_test "Hyprland Compositor"
    if command -v hyprctl &> /dev/null; then
        if hyprctl version &> /dev/null; then
            print_success "Hyprland is running"
            HYPRLAND_VERSION=$(hyprctl version | head -1)
            print_info "Version: $HYPRLAND_VERSION"
            return 0
        else
            print_failure "hyprctl found but can't connect to Hyprland"
            return 1
        fi
    else
        print_failure "hyprctl not found (Hyprland not installed?)"
        return 1
    fi
}

# Check for control tools
check_tools() {
    print_test "Control Tools Availability"
    
    if command -v ydotool &> /dev/null; then
        print_success "ydotool is installed"
        YDOTOOL_AVAILABLE=1
    else
        print_info "ydotool not installed (optional, for global control)"
        YDOTOOL_AVAILABLE=0
    fi
    
    if command -v xdotool &> /dev/null; then
        print_success "xdotool is installed"
        XDOTOOL_AVAILABLE=1
    else
        print_info "xdotool not installed (optional, for X11 compatibility)"
        XDOTOOL_AVAILABLE=0
    fi
    
    if command -v playerctl &> /dev/null; then
        print_success "playerctl is installed"
        PLAYERCTL_AVAILABLE=1
    else
        print_info "playerctl not installed (optional, for MPRIS control)"
        PLAYERCTL_AVAILABLE=0
    fi
}

# Check control script
check_control_script() {
    print_test "Control Script"
    if [ -f "$CONTROL_SCRIPT" ]; then
        print_success "Control script exists: $CONTROL_SCRIPT"
        if [ -x "$CONTROL_SCRIPT" ]; then
            print_success "Control script is executable"
        else
            print_failure "Control script is not executable (run: chmod +x $CONTROL_SCRIPT)"
            return 1
        fi
    else
        print_failure "Control script not found: $CONTROL_SCRIPT"
        return 1
    fi
}

# Find browser window
find_browser() {
    print_test "Browser Window Detection (Brave)"
    
    if command -v hyprctl &> /dev/null; then
        # Try multiple Brave patterns
        for pattern in "brave" "Brave" "brave-browser" "Brave-browser"; do
            BROWSER_WINDOW=$(hyprctl clients | grep -i "class:.*$pattern" | head -1)
            if [ -n "$BROWSER_WINDOW" ]; then
                print_success "Brave browser window found via hyprctl (pattern: $pattern)"
                print_info "Window info: $(echo "$BROWSER_WINDOW" | head -1)"
                BROWSER_CLASS="$pattern"
                return 0
            fi
        done
    fi
    
    if [ "$XDOTOOL_AVAILABLE" = "1" ]; then
        # Try multiple Brave patterns with xdotool
        for pattern in "brave" "Brave" "brave-browser" "Brave-browser"; do
            BROWSER_WINDOW_ID=$(xdotool search --class "$pattern" 2>/dev/null | head -1)
            if [ -n "$BROWSER_WINDOW_ID" ]; then
                print_success "Brave browser window found via xdotool (ID: $BROWSER_WINDOW_ID, pattern: $pattern)"
                BROWSER_CLASS="$pattern"
                return 0
            fi
        done
    fi
    
    print_failure "Brave browser window not found"
    print_info "Make sure Brave is open with Posterchanai"
    print_info "Try: hyprctl clients | grep -i brave"
    return 1
}

# Test browser focus
test_browser_focus() {
    print_test "Browser Window Focus (Brave)"
    
    if command -v hyprctl &> /dev/null; then
        # Try focusing with different Brave patterns
        FOCUSED=0
        for pattern in "brave" "Brave" "brave-browser" "Brave-browser"; do
            hyprctl dispatch focuswindow "class:.*$pattern" &> /dev/null
            sleep 0.5
            
            # Check if browser is focused
            ACTIVE=$(hyprctl activewindow | grep -i "class:.*$pattern" || echo "")
            if [ -n "$ACTIVE" ]; then
                print_success "Brave browser window focused successfully (pattern: $pattern)"
                BROWSER_CLASS="$pattern"
                FOCUSED=1
                break
            fi
        done
        
        if [ $FOCUSED -eq 0 ]; then
            print_failure "Brave browser window focus failed"
            print_info "Try manually: hyprctl dispatch focuswindow 'class:.*brave'"
            return 1
        fi
        return 0
    else
        print_info "Skipping focus test (hyprctl not available)"
        return 0
    fi
}

# Test control script
test_control_script() {
    print_test "Control Script Execution"
    
    if [ ! -f "$CONTROL_SCRIPT" ]; then
        print_failure "Control script not found"
        return 1
    fi
    
    # Test each command
    for action in toggle next prev stop; do
        print_info "Testing action: $action"
        if "$CONTROL_SCRIPT" "$action" &> /dev/null; then
            print_success "Action '$action' executed"
        else
            print_failure "Action '$action' failed"
        fi
        sleep 0.5
    done
}

# Test keyboard shortcuts (simulated)
test_keyboard_shortcuts() {
    print_test "Keyboard Shortcuts (Simulated)"
    
    print_info "Expected shortcuts in browser:"
    echo "  - Space or Alt+P: Play/Pause"
            echo "  - Alt+F: Skip forward (next track)"
            echo "  - Alt+R: Skip back (previous track)"
    echo "  - Alt+S: Stop"
    
    if [ "$YDOTOOL_AVAILABLE" = "1" ]; then
        print_info "Testing with ydotool..."
        # Test Space key
        if ydotool key 57:1 57:0 &> /dev/null; then
            print_success "ydotool can send keys"
        else
            print_failure "ydotool key sending failed (may need root or udev rules)"
        fi
    elif [ "$XDOTOOL_AVAILABLE" = "1" ]; then
        print_info "Testing with xdotool..."
        BROWSER_WINDOW_ID=$(xdotool search --class "$BROWSER_CLASS" 2>/dev/null | head -1)
        if [ -n "$BROWSER_WINDOW_ID" ]; then
            if xdotool key --window "$BROWSER_WINDOW_ID" space &> /dev/null; then
                print_success "xdotool can send keys to browser"
            else
                print_failure "xdotool key sending failed"
            fi
        else
            print_info "Browser window not found for xdotool test"
        fi
    else
        print_info "No keyboard tools available (ydotool/xdotool)"
    fi
}

# Test Media Session API (requires browser inspection)
test_media_session() {
    print_test "Media Session API Support"
    
    print_info "Media Session API is implemented in music-player.js"
    print_info "To test:"
    echo "  1. Open Posterchanai in browser"
    echo "  2. Start playing music"
    echo "  3. Check browser console for 'Media Session API handlers registered'"
    echo "  4. Try system media keys (should work if tab is active)"
    
    # Check if we can detect media session in browser
    if command -v playerctl &> /dev/null; then
        PLAYERS=$(playerctl -l 2>/dev/null || echo "")
        if [ -n "$PLAYERS" ]; then
            print_success "MPRIS players detected: $PLAYERS"
            print_info "Browser may support MPRIS (check with: playerctl -l)"
        else
            print_info "No MPRIS players detected (browser may not support MPRIS)"
        fi
    fi
}

# Test Hyprland keybinds
test_hyprland_keybinds() {
    print_test "Hyprland Keybind Configuration"
    
    if command -v hyprctl &> /dev/null; then
        KEYBINDS=$(hyprctl binds | grep -i "music\|audio\|player" || echo "")
        if [ -n "$KEYBINDS" ]; then
            print_success "Found music-related keybinds in Hyprland"
            echo "$KEYBINDS"
        else
            print_info "No music-related keybinds found in Hyprland config"
            print_info "Add keybinds to ~/.config/hypr/hyprland.conf:"
            echo "  bind = SUPER, P, exec, $CONTROL_SCRIPT toggle"
            echo "  bind = SUPER, N, exec, $CONTROL_SCRIPT next"
            echo "  bind = SUPER, B, exec, $CONTROL_SCRIPT prev"
        fi
    fi
}

# Interactive test mode
interactive_test() {
    print_header "Interactive Test Mode"
    
    print_info "This will test controls interactively"
    print_info "Make sure:"
    echo "  1. Browser is open with Posterchanai"
    echo "  2. Music player is visible (or music is playing)"
    echo "  3. Browser tab is active"
    
    read -p "Press Enter to start interactive tests..."
    
    # Test each control
    for action in toggle next prev stop; do
        echo -e "\n${YELLOW}Testing: $action${NC}"
        echo "Watch the browser - the music player should respond"
        read -p "Press Enter to execute $action..."
        "$CONTROL_SCRIPT" "$action"
        sleep "$TEST_DELAY"
    done
    
    print_success "Interactive tests completed"
}

# Generate test report
generate_report() {
    print_header "Test Report"
    
    echo "Total tests: $TESTS_TOTAL"
    echo -e "${GREEN}Passed: $TESTS_PASSED${NC}"
    echo -e "${RED}Failed: $TESTS_FAILED${NC}"
    
    if [ $TESTS_FAILED -eq 0 ]; then
        echo -e "\n${GREEN}All tests passed!${NC}"
        return 0
    else
        echo -e "\n${YELLOW}Some tests failed. Check output above.${NC}"
        return 1
    fi
}

# Main menu
show_menu() {
    echo -e "\n${BLUE}Music Player Controls Test Suite${NC}"
    echo "=================================="
    echo "1. Run all tests"
    echo "2. Check environment"
    echo "3. Test browser focus"
    echo "4. Test control script"
    echo "5. Test keyboard shortcuts"
    echo "6. Interactive test mode"
    echo "7. Show configuration"
    echo "8. Exit"
    echo ""
    read -p "Select option [1-8]: " choice
    
    case $choice in
        1)
            check_wayland
            check_hyprland
            check_tools
            check_control_script
            find_browser
            test_browser_focus
            test_control_script
            test_keyboard_shortcuts
            test_media_session
            test_hyprland_keybinds
            generate_report
            ;;
        2)
            check_wayland
            check_hyprland
            check_tools
            check_control_script
            ;;
        3)
            find_browser
            test_browser_focus
            ;;
        4)
            check_control_script
            test_control_script
            ;;
        5)
            check_tools
            test_keyboard_shortcuts
            ;;
        6)
            interactive_test
            ;;
        7)
            echo -e "\n${BLUE}Configuration:${NC}"
            echo "Browser class: $BROWSER_CLASS (configured for Brave)"
            echo "Posterchanai URL: $POSTERCHANAI_URL"
            echo "Control script: $CONTROL_SCRIPT"
            echo "Test delay: $TEST_DELAY seconds"
            echo ""
            echo "To check your Brave window class:"
            echo "  hyprctl clients | grep -i brave"
            echo ""
            echo "To tweak, edit variables at the top of this script"
            ;;
        8)
            exit 0
            ;;
        *)
            echo "Invalid option"
            ;;
    esac
}

# Command line arguments
if [ $# -gt 0 ]; then
    case "$1" in
        --all|-a)
            check_wayland
            check_hyprland
            check_tools
            check_control_script
            find_browser
            test_browser_focus
            test_control_script
            test_keyboard_shortcuts
            test_media_session
            test_hyprland_keybinds
            generate_report
            ;;
        --env|-e)
            check_wayland
            check_hyprland
            check_tools
            ;;
        --focus|-f)
            find_browser
            test_browser_focus
            ;;
        --script|-s)
            check_control_script
            test_control_script
            ;;
        --keys|-k)
            check_tools
            test_keyboard_shortcuts
            ;;
        --interactive|-i)
            interactive_test
            ;;
        --config|-c)
            echo "Browser class: $BROWSER_CLASS (configured for Brave)"
            echo "Posterchanai URL: $POSTERCHANAI_URL"
            echo "Control script: $CONTROL_SCRIPT"
            echo "Test delay: $TEST_DELAY seconds"
            echo ""
            echo "To check your Brave window class:"
            echo "  hyprctl clients | grep -i brave"
            ;;
        --help|-h)
            echo "Music Player Controls Test Script"
            echo ""
            echo "Usage: $0 [OPTION]"
            echo ""
            echo "Options:"
            echo "  --all, -a          Run all tests"
            echo "  --env, -e          Check environment"
            echo "  --focus, -f        Test browser focus"
            echo "  --script, -s       Test control script"
            echo "  --keys, -k         Test keyboard shortcuts"
            echo "  --interactive, -i  Interactive test mode"
            echo "  --config, -c       Show configuration"
            echo "  --help, -h         Show this help"
            echo ""
            echo "Without options, shows interactive menu"
            echo ""
            echo "Tweak configuration by editing variables at the top of the script:"
            echo "  - BROWSER_CLASS: Browser window class pattern"
            echo "  - POSTERCHANAI_URL: Posterchanai web interface URL"
            echo "  - CONTROL_SCRIPT: Path to control script"
            echo "  - TEST_DELAY: Delay between tests (seconds)"
            ;;
        *)
            echo "Unknown option: $1"
            echo "Run '$0 --help' for usage"
            exit 1
            ;;
    esac
else
    # Interactive mode
    while true; do
        show_menu
        echo ""
        read -p "Press Enter to continue..."
    done
fi
