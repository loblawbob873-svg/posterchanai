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
source "$INSTALL_DIR/postgres.sh"
source "$INSTALL_DIR/telegram_botapi.sh"
source "$INSTALL_DIR/update.sh"
source "$INSTALL_DIR/music.sh"
source "$INSTALL_DIR/video.sh"
source "$INSTALL_DIR/voice.sh"
source "$INSTALL_DIR/turn.sh"
source "$INSTALL_DIR/stream.sh"
source "$INSTALL_DIR/sandbox.sh"
source "$INSTALL_DIR/searxng.sh"
source "$INSTALL_DIR/webxdc.sh"

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

# Add-on: install just the voice-cloning deps into the existing torch venv.
if [ "$1" = "--voice" ]; then
    setup_voice_deps
    exit $?
fi

# Add-on: run this node's own SearXNG (private metasearch) — what the AI's web search, the news
# digests, the bots and the Web Search screen all query. Needs docker/podman.
if [ "$1" = "--searxng" ]; then
    setup_searxng
    exit $?
fi

# Add-on: build the built-in Pion TURN relay for voice/video calls (needs the Go toolchain).
if [ "$1" = "--turn" ]; then
    setup_turn_server
    exit $?
fi

# Add-on: install the built-in MediaMTX media server for OBS streaming (downloads a prebuilt binary).
if [ "$1" = "--stream" ]; then
    setup_stream_server
    exit $?
fi

# Add-on: the webxdc mini-app sandbox ORIGIN — xdc.<your-domain>: a DNS record, its own certificate
# and an nginx vhost. The app already serves both paths that origin needs; without the hostname a
# mini app posts fine and then shows a blank window forever, with nothing in any log.
if [ "$1" = "--webxdc" ]; then
    setup_webxdc_sandbox
    exit $?
fi

# Add-on: set up the per-user Debian Docker sandbox (docker group + base image) for agentic node tasks.
if [ "$1" = "--sandbox" ]; then
    setup_sandbox
    exit $?
fi

# Add-on: verify the built-in GRASP git host deps. No packages to install — it uses stdlib http.server
# + psycopg2 (already required) + `git`/`git-http-backend` (ship together with the git package). This
# just confirms git-http-backend is present at the expected path.
# Prerequisite check for the built-in git server. Also run (non-fatally) by the main install so a
# fresh box learns whether the git host will work BEFORE someone flips it on in Admin.
check_git_host() {
    echo "Checking GRASP git host prerequisites..."
    if command -v git >/dev/null 2>&1; then echo "  git: $(git --version)"; else
        echo "  MISSING: git — install it (Debian/Ubuntu: apt-get install git)"; return 1; fi
    GITBK=""
    for p in /usr/libexec/git-core/git-http-backend /usr/lib/git-core/git-http-backend; do
        if [ -x "$p" ]; then echo "  git-http-backend: $p"; GITBK="$p"; fi
    done
    if [ -z "$GITBK" ]; then
        echo "  MISSING: git-http-backend (ships with git; check /usr/libexec/git-core or /usr/lib/git-core)"; return 1; fi
    echo "OK — enable the host in Admin → Git (git_server_enabled) and set its public base URL."
    echo "     Repos will live under <Storage Path>/git_repos. Guide: docs/GIT.md"
    return 0
}

if [ "$1" = "--git-host" ]; then
    check_git_host
    exit $?
fi

# =============================================================================
# Install mode: Full (AI + Nostr) vs Nostr-only (relay + Nostr web client, NO AI)
# =============================================================================
select_install_mode() {
    echo ""
    echo -e "${BOLD}What do you want to install?${NC}"
    echo "  1) Full        - AI assistant + image/music/video + the Nostr relay & client (needs a GPU for the good stuff)"
    echo "  2) Nostr-only  - just the self-hosted Nostr relay + web client + Blossom (NO AI). Light, no GPU needed."
    echo ""
    local choice
    read -r -p "Choose [1]: " choice
    if [ "$choice" = "2" ]; then
        NOSTR_ONLY=1
        print_success "Nostr-only mode: no AI stack will be installed."
    else
        NOSTR_ONLY=0
    fi
}

# Lean install path for Nostr-only: relay + client + Blossom + the non-AI features. Skips LLM/
# image/music/video entirely; installs requirements-nostr.txt; turns the relay + nostr-only UI on.
install_nostr_only() {
    BACKEND="cpu"            # generic run script (sources data/secrets.env), no GPU stack
    NOSTR_ONLY=1

    setup_directories
    setup_postgres           # the one and only database (app + built-in Nostr relay)
    setup_python_env         # honours NOSTR_ONLY -> installs requirements-nostr.txt

    # Provision the relay's instance (operator) key now, seed the WoT with it, and print the npub.
    venv/bin/python scripts/init_instance_key.py || print_warning "instance key init deferred to first run"

    # Turnkey runtime flags (the run script sources data/secrets.env): enable the relay and the
    # nostr-only UI. DATABASE_URL defaults to the local-trust Postgres set up above.
    mkdir -p data
    touch data/secrets.env
    grep -q '^export POSTERCHANAI_NOSTR_RELAY=' data/secrets.env || echo 'export POSTERCHANAI_NOSTR_RELAY=1' >> data/secrets.env
    grep -q '^export POSTERCHANAI_NOSTR_ONLY='  data/secrets.env || echo 'export POSTERCHANAI_NOSTR_ONLY=1'  >> data/secrets.env
    print_success "Wrote data/secrets.env (relay on, AI hidden)"

    setup_systemd            # generic service + run-cpu.sh (sources data/secrets.env)

    echo ""
    print_success "Nostr-only install complete."
    echo -e "  • Web client + relay: ${BOLD}http://localhost:${POSTERCHANAI_PORT:-3051}/client${NC}  (relay ws on :3052)"
    echo -e "  • Front it with TLS (nginx) for production — see ${BOLD}docs/NGINX.md${NC} and nginx/posterchanai.conf.example"
    echo -e "  • Add AI later by re-running ./install.sh and choosing Full."
}

# Best-effort: make sure the system clock is correct. The Nostr relay's queries are time-windowed
# (backfill `since = now - 48h`, created_at sanity), so a wrong clock silently breaks it — the WoT
# still builds but the timeline stays EMPTY (the post window is in the future). Enable NTP via
# whatever the host has; never fail the install over it.
ensure_system_clock() {
    if command -v timedatectl >/dev/null 2>&1; then
        if timedatectl set-ntp true >/dev/null 2>&1 || sudo timedatectl set-ntp true >/dev/null 2>&1; then
            print_success "System clock: NTP sync enabled"
            return
        fi
    fi
    for svc in systemd-timesyncd chronyd ntpd ntp; do
        if sudo systemctl enable --now "$svc" >/dev/null 2>&1; then
            print_success "System clock: enabled $svc for NTP sync"
            return
        fi
    done
    print_warning "Could not auto-enable NTP — make sure the system clock is correct (a wrong clock makes the Nostr relay show NO posts)"
}

# =============================================================================
# Main Installation Flow
# =============================================================================

main() {
    print_banner

    # Step 1: Check system dependencies
    check_dependencies

    # Step 1b: ensure the system clock is correct (a wrong clock makes the relay show no posts)
    ensure_system_clock

    # Step 2: Detect GPU
    detect_gpu

    # Step 2b: Full (AI) vs Nostr-only. Nostr-only short-circuits the whole AI pipeline.
    select_install_mode
    if [ "${NOSTR_ONLY:-0}" = "1" ]; then
        install_nostr_only
        return
    fi

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

    # Option 6 (update): safely refresh deps + Telegram server, then stop. Ensure Postgres exists
    # too (existing SQLite installs upgrading to the Postgres-only datastore).
    if [ "${UPDATE_ONLY:-0}" = "1" ]; then
        setup_postgres
        run_updates
        return
    fi

    # Step 4: Select LLM backend
    select_llm_backend

    # Step 5: Select image backend
    select_image_backend

    # Step 6: Setup directories
    setup_directories

    # Step 6b: PostgreSQL — the one and only database (app + built-in Nostr relay).
    setup_postgres

    # Step 7: Setup Python environment
    setup_python_env

    # Step 7b: Provision the relay's instance (operator) key + seed the WoT; prints the instance npub.
    venv/bin/python scripts/init_instance_key.py || print_warning "instance key init deferred to first run"

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

    # Step 9d: Install the built-in MediaMTX media server for OBS streaming (prebuilt binary, ~30MB).
    # Shipped by DEFAULT so streaming is a single Admin toggle (no separate install step). Non-fatal —
    # a download hiccup just means streaming stays off until retried with ./install.sh --stream.
    setup_stream_server || print_warning "MediaMTX (streaming) download did not complete; enable later with ./install.sh --stream"

    # Step 9d2: This node's own SearXNG — the metasearch behind the AI's web-search tool, the news
    # digests, the bots and the Web Search screen. Shipped by DEFAULT (like MediaMTX and TURN) so a
    # fresh node searches for itself instead of falling back to a public instance that rate-limits
    # servers. Runs NATIVELY in the app's venv now (no docker) — non-fatal either way: a failure here
    # just means Admin → Tools decides where this node searches.
    setup_searxng || print_warning "SearXNG not installed; set Admin → Tools → SearXNG URL, or run ./install.sh --searxng later"

    # Step 9e: Build the built-in Pion TURN relay (voice/video-call NAT traversal). Shipped by DEFAULT so
    # calls are turnkey (no separate ./install.sh --turn step). Non-fatal — a missing Go toolchain just leaves
    # calls on STUN-only until built later with ./install.sh --turn.
    setup_turn_server || print_warning "TURN relay build skipped (Go toolchain missing?); build later with ./install.sh --turn"

    # Step 9f: Verify the built-in git server's prerequisites (git + git-http-backend). Nothing to
    # install — it's stdlib + the git package — so this only reports, and the host still ships OFF
    # (Admin → Git). Non-fatal: a box without git simply can't host repos.
    check_git_host || print_warning "Git host prerequisites missing; docs/GIT.md explains what to install"

    # Step 9g: the webxdc mini-app sandbox ORIGIN (xdc.<domain>). NOT run by default like MediaMTX or
    # SearXNG, because it is the only optional feature that needs something outside this machine — a
    # DNS record — before it can do anything. Offered instead, and only where there is an nginx to
    # add a vhost to; declining costs nothing, since every other part of mini apps already ships.
    # Non-fatal in every branch: a node with no hostname yet still finishes its install.
    if command -v nginx >/dev/null 2>&1; then
        echo ""
        echo "Mini apps (.xdc games, polls, shared editors) run on a SEPARATE hostname — xdc.<your-domain>"
        echo "— because an app is untrusted code and must not share the origin holding your Nostr key."
        echo "Needs a DNS record + its own certificate. Skip it and everything else still works; mini"
        echo "apps just show a blank window. You can do it later with ./install.sh --webxdc"
        WEBXDC_ANS=""
        read -r -p "Set up the mini-app sandbox origin now? [y/N]: " WEBXDC_ANS || true
        case "$WEBXDC_ANS" in
            [Yy]*) setup_webxdc_sandbox || print_warning "Mini-app sandbox not configured; retry with ./install.sh --webxdc (docs/WEBXDC.md)";;
            *) echo "  Skipped. Later:  ./install.sh --webxdc   (docs/WEBXDC.md)";;
        esac
    else
        echo ""
        echo "Mini apps (.xdc) need a second hostname, xdc.<your-domain>, in front of this app."
        echo "  With nginx:   ./install.sh --webxdc"
        echo "  With Caddy / Traefik / another proxy: one rule — docs/WEBXDC.md"
    fi

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
