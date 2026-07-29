#!/bin/bash
# Build the built-in Pion TURN/STUN relay for voice/video calls (add-on: ./install.sh --turn).
# The app supervises the resulting binary as a subprocess (app/services/turn_service.py) — no systemd unit.

setup_turn_server() {
    print_banner 2>/dev/null || true
    echo -e "${BOLD:-}📞 Building the built-in TURN relay (turnserver/pion-turn)${NC:-}"
    echo ""

    local repo_root turn_dir
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    turn_dir="$repo_root/turnserver"

    if [ ! -f "$turn_dir/main.go" ]; then
        print_error "turnserver/main.go not found — is this the PosterChanAI repo?" 2>/dev/null || echo "ERROR: turnserver/main.go not found"
        return 1
    fi

    if ! command -v go >/dev/null 2>&1; then
        print_error "Go toolchain not found." 2>/dev/null || echo "ERROR: Go toolchain not found."
        echo "Install Go, then re-run:  ./install.sh --turn"
        echo "  Debian/Ubuntu:  sudo apt-get install -y golang-go"
        echo "  Fedora:         sudo dnf install -y golang"
        echo "  Arch:           sudo pacman -S go"
        echo "  Gentoo:         sudo emerge dev-lang/go"
        return 1
    fi

    ( cd "$turn_dir" && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o pion-turn . ) || {
        print_error "go build failed" 2>/dev/null || echo "ERROR: go build failed"; return 1;
    }
    print_success "Built $turn_dir/pion-turn" 2>/dev/null || echo "OK: built pion-turn"

    cat <<'EOF'

Next steps to turn it on (Admin → Live → "Voice/Video Calls + Built-in TURN Relay"):
  1. DNS: add a GREY-CLOUDED (DNS-only, NOT proxied) A record  turn.<yourdomain>  → this server's PUBLIC IP.
  2. Firewall/router: forward these to this machine —
       - UDP + TCP 3478            (STUN + TURN)
       - the relay UDP range       (default 49160-49200; widen for many concurrent relays)
       - optionally TCP 443        (TURN-over-TLS, for restrictive/mobile networks — set the cert/key too)
  3. In Admin: set "Public IP" (required) + "TURN domain", tick "Run the built-in TURN relay", Save.
     The app starts + supervises the relay automatically (no restart needed). The shared secret auto-generates.

Calls work P2P for most users WITHOUT the relay; the relay is only the NAT fallback.
EOF
}
