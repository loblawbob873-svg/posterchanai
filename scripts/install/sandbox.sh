#!/bin/bash
# Set up the per-user Docker sandbox (add-on: ./install.sh --sandbox).
#
# The sandbox lets NON-admin AI users (and opt-in admins) run agentic `node`/`agent` tasks confined to
# a throwaway per-user Debian container (app/services/sandbox_service.py, via `docker exec`) instead of
# the host. This needs three things on the host, which this add-on arranges:
#   1) the Docker engine + CLI installed and the daemon running,
#   2) the service user (who runs `python run.py`) in the `docker` group — so the app can reach
#      /var/run/docker.sock WITHOUT sudo (takes effect on the NEXT restart of the service),
#   3) the base image pulled (node_exec_sandbox_image, default python:3.12-slim — it ships an
#      interpreter so an agent run does not burn its first two steps apt-get installing python3).
#
# After this, enable it in Admin → Nodes → "Enable the per-user Debian sandbox" (off by default),
# then restart the service so the new docker-group membership is picked up.
#
# Docker itself is NOT auto-installed here (too distro-specific + a big host change): if it's missing we
# print the one-liner for your distro and stop. Everything else is idempotent.

setup_sandbox() {
    print_banner 2>/dev/null || true
    echo -e "${BOLD:-}🐳 Setting up the per-user Debian sandbox (Docker)${NC:-}"
    echo ""

    local svc_user image repo_root dockerfile
    svc_user="${SUDO_USER:-$(whoami)}"
    # Default is the locally-BUILT image (Dockerfile.sandbox). Override with SANDBOX_IMAGE=<registry image>
    # to skip the build and just pull a plain image instead.
    image="${SANDBOX_IMAGE:-posterchanai-sandbox:3}"
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    dockerfile="$repo_root/Dockerfile.sandbox"

    # 1) Docker present?
    if ! command -v docker >/dev/null 2>&1; then
        print_warning "Docker is not installed. Install it first, then re-run: ./install.sh --sandbox"
        echo "   • Debian/Ubuntu:  curl -fsSL https://get.docker.com | sh"
        echo "   • Fedora:         sudo dnf install -y docker-ce docker-ce-cli containerd.io"
        echo "   • Arch:           sudo pacman -S --noconfirm docker"
        echo "   • Gentoo:         sudo emerge -av app-containers/docker app-containers/docker-cli"
        return 1
    fi

    # 2) Daemon running (systemd hosts)
    if command -v systemctl >/dev/null 2>&1; then
        if ! systemctl is-active --quiet docker; then
            sudo systemctl enable --now docker 2>/dev/null \
                && print_success "Started + enabled the Docker daemon" \
                || print_warning "Could not start the Docker daemon — start it manually (sudo systemctl start docker)"
        else
            print_success "Docker daemon is running"
        fi
    fi

    # 3) Service user in the docker group (so the app reaches the socket without sudo)
    if getent group docker >/dev/null 2>&1; then
        if id -nG "$svc_user" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
            print_success "User '$svc_user' is already in the docker group"
        else
            sudo usermod -aG docker "$svc_user" \
                && print_success "Added '$svc_user' to the docker group (takes effect on the next service restart)" \
                || print_warning "Could not add '$svc_user' to the docker group — do it manually: sudo usermod -aG docker $svc_user"
        fi
    else
        print_warning "No 'docker' group found — is Docker fully installed?"
    fi

    # 4) Sandbox image. The default is BUILT from Dockerfile.sandbox (adds bech32 etc.); a custom
    # SANDBOX_IMAGE that is a registry reference is pulled instead. `docker` may need sudo until the
    # group membership from step 3 takes effect on the next login, so try both.
    _dock() { sudo docker "$@" 2>&1 || docker "$@" 2>&1; }
    if [ "$image" = "posterchanai-sandbox:3" ] && [ -f "$dockerfile" ]; then
        echo "   Building the sandbox image ($image) from Dockerfile.sandbox…"
        if _dock build -t "$image" -f "$dockerfile" "$repo_root" >/dev/null; then
            print_success "Built $image"
        else
            print_warning "Build failed now — the app self-builds it on first sandbox use, or run: docker build -t $image -f $dockerfile $repo_root"
        fi
    else
        echo "   Pulling the sandbox image ($image)…"
        if _dock pull "$image" >/dev/null; then
            print_success "Pulled $image"
        else
            print_warning "Could not pull $image now — it will be pulled lazily on first use if the daemon can reach a registry"
        fi
    fi

    echo ""
    print_success "Sandbox host setup done."
    echo "   Next: Admin → Nodes → enable \"Enable the per-user Debian sandbox\", then RESTART the"
    echo "   service (so the docker-group membership is active). It stays OFF until you enable it."
}
