#!/bin/bash
# Install a SearXNG of this node's OWN — NATIVELY, in the app's venv (add-on: ./install.sh --searxng).
#
# WHY THIS EXISTS. Every search this app does — the AI's web-search tool, the news digests, the bots,
# and the Web Search screen — goes through one SearXNG instance. For a long time a node that never
# filled in Admin → Tools silently searched through ONE deployment's box (the old hardcoded
# `search.poster.place` default), which made that box a single point of failure for everyone else,
# and the obvious replacement — a public instance — answers 429 Too Many Requests to anything that
# doesn't look like a browser. So a node runs its own.
#
# WHAT CHANGED. This used to run the upstream Docker image, on the reasoning that "SearXNG upstream
# ships a container, and its bare-metal path is a uwsgi/nginx build against system Python that would
# fight the app's own venv". That is true of upstream's *deployment* documentation and false of the
# package: `searx.webapp.app` is an ordinary WSGI (Flask) application. So it is installed into the
# app's venv like any other dependency, and docker is no longer required to search.
#
# SHAPE, unchanged where it matters. `posterchanai-searxng.service` still exists and is still what
# `systemctl status posterchanai-searxng` reports on — it now runs `python -m
# app.services.searxng_native` (uvicorn + a2wsgi, loopback) out of the app's venv instead of a
# container. And because the same code is importable by the app itself, the app MOUNTS it at
# /searxng as a fallback: when this unit is stopped, masked or crashed, search keeps working instead
# of falling through to a public instance. See app/services/searxng_native.py.
#
# THREE THINGS THAT BITE, each measured here rather than guessed:
#
#   * SearXNG is NOT ON PyPI (`pip index versions searxng` → "No matching distribution found"), so
#     its SOURCE is cloned and installed --no-deps — the ACE-Step pattern. Its runtime deps are in
#     the app's requirements.txt at RANGES, never its own exact pins, which would be licence for pip
#     to move typing-extensions/certifi/lxml out from under torch and pydantic.
#   * --no-build-isolation is REQUIRED. Its setup.py does `from searx.version import ...`, which
#     imports searx/__init__.py, which imports msgspec — absent from pip's isolated build env, so the
#     build dies with ModuleNotFoundError before any dependency of ours is consulted.
#   * The clone SHIPS its built static assets (searx/static/themes/simple/*.min.css and friends are
#     committed), so there is no node/webpack build here. Do not add one.
#
# WHAT THE APP DOES WITH IT. Nothing to configure: search_service probes 127.0.0.1:<port>/healthz +
# /config and uses it whenever Admin → Tools → SearXNG URL is EMPTY (a value there always wins). The
# port is written to searxng/port, because an env var set at install time never reaches the app's
# systemd service.

SEARXNG_REPO="${SEARXNG_REPO:-https://github.com/searxng/searxng.git}"
SEARXNG_UNIT="posterchanai-searxng"
# The RETIRED container, removed on upgrade. Left running it would keep answering on the same port —
# the app would use it, every fix here would look like it had no effect, and the settings file the
# two share would be edited by an installer whose changes never reached the process serving.
SEARXNG_OLD_CONTAINER="posterchanai-searxng"

_searxng_find_venv() {
    local d
    for d in "$SCRIPT_DIR/venv-unified" "$SCRIPT_DIR/venv" "$HOME/posterchanai/venv-unified"; do
        [ -x "$d/bin/python" ] && { echo "$d"; return 0; }
    done
    echo "$SCRIPT_DIR/venv-unified"   # nothing found: report the conventional path in the error
}

setup_searxng() {
    print_banner 2>/dev/null || true
    echo -e "${BOLD:-}🔎 Installing this node's own SearXNG (private metasearch)${NC:-}"
    echo ""

    # 8899, not 8888: MediaMTX serves HLS on 8888 on every node that streams, which is the default.
    local port repo_root conf_dir src_dir venv py
    port="${POSTERCHANAI_SEARXNG_PORT:-8899}"
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    conf_dir="${SEARXNG_CONFIG_DIR:-$repo_root/searxng}"
    src_dir="${SEARXNG_SRC_DIR:-$HOME/searxng}"
    venv="$(_searxng_find_venv)"
    py="$venv/bin/python"

    if [ ! -x "$py" ]; then
        print_error "App venv not found at $venv — run the main install first." 2>/dev/null \
            || echo "ERROR: app venv not found at $venv"
        return 1
    fi

    mkdir -p "$conf_dir/brand" "$conf_dir/cache"
    # Historical: the container chowned this directory to its own user (uid 977) on every start, so
    # from the second run onward this script could not write its own files. Nodes upgrading from that
    # still have those permissions, and the failure is a stray traceback in the middle of an
    # installer that then reports success.
    if [ ! -w "$conf_dir" ] || { [ -e "$conf_dir/settings.yml" ] && [ ! -w "$conf_dir/settings.yml" ]; }; then
        sudo chown -R "$(id -u):$(id -g)" "$conf_dir" 2>/dev/null || true
    fi

    # --- the source ---------------------------------------------------------------------------
    if [ ! -d "$src_dir/.git" ]; then
        print_step "Cloning SearXNG into $src_dir ..." 2>/dev/null || echo "Cloning SearXNG into $src_dir ..."
        git clone --depth 1 "$SEARXNG_REPO" "$src_dir" \
            || { print_error "git clone failed" 2>/dev/null || echo "ERROR: git clone failed"; return 1; }
    else
        print_step "Updating SearXNG in $src_dir ..." 2>/dev/null || echo "Updating SearXNG in $src_dir ..."
        git -C "$src_dir" pull --ff-only 2>/dev/null || echo "  (kept the checkout as-is)"
    fi

    # --- into the venv ------------------------------------------------------------------------
    print_step "Installing SearXNG into $venv (--no-deps) ..." 2>/dev/null || echo "Installing SearXNG (--no-deps) ..."
    "$venv/bin/pip" install -q --no-deps --no-build-isolation -e "$src_dir" \
        || { print_error "pip install failed" 2>/dev/null || echo "ERROR: pip install failed"; return 1; }

    # Its runtime deps live in the app's requirements.txt, so a normal install already has them. A
    # venv that predates that does not — and the symptom is `import searx` raising ModuleNotFoundError
    # for something like msgspec, which reads as "SearXNG is broken" rather than "deps are old". Try
    # once, from the app's own requirements file, so the version ranges stay in ONE place.
    if ! "$py" -c "import searx" >/dev/null 2>&1; then
        print_step "Installing SearXNG's runtime dependencies from requirements.txt ..." 2>/dev/null \
            || echo "Installing SearXNG's runtime dependencies ..."
        "$venv/bin/pip" install -q -r "$repo_root/requirements.txt" || true
    fi
    if ! "$py" -c "import searx" >/dev/null 2>&1; then
        print_error "SearXNG installed but will not import:" 2>/dev/null || echo "ERROR: searx will not import:"
        "$py" -c "import searx" 2>&1 | tail -3
        return 1
    fi

    # --- the outgoing proxy -------------------------------------------------------------------
    # Engine requests go through this node's HTTP proxy, specifically its FALLBACK listener
    # (proxy_fallback_port, default 8119: Tor1 → Tor2 → direct). NOT the main :8118, which is
    # Tor-only by design because torrents share it — pointed there, one Tor outage turns every search
    # into a timeout that reads as "no results".
    #
    # OFF BY DEFAULT, and that is a measurement rather than a preference. Routed through Tor, the
    # default engine set does not merely slow down — it stops answering: Brave and Google CSE return
    # "too many requests", DuckDuckGo "access denied", Startpage a CAPTCHA, and SearXNG then SUSPENDS
    # each of them for up to an hour. Measured on this node, same query, same minute: 25 results
    # direct, 0 results through Tor with all four engines suspended. Search engines block exit nodes;
    # no amount of timeout tuning changes that (the 12s timeout below is still needed when Tor IS on,
    # since the 3s default times out on its own).
    #
    # So: `SEARXNG_TOR=1` opts in, and the app→instance hop still goes through Tor for any REMOTE
    # instance (search_service.search_transport) — that part costs nothing and is always on.
    #
    # The proxy is PROBED rather than read from settings, because the settings store only loads inside
    # the running app and a CLI read answers None on a node that has one.
    # 25s and two attempts, not 5s and one: the first request through a freshly started Tor waits on
    # circuit construction, so a tight probe intermittently answers "no proxy" on a node that has a
    # working one. (Measured: warm, this returns in 0.8s.)
    local proxy_url="" proxy_up="" cand attempt
    if [ "${SEARXNG_TOR:-0}" = "1" ]; then
        for attempt in 1 2; do
            for cand in ${SEARXNG_PROXY:+"$SEARXNG_PROXY"} \
                        "http://127.0.0.1:${POSTERCHANAI_PROXY_FALLBACK_PORT:-8119}"; do
                if curl -fsS -m 25 -x "$cand" "http://example.com/" >/dev/null 2>&1; then
                    proxy_url="$cand"; proxy_up="1"; break 2
                fi
            done
            sleep 2
        done
        [ -n "$proxy_up" ] || print_warning "SEARXNG_TOR=1 but no proxy answered — engine requests will go DIRECT" 2>/dev/null \
            || echo "WARNING: SEARXNG_TOR=1 but no proxy answered; engine requests will go DIRECT"
    fi
    # Native, so 127.0.0.1 is simply 127.0.0.1 — no --network host, no host.docker.internal, nothing
    # to resolve. This is the paragraph that used to be the hardest part of running it in a container.
    local proxy_block
    if [ -n "$proxy_up" ]; then
        # The TIMEOUTS ride with the proxy, and they are not padding. SearXNG's default engine
        # timeout is 3s; over Tor that is short enough that essentially everything times out —
        # MEASURED on this node: 0 results with the default, 25 results (2 unresponsive engines) at
        # 12s, same query, same circuits. Without this, "route search through Tor" reads to the user
        # as "search is broken", which is how a privacy feature gets turned back off.
        proxy_block=$(printf 'outgoing:\n  request_timeout: 12.0\n  max_request_timeout: 20.0\n  proxies:\n    all://:\n      - %s' "$proxy_url")
        echo "Tor: engine requests go through this node's proxy ($proxy_url → Tor1 → Tor2 → direct)"
    else
        proxy_block=$(printf '# outgoing:\n#   request_timeout: 12.0   # 3s (the default) is too short over Tor\n#   max_request_timeout: 20.0\n#   proxies:\n#     all://:\n#       - %s   # Tor1 → Tor2 → direct' \
                      "http://127.0.0.1:${POSTERCHANAI_PROXY_FALLBACK_PORT:-8119}")
        echo "Tor: engine requests go DIRECT — search engines block Tor exits (measured: 0 results"
        echo "     through Tor with every engine suspended, vs 25 direct). Opt in anyway with:"
        echo "       SEARXNG_TOR=1 ./install.sh --searxng"
        echo "     The app→instance hop uses Tor regardless, for any REMOTE instance."
    fi

    # --- settings.yml -------------------------------------------------------------------------
    # ONE template for both install paths (docker/searxng/settings.yml, also baked into the image), so
    # a host install and a container install cannot drift — the JSON API and the disabled limiter are
    # the difference between an instance that works with this app and one that silently 403s.
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
        # A write that failed must be LOUD: the installer goes on to report success, and a stale
        # proxy block means the node queries engines from its real IP with nothing on screen to say so.
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
    # The header logo and the favicon. Cosmetic — the only client is the app — but this page IS what
    # an operator sees at 127.0.0.1:$port, and it may as well say whose node it is. Written into the
    # CHECKOUT's static directory, which is where a read-only bind mount used to do the same job; the
    # dark theme is in settings.yml (ui.theme_args.simple_style).
    local logo_src="$repo_root/static/posterchan-relay.png"
    local img_dir="$src_dir/searx/static/themes/simple/img"
    if [ -f "$logo_src" ] && [ -d "$img_dir" ]; then
        cp -f "$logo_src" "$conf_dir/brand/logo.png" 2>/dev/null || true
        local f
        for f in searxng.png favicon.png 192.png 512.png; do
            [ -e "$img_dir/$f" ] && cp -f "$logo_src" "$img_dir/$f" 2>/dev/null || true
        done
    fi

    # --- retire the container -----------------------------------------------------------------
    # An upgrade, not a fresh install, is where this matters: the old unit runs a container on the
    # SAME port and would keep answering it, so the app would go on using a SearXNG that no longer
    # reads the settings file this installer writes.
    local runner=""
    if command -v docker >/dev/null 2>&1; then runner="$(command -v docker)"
    elif command -v podman >/dev/null 2>&1; then runner="$(command -v podman)"; fi
    if [ -n "$runner" ] && "$runner" ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$SEARXNG_OLD_CONTAINER"; then
        echo "Removing the retired SearXNG container (this now runs natively) ..."
        sudo systemctl stop "${SEARXNG_UNIT}.service" >/dev/null 2>&1 || true
        "$runner" rm -f "$SEARXNG_OLD_CONTAINER" >/dev/null 2>&1 || true
    fi

    # --- the unit -----------------------------------------------------------------------------
    # WorkingDirectory is the repo, because `-m app.services.searxng_native` has to import the app
    # package. The settings path does NOT depend on it (searxng_native derives it from __file__), so
    # a run from anywhere still finds the right file.
    sudo tee "/etc/systemd/system/${SEARXNG_UNIT}.service" > /dev/null <<EOF
[Unit]
Description=PosterChan SearXNG (private metasearch for this node)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$repo_root
Environment=POSTERCHANAI_SEARXNG_PORT=${port}
Environment=SEARXNG_SETTINGS_PATH=${conf_dir}/settings.yml
# TMPDIR, because SearXNG's engine/data caches are SQLite files it puts in
# tempfile.gettempdir() — there is no cache_dir setting, TMPDIR is the only lever. On a node where
# /tmp is a tmpfs (this deployment's server1 is, and it has no swap) those files are RAM that
# free/MemAvailable do not report as reclaimable. Here they are ordinary files, which also means the
# 7-day engine cache survives a restart instead of being rebuilt on the next search.
Environment=TMPDIR=${conf_dir}/cache
# uvicorn + a2wsgi out of the app's own venv — no container, no granian, no second server to install.
# 127.0.0.1 ONLY: this instance has its limiter disabled (the limiter is what makes public instances
# 429 a server, which is the whole reason a node runs its own), so anything but loopback would be an
# open metasearch proxy on this box. An earlier, containerised version of this listened on *:${port}
# because the image read its bind address from GRANIAN_HOST and ignored the two settings that
# claimed to set it.
ExecStart=$py -m app.services.searxng_native
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
    # Not "the process is up": a running SearXNG with the JSON API off is the failure mode that looks
    # like success, and it is exactly what the app cannot use.
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
    echo "Source:   $src_dir (git pull + ./install.sh --searxng to update)"
    echo "Leave Admin → Tools → SearXNG URL EMPTY to use it; the app probes it automatically, and"
    echo "serves the same SearXNG itself at /searxng if this unit is ever down."
    echo "Set POSTERCHANAI_SEARXNG_PORT to install on a different port (recorded in $conf_dir/port)."
    return 0
}
