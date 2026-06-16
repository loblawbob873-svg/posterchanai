#!/bin/bash
# Posterchanai Installer
# Interactive setup for GPU acceleration and systemd service
# Supports modular installation: LLM, Image Generation, or Full Stack
#
# Modules are located in scripts/install/:
#   utils.sh    - Colors, print functions, banner
#   detect.sh   - GPU and distro detection
#   deps.sh     - Dependency checking and install instructions
#   backends.sh - Backend selection (LLM and image)
#   llama_cpp.sh - llama-cpp-python installation
#   image.sh    - Image generation dependencies
#   systemd.sh  - Systemd service setup
#   setup.sh    - Directories, Python env, database, model download

set -e

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Source all modules
INSTALL_DIR="$SCRIPT_DIR/scripts/install"

source "$INSTALL_DIR/utils.sh"
source "$INSTALL_DIR/detect.sh"
source "$INSTALL_DIR/deps.sh"
source "$INSTALL_DIR/backends.sh"
source "$INSTALL_DIR/llama_cpp.sh"
source "$INSTALL_DIR/image.sh"
source "$INSTALL_DIR/systemd.sh"
source "$INSTALL_DIR/setup.sh"
source "$INSTALL_DIR/telegram_botapi.sh"
source "$INSTALL_DIR/update.sh"
source "$INSTALL_DIR/music.sh"
source "$INSTALL_DIR/video.sh"

# Handle --help and --packages options
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    show_help
    exit 0
fi

if [ "$1" = "--packages" ]; then
    print_banner
    detect_distro
    echo -e "${BOLD}Required packages for your system:${NC}"
    echo ""
    show_install_instructions
    exit 0
fi

# Add-on: set up just the ACE-Step music server (venv-music) against an existing install.
if [ "$1" = "--music" ]; then
    setup_music_server
    exit $?
fi

# Add-on: install just the video generation (videogeni) deps into the existing image venv.
if [ "$1" = "--video" ]; then
    setup_video_deps
    exit $?
fi

# =============================================================================
# Main Installation Flow
# =============================================================================

main() {
    print_banner

    # Step 1: Check system dependencies
    check_dependencies

    # Step 2: Detect GPU
    detect_gpu

    # Step 3: Select what to install
    select_components

    # Option 5 (add-on only): set up just the local Telegram Bot API server
    # against an existing install, then stop — skip deps/models/GPU/systemd.
    if [ "${TELEGRAM_ONLY:-0}" = "1" ]; then
        setup_telegram_botapi
        echo ""
        print_success "Done. Local Telegram Bot API server add-on configured."
        return
    fi

    # Option 6 (update): safely refresh deps + Telegram server, then stop.
    if [ "${UPDATE_ONLY:-0}" = "1" ]; then
        run_updates
        return
    fi

    # Step 4: Select LLM backend
    select_llm_backend

    # Step 5: Select image backend
    select_image_backend

    # Step 6: Setup directories
    setup_directories

    # Step 7: Setup Python environment
    setup_python_env

    # Step 8: Install LLM dependencies (if selected and not Ollama)
    if [ "$INSTALL_LLM" = "1" ] && [ "$LLM_BACKEND" != "ollama" ]; then
        setup_llama_cpp
    fi

    # Step 9: Install image dependencies (if selected)
    setup_image_deps

    # Step 9a: Music generation (ACE-Step) add-on, if selected. Separate service via uv + systemd.
    if [ "$INSTALL_MUSIC" = "1" ]; then
        setup_music_server || print_warning "Music server setup did not complete; you can retry with ./install.sh --music"
    fi

    # Step 9a2: Video generation (videogeni) deps, if selected. Native diffusers — rides the image
    # venv (no separate service); just adds sentencepiece/ftfy + optional model prefetch.
    if [ "$INSTALL_VIDEO" = "1" ]; then
        setup_video_deps || print_warning "Video setup did not complete; you can retry with ./install.sh --video"
    fi

    # Step 9b: Fetch the depth model for the `alive` 3D-parallax effect (gitignored,
    # ~94MB). Skipped automatically if already present.
    download_depth_model

    # Step 9c: Fetch the u2net model for the `removebackground` command (~176MB).
    # Skipped automatically if already present.
    download_u2net_model

    # Step 10: Setup XPU image instance for Intel Arc
    setup_xpu_image_instance

    # Step 11: Setup systemd service
    setup_systemd

    # Step 13: Offer model download (if local LLM)
    if [ "$INSTALL_LLM" = "1" ] && [ "$LLM_BACKEND" != "ollama" ]; then
        download_model
    fi

    # Step 15: Configure database
    configure_database_settings

    # Step 16: Optional local Telegram Bot API server (large-file support).
    # Comes after the DB step because it writes settings into the database.
    setup_telegram_botapi

    # Step 17: Print summary
    print_summary
}

# Run main installation
main "$@"
