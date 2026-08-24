#!/bin/bash
# Nextcloud-style built-in Collabora Online Development Edition (CODE).

setup_office_server() {
    print_banner 2>/dev/null || true
    local root arch url tmp app user unit
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    arch="$(uname -m)"; tmp="$(mktemp -d)"; app="$root/officeserver/Collabora_Online.AppImage"
    case "$arch" in
      x86_64|amd64)
        url="${CODE_APPIMAGE_URL:-https://www.collaboraoffice.com/downloads/Collabora-Office-AppImage-Release/collabora-online-release-LATEST.AppImage}" ;;
      aarch64|arm64)
        url="${CODE_APPIMAGE_URL:-https://github.com/CollaboraOnline/richdocumentscode/releases/latest/download/richdocumentscode_arm64.tar.gz}" ;;
      *) print_error "Built-in CODE supports Linux x86_64 and ARM64; found $arch"; rm -rf "$tmp"; return 1 ;;
    esac
    mkdir -p "$(dirname "$app")"
    echo "Downloading built-in CODE for $arch…"
    if ! curl -fL --retry 3 "$url" -o "$tmp/code.download"; then rm -rf "$tmp"; return 1; fi
    if [[ "$url" = *.tar.gz ]]; then
        tar -xzf "$tmp/code.download" -C "$tmp"
        local found; found="$(find "$tmp" -name 'Collabora_Online.AppImage' -type f -print -quit)"
        [ -n "$found" ] || { print_error "CODE archive did not contain Collabora_Online.AppImage"; rm -rf "$tmp"; return 1; }
        install -m 0755 "$found" "$app"
    else install -m 0755 "$tmp/code.download" "$app"; fi
    rm -rf "$tmp"
    user="$(id -un)"; unit="/etc/systemd/system/posterchanai-office.service"
    sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=PosterChan built-in CODE office editor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$user
WorkingDirectory=$root
Environment=HOME=$root/data/office-home
# TMPDIR: --appimage-extract-and-run unpacks the whole AppImage into it on EVERY start, and CODE
# keeps per-document working files there. server1's /tmp is a tmpfs on a box with no swap, so the
# default spends hundreds of MB of unreclaimable RAM per restart. data/office-work was already
# being created for this and was never wired up. See posterchanai-office.service.
Environment=TMPDIR=$root/data/office-work
ExecStart=$app --appimage-extract-and-run --port=9983 --o:ssl.enable=false --o:ssl.termination=true --o:net.proxy_prefix=true --o:security.capabilities=false --o:security.seccomp=false --o:welcome.enable=false
Restart=on-failure
RestartSec=5
# It unpacks itself before it serves anything; a short stop timeout kills it mid-extraction and
# leaves a half-written tree in TMPDIR for the next start to trip over.
TimeoutStopSec=60
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
    mkdir -p "$root/data/office-home" "$root/data/office-work"
    touch "$root/data/secrets.env"
    grep -q '^export POSTERCHANAI_OFFICE=' "$root/data/secrets.env" || echo 'export POSTERCHANAI_OFFICE=1' >> "$root/data/secrets.env"
    grep -q '^export POSTERCHANAI_CODE_URL=' "$root/data/secrets.env" || echo 'export POSTERCHANAI_CODE_URL=http://127.0.0.1:9983' >> "$root/data/secrets.env"
    sudo systemctl daemon-reload
    sudo systemctl enable --now posterchanai-office
    print_success "Built-in CODE installed on loopback port 9983"
    echo "Add the /office-code/ block from nginx/posterchanai.conf.example, then reload nginx."
}
