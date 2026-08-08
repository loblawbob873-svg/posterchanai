#!/bin/bash
# Install a SearXNG of this node's OWN, as a systemd service (add-on: ./install.sh --searxng).
#
# WHY THIS EXISTS. Every search this app does — the AI's web-search tool, the news digests, the bots,
# and the Web Search screen — goes through one SearXNG instance. For a long time a node that never
# filled in Admin → Tools silently searched through ONE deployment's box (the old hardcoded
# `search.poster.place` default), which made that box a single point of failure for everyone else,
# and the obvious replacement — a public instance — answers 429 Too Many Requests to anything that
# doesn't look like a browser. So a node runs its own.
#
# SHAPE. `posterchanai-searxng.service` runs the official container in the FOREGROUND under systemd,
# like every other service here (`systemctl status posterchanai-searxng`, journald logs, one restart
# policy), rather than a detached `--restart=always` container that no unit file knows about.
#
# `--network host`, which is not a shortcut: the container has to reach this node's HTTP proxy on
# 127.0.0.1 to send its engine requests through Tor, and that proxy binds to proxy_listen_host
# (loopback by default). From a bridge network there is nothing at that address — the first version
# of this pointed the container at host.docker.internal and every engine request would have failed
# while /healthz kept answering, i.e. an instance that looks healthy and returns nothing.
#
# WHAT THE APP DOES WITH IT. Nothing to configure: search_service probes 127.0.0.1:<port>/healthz +
# /config and uses it whenever Admin → Tools → SearXNG URL is EMPTY (a value there always wins). The
# port is written to searxng/port, because an env var set at install time never reaches the app's
# systemd service.
#
# Docker/podman rather than a bare-metal install: SearXNG upstream ships a container, and its
# bare-metal path is a uwsgi/nginx build against system Python that would fight the app's own venv.

SEARXNG_IMAGE="${SEARXNG_IMAGE:-docker.io/searxng/searxng:latest}"
SEARXNG_NAME="${SEARXNG_NAME:-posterchanai-searxng}"
SEARXNG_UNIT="posterchanai-searxng"

setup_searxng() {
    print_banner 2>/dev/null || true
    echo -e "${BOLD:-}🔎 Installing this node's own SearXNG (private metasearch)${NC:-}"
    echo ""

    # 8899, not 8888: MediaMTX serves HLS on 8888 on every node that streams, which is the default.
    local port repo_root conf_dir
    port="${POSTERCHANAI_SEARXNG_PORT:-8899}"
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    conf_dir="${SEARXNG_CONFIG_DIR:-$repo_root/searxng}"

    local runner=""
    if command -v docker >/dev/null 2>&1; then runner="$(command -v docker)"
    elif command -v podman >/dev/null 2>&1; then runner="$(command -v podman)"
    else
        print_error "docker (or podman) is required to run the bundled SearXNG" 2>/dev/null \
            || echo "ERROR: docker (or podman) is required"
        echo "Install docker, then re-run: ./install.sh --searxng"
        echo "Or point Admin → Tools → SearXNG URL at an instance you already run."
        return 1
    fi

    mkdir -p "$conf_dir/brand"
    # The container's entrypoint CHOWNS its config directory to the searxng user (uid 977) on every
    # start, so from the second run onward this script cannot write its own files. Take the directory
    # back before touching anything — without this the settings rewrite fails with EACCES, and since
    # the failure is a stray traceback in the middle of an installer that then reports success, the
    # node keeps whatever settings.yml it had (i.e. the proxy block never refreshes).
    if [ ! -w "$conf_dir" ] || [ -e "$conf_dir/settings.yml" -a ! -w "$conf_dir/settings.yml" ]; then
        sudo chown -R "$(id -u):$(id -g)" "$conf_dir" 2>/dev/null || true
    fi

    # --- the outgoing proxy -------------------------------------------------------------------
    # Engine requests go through this node's HTTP proxy, specifically its FALLBACK listener
    # (proxy_fallback_port, default 8119: Tor1 → Tor2 → direct). NOT the main :8118, which is
    # Tor-only by design because torrents share it — pointed there, one Tor outage turns every search
    # into a timeout that reads as "no results".
    #
    # PROBED, not read from settings: the settings store only loads inside the running app, so a CLI
    # read answers None on a node that has a proxy, and we would silently configure direct search.
    # Force either way with SEARXNG_TOR=1 / SEARXNG_TOR=0.
    local proxy_url="" cand
    for cand in ${SEARXNG_PROXY:+"$SEARXNG_PROXY"} \
                "http://127.0.0.1:${POSTERCHANAI_PROXY_FALLBACK_PORT:-8119}"; do
        if curl -fsS -m 5 -x "$cand" "http://example.com/" >/dev/null 2>&1; then proxy_url="$cand"; break; fi
    done
    local proxy_up=""
    [ -n "$proxy_url" ] && proxy_up="1"
    case "${SEARXNG_TOR:-auto}" in
        0) proxy_up="" ;;
        1) proxy_up="1"; [ -n "$proxy_url" ] || proxy_url="http://127.0.0.1:${POSTERCHANAI_PROXY_FALLBACK_PORT:-8119}" ;;
    esac
    # With --network host the container shares this namespace, so 127.0.0.1 IS the proxy. No
    # host.docker.internal, no bridge gateway, nothing to resolve.
    local proxy_block
    if [ -n "$proxy_up" ]; then
        proxy_block=$(printf 'outgoing:\n  proxies:\n    all://:\n      - %s' "$proxy_url")
        echo "Tor: engine requests go through this node's proxy ($proxy_url → Tor1 → Tor2 → direct)"
    else
        proxy_block=$(printf '# outgoing:\n#   proxies:\n#     all://:\n#       - %s   # Tor1 → Tor2 → direct' \
                      "http://127.0.0.1:${POSTERCHANAI_PROXY_FALLBACK_PORT:-8119}")
        echo "Tor: no HTTP proxy answering — engine requests will go DIRECT"
        echo "     (turn the proxy on in Admin → Network, restart posterchanai, then: SEARXNG_TOR=1 ./install.sh --searxng)"
    fi

    # --- settings.yml -------------------------------------------------------------------------
    # ONE template, shared with the compose service (docker/searxng/settings.yml), so the two install
    # paths cannot drift — the JSON API and the disabled limiter are the difference between an
    # instance that works with this app and one that silently answers 403 to everything.
    local template secret secret_file
    template="$repo_root/docker/searxng/settings.yml"
    [ -f "$template" ] || { print_error "missing $template" 2>/dev/null || echo "ERROR: missing $template"; return 1; }

    secret_file="$conf_dir/.secret"
    if [ -s "$secret_file" ]; then
        secret="$(cat "$secret_file")"
    else
        # Generated once. Regenerating it per install would invalidate existing preference cookies
        # for no reason.
        secret="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        printf '%s' "$secret" > "$secret_file"
        chmod 600 "$secret_file"
    fi

    # An existing settings.yml is the OPERATOR's file and is kept — except for the outgoing-proxy
    # block, which is refreshed on every run. That block is decided by probing, and on a FRESH
    # install the probe runs before the app (and therefore its proxy) has ever started, so it always
    # says "no proxy". Frozen, that would pin the node's engine requests to DIRECT forever, from the
    # operator's real IP, with nothing afterwards to say so. Now every `./install.sh --searxng` and
    # every upgrade re-decides it.
    if [ -f "$conf_dir/settings.yml" ] && [ "${SEARXNG_FORCE_SETTINGS:-0}" != "1" ]; then
        echo "Keeping $conf_dir/settings.yml (refreshing its outgoing-proxy block only)"
        PC_OUTGOING="$proxy_block" "${PYTHON:-python3}" - "$conf_dir/settings.yml" <<'PY'
import os, re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
block = os.environ["PC_OUTGOING"].rstrip("\n")
# Replace an existing outgoing block (live OR commented) wholesale; append when there is none. The
# block always sits at the end of the file, which is where the template's marker put it.
pat = re.compile(r"(?ms)^#?\s?outgoing:\n(?:^[#\s].*\n?)*")
text = pat.sub("", text).rstrip("\n")
open(path, "w", encoding="utf-8").write(text + "\n\n" + block + "\n")
PY
        # A write that failed (the container chowns this directory to its own user, so it happens)
        # must be LOUD: the installer goes on to report success, and a stale proxy block means the
        # node queries engines from its real IP with nothing on screen to say so.
        [ $? -eq 0 ] || print_warning "could not refresh the outgoing-proxy block in $conf_dir/settings.yml" 2>/dev/null \
            || echo "WARNING: could not refresh the outgoing-proxy block in $conf_dir/settings.yml"
    else
        # `python -` rather than sed: the proxy block is multi-line, and a sed replacement with
        # newlines is the kind of quoting that works until the day a path contains a slash.
        PC_SECRET="$secret" PC_OUTGOING="$proxy_block" "${PYTHON:-python3}" - "$template" "$conf_dir/settings.yml" <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
text = text.replace('secret_key: "ultrasecretkey"', 'secret_key: "%s"' % os.environ["PC_SECRET"])
out = []
for line in text.splitlines():
    if "@PC_OUTGOING@" in line:
        out.append(os.environ["PC_OUTGOING"])
        continue
    # the marker's continuation lines are comments; drop them with it
    if out and out[-1].startswith(("outgoing:", "# outgoing:")) and line.startswith("# "):
        continue
    out.append(line)
open(dst, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
        echo "Wrote $conf_dir/settings.yml"
    fi

    # --- branding -----------------------------------------------------------------------------
    # The header logo and the favicon, mounted over the image's own files. Cosmetic — the only client
    # is the app — but this page IS what an operator sees at 127.0.0.1:$port, and it may as well say
    # whose node it is. The dark theme is in settings.yml (ui.theme_args.simple_style).
    local logo_src="$repo_root/static/posterchan-relay.png"
    local brand_ok=""
    if [ -f "$logo_src" ]; then
        cp -f "$logo_src" "$conf_dir/brand/logo.png" && brand_ok="1"
    fi

    # --- the unit -----------------------------------------------------------------------------
    echo "Pulling $SEARXNG_IMAGE …"
    "$runner" pull "$SEARXNG_IMAGE" || { print_error "image pull failed" 2>/dev/null || echo "ERROR: pull failed"; return 1; }

    # Any container left over from an earlier (detached) install, or from the last run of the unit.
    "$runner" rm -f "$SEARXNG_NAME" >/dev/null 2>&1 || true

    local brand_mounts=""
    if [ -n "$brand_ok" ]; then
        local img_dir="/usr/local/searxng/searx/static/themes/simple/img"
        local f
        for f in searxng.png favicon.png 192.png 512.png; do
            brand_mounts="$brand_mounts -v $conf_dir/brand/logo.png:$img_dir/$f:ro"
        done
    fi

    sudo tee "/etc/systemd/system/${SEARXNG_UNIT}.service" > /dev/null <<EOF
[Unit]
Description=PosterChan SearXNG (private metasearch for this node)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
# --network host: the container must reach this node's HTTP proxy on 127.0.0.1 (Tor1 → Tor2 →
# direct) for its engine requests, and that proxy binds to loopback. It also puts SearXNG on
# 127.0.0.1:${port} directly, which is where the app probes for it.
#
# GRANIAN_HOST/GRANIAN_PORT and NOT SEARXNG_BIND_ADDRESS/SEARXNG_PORT: this image serves through
# granian, which reads its own variables (SEARXNG_PORT happens to be aliased by the entrypoint;
# SEARXNG_BIND_ADDRESS is not read at all, and neither is server.bind_address in settings.yml).
# MEASURED, not assumed: with SEARXNG_BIND_ADDRESS=127.0.0.1 set, `ss -ltn` showed *:${port} —
# i.e. in the host namespace this was an unauthenticated, limiter-disabled metasearch instance
# listening on every interface of the box.
# --rm + foreground: systemd owns the lifecycle, so \`systemctl restart\` really restarts it.
ExecStartPre=-$runner rm -f $SEARXNG_NAME
ExecStart=$runner run --rm --name $SEARXNG_NAME \\
    --network host \\
    -e GRANIAN_HOST=127.0.0.1 \\
    -e GRANIAN_PORT=${port} \\
    -e SEARXNG_BASE_URL=http://127.0.0.1:${port}/ \\
    -v $conf_dir:/etc/searxng:rw$brand_mounts \\
    $SEARXNG_IMAGE
ExecStop=-$runner stop -t 10 $SEARXNG_NAME
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable "${SEARXNG_UNIT}.service" >/dev/null 2>&1 || true
    sudo systemctl restart "${SEARXNG_UNIT}.service" || {
        print_error "could not start ${SEARXNG_UNIT}.service" 2>/dev/null || echo "ERROR: unit did not start"
        echo "  journalctl -u ${SEARXNG_UNIT} -n 50"
        return 1
    }

    # The app reads this: an env var set HERE never reaches the app's own systemd service, so a
    # non-default port would install an instance the app then looks for on 8899 and never finds.
    printf '%s' "$port" > "$conf_dir/port"

    # --- verify the thing the app actually needs -----------------------------------------------
    # Not "the container is up": a running SearXNG with the JSON API off is the failure mode that
    # looks like success, and it is exactly what the app cannot use.
    echo -n "Waiting for SearXNG"
    local i ok=""
    for i in $(seq 1 40); do
        if curl -fsS -m 3 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then ok="1"; break; fi
        echo -n "."; sleep 2
    done
    echo ""
    if [ -z "$ok" ]; then
        print_warning "SearXNG did not answer on 127.0.0.1:${port} — check: journalctl -u ${SEARXNG_UNIT} -n 50" 2>/dev/null \
            || echo "WARNING: no answer on 127.0.0.1:${port}"
        return 1
    fi
    if curl -fsS -m 15 "http://127.0.0.1:${port}/search?q=test&format=json" 2>/dev/null | head -c 1 | grep -q '{'; then
        print_success "SearXNG is up on 127.0.0.1:${port} and answering JSON" 2>/dev/null \
            || echo "OK: SearXNG up on 127.0.0.1:${port} (JSON enabled)"
    else
        print_warning "SearXNG is up but its JSON API is not answering — check 'search.formats' in $conf_dir/settings.yml" 2>/dev/null \
            || echo "WARNING: JSON API not answering"
        return 1
    fi

    echo ""
    echo "Service:  systemctl status ${SEARXNG_UNIT}   ·   journalctl -u ${SEARXNG_UNIT} -f"
    echo "Leave Admin → Tools → SearXNG URL EMPTY to use it; the app probes it automatically."
    echo "Set POSTERCHANAI_SEARXNG_PORT to install on a different port (recorded in $conf_dir/port)."
    return 0
}
