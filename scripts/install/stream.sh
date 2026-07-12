#!/bin/bash
# Install the built-in MediaMTX media server for OBS streaming (add-on: ./install.sh --stream).
# MediaMTX is a single prebuilt Go binary (no build step); the app supervises it as a subprocess
# (app/services/stream_service.py) — no systemd unit. Absent binary → the streaming feature is a no-op.

setup_stream_server() {
    print_banner 2>/dev/null || true
    echo -e "${BOLD:-}🔴 Installing the built-in media server (MediaMTX) for OBS streaming${NC:-}"
    echo ""

    local repo_root stream_dir
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    stream_dir="$repo_root/streamserver"
    mkdir -p "$stream_dir"

    # Resolve the release version (latest by default; override with MEDIAMTX_VERSION; pinned fallback).
    local ver
    ver="${MEDIAMTX_VERSION:-$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest 2>/dev/null \
        | grep -oE '"tag_name" *: *"[^"]+"' | head -1 | cut -d'"' -f4)}"
    ver="${ver:-v1.11.3}"

    # Map uname → MediaMTX asset arch.
    local os arch m
    os="linux"; m="$(uname -m)"
    case "$m" in
        x86_64|amd64) arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        armv7l|armv7)  arch="armv7" ;;
        *) print_error "unsupported arch: $m" 2>/dev/null || echo "ERROR: unsupported arch: $m"; return 1 ;;
    esac
    [ "$(uname -s)" = "Darwin" ] && os="darwin"

    local tarball url tmp
    tarball="mediamtx_${ver}_${os}_${arch}.tar.gz"
    url="https://github.com/bluenviron/mediamtx/releases/download/${ver}/${tarball}"
    tmp="$(mktemp -d)"
    echo "Downloading $url"
    if ! curl -fsSL "$url" -o "$tmp/$tarball"; then
        print_error "download failed ($url)" 2>/dev/null || echo "ERROR: download failed"
        echo "Set MEDIAMTX_VERSION to a valid tag from https://github.com/bluenviron/mediamtx/releases and retry."
        rm -rf "$tmp"; return 1
    fi
    tar -xzf "$tmp/$tarball" -C "$tmp" mediamtx 2>/dev/null || tar -xzf "$tmp/$tarball" -C "$tmp"
    if [ ! -f "$tmp/mediamtx" ]; then
        print_error "mediamtx binary not found in archive" 2>/dev/null || echo "ERROR: binary missing"
        rm -rf "$tmp"; return 1
    fi
    install -m 0755 "$tmp/mediamtx" "$stream_dir/mediamtx"
    rm -rf "$tmp"
    print_success "Installed $stream_dir/mediamtx ($ver)" 2>/dev/null || echo "OK: installed mediamtx $ver"

    cat <<'EOF'

Next steps to turn it on (Admin → Services → "OBS Streaming"):
  1. Firewall/router: forward TCP 1935 (RTMP ingest from OBS) to this machine. For many viewers, also
     forward the HLS port (default 8888) and set "HLS base URL" to a grey-clouded stream.<domain>; otherwise
     the app reverse-proxies HLS over your existing tunnel (fine for a handful of viewers).
  2. In Admin: tick "Run the built-in media server", set "Stream domain" (host OBS pushes to), Save.
     The app starts + supervises MediaMTX automatically (no restart needed).
  3. In the web client → Discover → Streams → "Go Live": copy the OBS Server + Stream key, start OBS,
     then tap Go Live to announce it on Nostr.
EOF
}
