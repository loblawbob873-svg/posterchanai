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

    # Step 10: Setup XPU image instance for Intel Arc
    setup_xpu_image_instance

    # Step 11: Setup systemd service
    setup_systemd

    # Step 12: Install CLI control tool
    setup_cli_tool

    # Step 13: Show MCP server info
    setup_mcp_server

    # Step 13: Offer model download (if local LLM)
    if [ "$INSTALL_LLM" = "1" ] && [ "$LLM_BACKEND" != "ollama" ]; then
        download_model
    fi

    # Step 15: Configure database
    configure_database_settings

    # Step 16: Print summary
    print_summary
}

# Run main installation
main "$@"
