#!/bin/bash
# ptop -- PosterChan top, the cyberpunk system monitor (CPU cores, memory, per-interface network, iostat-style
# disk latency, processes). ONE source for every install: the PosterChanOS overlay helper. On a server it is
# SYMLINKED, not copied, so every deploy's `git pull` updates it with nothing else to run.

install_ptop() {
    local src="$SCRIPT_DIR/os/overlay/app-misc/posterchanos-shell/files/ptop"
    local dst="/usr/local/bin/ptop"
    if [ ! -f "$src" ]; then
        print_warning "ptop is not in this checkout ($src) -- skipped"
        return 0
    fi
    chmod 0755 "$src" 2>/dev/null || true
    local SUDO=""
    [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    if $SUDO ln -sfn "$src" "$dst"; then
        print_success "ptop installed ($dst -> $src)"
    else
        print_warning "could not link $dst -- run: sudo ln -sfn $src $dst"
    fi
}
