#!/bin/bash
# Installer option 6: update dependencies for posterchanai AND the local
# Telegram Bot API server — without breaking the fragile Intel Arc / IPEX pins.
#
# Intel Arc's venv-ipex has packages that MUST NOT be upgraded (torch 2.1.0a0
# Intel build, intel-extension-for-pytorch, ipex-llm, numpy<2, the transformers
# stack). We freeze those to their currently-installed versions via a pip
# constraints file, so `pip install --upgrade` can move everything else but can
# never touch them (a conflicting upgrade fails loudly instead of breaking Arc).

# Packages that are unsafe to upgrade in the unified Intel Arc venv (torch-XPU + diffusers).
# A generic `pip install -U` must NOT pull a CPU torch over the XPU build, nor bump transformers
# to >=5 (breaks diffusers SDXL). The EOL IPEX/bigdl/numpy<2 pins are gone (no longer installed).
_IPEX_FROZEN_RE='^(torch|torchvision|torchaudio|transformers|tokenizers|diffusers|accelerate|safetensors)=='

_update_one_venv() {
    local venv="$1" is_ipex="$2"
    [ -d "$venv" ] || return 0

    if [ "$is_ipex" = "1" ]; then
        print_step "Updating Python deps in $venv (Intel Arc — frozen pins protected)"
        local cf
        cf="$(mktemp)"
        # Pin the fragile packages to their current versions.
        "$venv/bin/pip" freeze 2>/dev/null | grep -iE "$_IPEX_FROZEN_RE" > "$cf" || true
        # SAFETY: never run an unconstrained --upgrade on an Arc venv. If we
        # couldn't capture the fragile pins, an empty constraints file would let
        # pip pull numpy>=2 / a newer torch and break IPEX — so bail out instead.
        if [ ! -s "$cf" ]; then
            print_warning "Could not detect the Intel Arc/IPEX pins in $venv — skipping its"
            print_warning "dependency upgrade to avoid breaking it. Upgrade it manually if needed."
            rm -f "$cf"
            return 0
        fi
        echo "  Frozen (will NOT be upgraded):"
        sed 's/^/    /' "$cf"
        if "$venv/bin/pip" install -r requirements.txt --upgrade -c "$cf" -q; then
            print_success "Updated $venv (Arc pins preserved)"
        else
            print_warning "Update aborted to protect the Arc pins — a dependency wanted a version"
            print_warning "that conflicts with a frozen pin (e.g. numpy>=2). Nothing was changed;"
            print_warning "the Intel Arc environment is left intact."
        fi
        # Merged bot framework deps (psycopg2, edge-tts, pytz, …) — also pin-protected.
        if [ -f botframework/requirements.txt ]; then
            "$venv/bin/pip" install -r botframework/requirements.txt --upgrade -c "$cf" -q \
                && print_success "Updated $venv with botframework deps" \
                || print_warning "Some botframework deps skipped in $venv (pin conflict)"
        fi
        # Re-assert the numpy<2 requirement just in case.
        "$venv/bin/pip" install "numpy<2" -q 2>/dev/null || true
        rm -f "$cf"
    else
        print_step "Updating Python deps in $venv"
        if "$venv/bin/pip" install -r requirements.txt --upgrade -q; then
            print_success "Updated $venv"
        else
            print_warning "Some deps failed to update in $venv (see output above)."
        fi
        # Merged bot framework deps.
        if [ -f botframework/requirements.txt ]; then
            "$venv/bin/pip" install -r botframework/requirements.txt --upgrade -q \
                || print_warning "Some botframework deps failed to update in $venv."
        fi
    fi
}

run_updates() {
    print_step "Update posterchanai dependencies + local Telegram Bot API server"
    cd "$SCRIPT_DIR" || return 1

    if [ ! -f requirements.txt ]; then
        print_error "requirements.txt not found in $SCRIPT_DIR — run this from the posterchanai checkout."
        return 1
    fi

    # 1) Python deps for whichever venvs exist (the helper skips ones that don't). venv-unified
    # is the modern Intel Arc venv (chat llama.cpp SYCL + image torch-XPU); the "1" freezes the
    # fragile pins (torch/torchvision/transformers) so a generic upgrade can't pull a CPU torch
    # over the XPU build. venv-ipex/venv-xpu are kept for older installs that still have them.
    _update_one_venv "venv" 0
    _update_one_venv "venv-unified" 1
    _update_one_venv "venv-ipex" 1
    _update_one_venv "venv-xpu" 1

    # 1b) Depth model for the `alive` 3D-parallax effect (gitignored; fetch if missing
    # so existing installs gain the feature on update).
    download_depth_model

    # 1b2) u2net model for the `removebackground` command (fetch if missing so existing
    # installs gain the feature on update).
    download_u2net_model

    # 1c) ACE-Step music server (if installed): git pull + uv sync + re-swap torch + restart.
    update_music_server

    # 1d) This node's own SearXNG. An UPGRADE is the path that matters most here: an existing install
    # is exactly the one whose Admin → Tools points at some other box (or at nothing), and search —
    # the AI's web lookups, the news digests, the bots, Web Search — silently returns nothing the day
    # that box goes away. Idempotent: it keeps an existing settings.yml and just re-runs the
    # container, and it pulls a newer image while it's here. Non-fatal (no docker = no bundled
    # instance; the node falls back to whatever Admin → Tools says).
    setup_searxng || print_warning "SearXNG setup skipped — Admin → Tools → SearXNG URL still decides where this node searches"

    # 2) Telegram Bot API server (rebuild to the latest upstream).
    if command -v telegram-bot-api >/dev/null 2>&1; then
        echo ""
        read -p "Rebuild the local Telegram Bot API server to the latest version (~10-20 min)? [y/N]: " UPD_TG
        if [[ "$UPD_TG" =~ ^[Yy] ]]; then
            REBUILD=1 bash "$SCRIPT_DIR/scripts/setup-telegram-local-api.sh" \
                || print_warning "Telegram Bot API server rebuild failed (existing one keeps running)."
        fi
    fi

    # 3) Restart whichever posterchanai services are installed on this host.
    echo ""
    print_step "Restarting services"
    local restarted=0
    for svc in posterchanai-cpu posterchanai-rocm posterchanai; do
        if systemctl list-unit-files 2>/dev/null | grep -q "^${svc}\.service"; then
            sudo systemctl restart "${svc}.service" 2>/dev/null && { echo "  ✓ restarted ${svc}"; restarted=1; }
        fi
    done
    [ "$restarted" = "0" ] && echo "  (no posterchanai systemd services found — restart manually if needed)"

    print_success "Update complete."
}
