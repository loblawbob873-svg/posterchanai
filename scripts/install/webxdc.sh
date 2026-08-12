#!/bin/bash
# The webxdc mini-app sandbox origin (add-on: ./install.sh --webxdc).
#
# WHY THIS EXISTS. A mini app (.xdc — a game, a poll, a shared editor) is code somebody else wrote,
# and it runs in an iframe. The ONLY thing between that code and the localStorage/IndexedDB this
# client keeps the user's Nostr key and session in is the same-origin policy. So an app must not run
# on the instance's own origin, and the client does not offer to: it always loads a mini app from
# `xdc.<instance-host>` (see `sandboxOrigin` in static/js/client/webxdc.js — the label is hardcoded
# on purpose, so there is nothing to configure and nothing to get out of step).
#
# The APP already answers both of the paths that origin needs (`/__sandbox__/` and `/sw.js`,
# host-gated in app/main.py). What a fresh node is missing is purely the front door: a DNS record, a
# certificate, and an nginx vhost. Without them the composer still offers "🎮 Mini app", posts still
# publish, and pressing Play shows a blank window FOREVER with nothing in any log — which is why
# this is an installer step rather than a paragraph in a README.
#
# WHAT IT DOES, in order, refusing rather than half-doing:
#   1. works out the instance hostname (or asks),
#   2. REFUSES if `xdc.<host>` does not resolve yet, printing the exact DNS record to add,
#   3. installs an HTTP-only vhost so the ACME challenge can be answered,
#   4. offers `certbot --nginx -d xdc.<host>` — its OWN certificate, matching how every other
#      subdomain here is done. NOT `--expand` on the main one: that rewrites the production
#      certificate for a game feature, and a failure there takes the whole instance off TLS.
#   5. installs the real vhost (nginx/webxdc-sandbox.conf.example, hostname + upstream substituted),
#      `nginx -t` BEFORE reloading and rolling back to the previous file if the test fails,
#   6. smoke-tests https://xdc.<host>/__sandbox__/ and says what it got.
#
# Safe to re-run: every step is idempotent, an unchanged config is left alone, and an existing
# certificate is never re-requested.
#
# Testing knobs (so this can be reviewed without reconfiguring a live box):
#   WEBXDC_DRY_RUN=1        print every action and change nothing (no sudo, no reload, no certbot)
#   WEBXDC_DOMAIN=…         instance hostname (skips the prompt; required with no tty)
#   WEBXDC_NGINX_DIR=…      where to write the vhost (default: autodetected /etc/nginx/…)
#   WEBXDC_UPSTREAM=…       host:port of the app (default 127.0.0.1:3051)
#   WEBXDC_SKIP_CERTBOT=1   never call certbot (bring your own certificate)
#   WEBXDC_CERTBOT=1        run certbot without asking (needed with no tty, where it is off)
#   WEBXDC_SKIP_DNS=1       skip the DNS refusal (split-horizon DNS, or a record that is propagating)
#   WEBXDC_LE_DIR=…         certificate root (default /etc/letsencrypt)

WEBXDC_LABEL="xdc"     # must match WEBXDC_SANDBOX_LABEL in app/main.py and SANDBOX_LABEL in webxdc.js

# --- small helpers -------------------------------------------------------------------------------

_wx_say()   { echo -e "${1}"; }
_wx_ok()    { print_success "$1" 2>/dev/null || echo "OK: $1"; }
_wx_warn()  { print_warning "$1" 2>/dev/null || echo "WARNING: $1"; }
_wx_err()   { print_error   "$1" 2>/dev/null || echo "ERROR: $1"; }

# Run a privileged command — unless this is a dry run, in which case just show it. Everything that
# touches /etc or reloads a service goes through here, so WEBXDC_DRY_RUN=1 is a real dry run and not
# a promise.
_wx_sudo() {
    # stderr, not stdout: _wx_install_file's result is read through a command substitution, and a
    # dry-run line landing in there would be taken for its answer.
    if [ "${WEBXDC_DRY_RUN:-0}" = "1" ]; then
        echo "    [dry-run] $*" >&2
        return 0
    fi
    if [ "$(id -u)" = "0" ]; then "$@"; else sudo "$@"; fi
}

# Does this name resolve? Tried in order of what a minimal box actually has. `getent` also consults
# /etc/hosts, which is a legitimate way to point a LAN deployment at itself.
_wx_resolves() {
    local n="$1"
    if command -v getent >/dev/null 2>&1 && getent hosts "$n" >/dev/null 2>&1; then return 0; fi
    if command -v dig    >/dev/null 2>&1 && [ -n "$(dig +short "$n" 2>/dev/null)" ]; then return 0; fi
    if command -v host   >/dev/null 2>&1 && host "$n" >/dev/null 2>&1; then return 0; fi
    if command -v python3 >/dev/null 2>&1 && \
       python3 -c "import socket,sys; socket.getaddrinfo(sys.argv[1], None)" "$n" >/dev/null 2>&1; then return 0; fi
    return 1
}

_wx_addrs() {
    if command -v getent >/dev/null 2>&1; then
        getent ahosts "$1" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' | sed 's/ *$//'
    elif command -v dig >/dev/null 2>&1; then
        dig +short "$1" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//'
    fi
}

# The instance's own hostname, guessed from the nginx vhost that already proxies to the app. A guess
# is offered as the prompt's default and never used silently — getting this wrong installs a vhost
# for a name nobody visits, which is indistinguishable from not installing one at all.
_wx_guess_domain() {
    local f n
    for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf /etc/nginx/sites-available/*; do
        [ -f "$f" ] || continue
        grep -qE ':(3051|3052)\b' "$f" 2>/dev/null || continue
        n=$(grep -hE '^[[:space:]]*server_name' "$f" 2>/dev/null \
            | sed -E 's/^[[:space:]]*server_name[[:space:]]+//; s/;.*//' \
            | tr ' ' '\n' | grep '\.' \
            | grep -vE "^(_|localhost|\*|${WEBXDC_LABEL}\.)" | grep -v '^\*\.' | head -1)
        if [ -n "$n" ]; then echo "${n#www.}"; return 0; fi
    done
    if [ -n "${POSTERCHANAI_DOMAIN:-}" ] && [ "${POSTERCHANAI_DOMAIN}" != "example.com" ]; then
        echo "$POSTERCHANAI_DOMAIN"; return 0
    fi
    n="$(hostname -f 2>/dev/null || true)"
    case "$n" in *.*) echo "$n"; return 0;; esac
    return 1
}

# Debian keeps vhosts in sites-available + a symlink; everyone else uses conf.d. Pick whichever this
# box's nginx.conf actually includes, so the file is not written somewhere nginx never reads.
_wx_conf_dir() {
    if [ -n "${WEBXDC_NGINX_DIR:-}" ]; then echo "$WEBXDC_NGINX_DIR"; return 0; fi
    if [ -d /etc/nginx/sites-enabled ] && grep -qs 'sites-enabled' /etc/nginx/nginx.conf; then
        echo /etc/nginx/sites-available; return 0
    fi
    echo /etc/nginx/conf.d
}

# certbot names a lineage after the first -d, but appends -0001 etc. if that directory already
# exists (a re-issue after a failed one, most often). Reading it back beats assuming: a vhost
# pointing at a directory that isn't there fails `nginx -t`, and pointing at a STALE one serves an
# expired certificate months later with nothing said at install time.
_wx_cert_dir() {
    local host="$1" d best="" le
    le="${WEBXDC_LE_DIR:-/etc/letsencrypt}"
    for d in "$le/live/$host" "$le/live/$host"-*; do
        [ -f "$d/fullchain.pem" ] || continue
        best="$d"
    done
    if [ -z "$best" ]; then
        # `live/` is 0700 root on most distros, so an ordinary (non-root) run cannot SEE the
        # certificate it is about to point nginx at — and reading that as "no certificate" would
        # send a node with working TLS back to the HTTP-only stage, i.e. break mini apps by
        # re-running the tool that installs them. `renewal/*.conf` is world-readable, is written
        # once per lineage, and answers the same question without root.
        for d in "$le/renewal/$host.conf" "$le/renewal/$host"-*.conf; do
            [ -f "$d" ] || continue
            best="$le/live/$(basename "$d" .conf)"
        done
    fi
    [ -n "$best" ] && basename "$best"
}

# The lineage an already-installed vhost is pointing at. Last line of defence against the same
# downgrade: whatever this box can or cannot read under /etc/letsencrypt, a config that already
# names a certificate is proof that one exists.
_wx_cert_dir_in_use() {
    local dest="$1"
    [ -f "$dest" ] || return 1
    sed -nE 's#^[[:space:]]*ssl_certificate[[:space:]]+.*/live/([^/]+)/fullchain\.pem;.*#\1#p' \
        "$dest" | head -1 | grep . || return 1
}

# --- the two configs -----------------------------------------------------------------------------

# Phase 1: HTTP only. Its whole job is to let the ACME challenge through, so it must exist BEFORE
# certbot runs and must not redirect to an https:// vhost that has no certificate yet.
_wx_http_only_conf() {
    local host="$1" upstream="$2"
    cat <<EOF
# ${host} — the webxdc mini-app sandbox origin (PosterChanAI, ./install.sh --webxdc).
# HTTP-ONLY STAGE: this file exists so certbot can answer the ACME challenge for ${host}.
# Once the certificate is issued, ./install.sh --webxdc replaces it with the real vhost
# (see nginx/webxdc-sandbox.conf.example in the repo, and docs/WEBXDC.md).
#
# Mini apps CANNOT run over http://: a service worker needs a secure context, so registration
# fails and the app window stays blank. This stage is a stepping stone, not a deployment.
server {
    listen 80;
    listen [::]:80;
    server_name ${host};

    location /.well-known/acme-challenge/ { root /var/www/html; }

    location = /sw.js      { proxy_pass http://${upstream}; proxy_set_header Host \$host; }
    location /__sandbox__  { proxy_pass http://${upstream}; proxy_set_header Host \$host; }
    location / { default_type text/plain; return 200 "webxdc sandbox origin (no TLS yet)\n"; }
}
EOF
}

# Phase 2: the real vhost, rendered from the file the repo ships and reviews. Everything
# deployment-specific is a substitution, so the comments (which record two designs that were tried
# and measured first, and the duplicate-header bug that read as "Firefox forbids this") ship intact.
_wx_render_conf() {
    local example="$1" domain="$2" upstream="$3" certdir="$4" le
    le="${WEBXDC_LE_DIR:-/etc/letsencrypt}"
    sed -e "s/poster\.place/${domain}/g" \
        -e "s#http://192\.168\.0\.2:3051#http://${upstream}#g" \
        -e "s#/etc/letsencrypt/live/[^/]*/#${le}/live/${certdir}/#g" \
        "$example"
}

# Write $2 to $1 only if it differs, keeping one .bak. Echoes `changed` or `unchanged` so the caller
# can decide whether a reload is even needed.
_wx_install_file() {
    local dest="$1" content="$2" tmp
    if [ -f "$dest" ] && [ "$(cat "$dest")" = "$content" ]; then echo unchanged; return 0; fi
    tmp="$(mktemp)"
    printf '%s\n' "$content" > "$tmp"
    if [ -f "$dest" ]; then _wx_sudo cp -a "$dest" "$dest.bak"; fi
    _wx_sudo cp "$tmp" "$dest"
    _wx_sudo chmod 0644 "$dest"
    rm -f "$tmp"
    echo changed
}

_wx_nginx_test() {
    if [ "${WEBXDC_DRY_RUN:-0}" = "1" ]; then echo "    [dry-run] nginx -t" >&2; return 0; fi
    _wx_sudo nginx -t >/dev/null 2>&1
}

_wx_nginx_reload() {
    if _wx_sudo systemctl reload nginx >/dev/null 2>&1; then return 0; fi
    _wx_sudo nginx -s reload >/dev/null 2>&1
}

# --- the add-on ----------------------------------------------------------------------------------

setup_webxdc_sandbox() {
    print_banner 2>/dev/null || true
    _wx_say "${BOLD:-}🎮 Setting up the webxdc mini-app sandbox origin${NC:-}"
    echo ""

    local repo_root example
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    example="$repo_root/nginx/webxdc-sandbox.conf.example"
    if [ ! -f "$example" ]; then
        _wx_err "nginx/webxdc-sandbox.conf.example not found — is this the PosterChanAI repo?"
        return 1
    fi

    if ! command -v nginx >/dev/null 2>&1; then
        _wx_err "nginx is not installed on this host."
        echo "  This add-on installs an nginx vhost. If you terminate TLS somewhere else (Caddy,"
        echo "  Traefik, nginx-proxy, a cloud load balancer, the Docker \`proxy\` service), you do not"
        echo "  need it — you need ONE rule there instead. docs/WEBXDC.md → \"Deploying this on your"
        echo "  own node\" has the one-liner for each."
        return 1
    fi

    # --- 1. the hostname ------------------------------------------------------------------------
    local domain guess
    domain="${WEBXDC_DOMAIN:-}"
    if [ -z "$domain" ]; then
        guess="$(_wx_guess_domain || true)"
        if [ -t 0 ]; then
            read -r -p "Instance hostname (the domain people load the client from)${guess:+ [$guess]}: " domain
            domain="${domain:-$guess}"
        else
            # A GUESS IS NOT AN ANSWER WITH NOBODY THERE TO CORRECT IT. This box has several vhosts
            # proxying to :3051 (ai., news., …) and the client is served from only one of them; a
            # confidently-wrong pick installs a vhost for a hostname nobody visits, which looks
            # exactly like not installing one. So say what it would have chosen, and stop.
            _wx_err "No tty, and no WEBXDC_DOMAIN. Nothing has been changed."
            echo "  Re-run naming the hostname people load the client from, e.g.:"
            echo "      WEBXDC_DOMAIN=${guess:-your-domain.com} ./install.sh --webxdc"
            return 1
        fi
    fi
    domain="$(echo "$domain" | tr -d '[:space:]' | sed -E 's#^https?://##; s#/.*$##' | tr 'A-Z' 'a-z')"
    if [ -z "$domain" ] || ! echo "$domain" | grep -q '\.'; then
        _wx_err "No instance hostname. Re-run with:  WEBXDC_DOMAIN=your-domain.com ./install.sh --webxdc"
        return 1
    fi
    case "$domain" in
        "${WEBXDC_LABEL}."*)
            _wx_err "That is already the sandbox hostname. Give the INSTANCE hostname (${domain#*.})."
            return 1;;
    esac

    local host="${WEBXDC_LABEL}.${domain}"
    local upstream="${WEBXDC_UPSTREAM:-127.0.0.1:3051}"
    _wx_ok "Instance: https://${domain}   →   sandbox origin: https://${host}   (app at ${upstream})"

    # --- 2. DNS, or refuse ----------------------------------------------------------------------
    # Refusing here rather than installing something broken is the point of the step. A vhost for a
    # name that does not resolve is exactly as invisible as no vhost at all, and the operator would
    # have no way to tell which of the two they were looking at.
    if [ "${WEBXDC_SKIP_DNS:-0}" != "1" ] && ! _wx_resolves "$host"; then
        echo ""
        _wx_err "${host} does not resolve yet. Nothing has been changed."
        echo ""
        echo "  Add this at your DNS provider, then re-run  ./install.sh --webxdc :"
        echo ""
        echo "      Type:   CNAME        (or an A/AAAA record with the same address as ${domain})"
        echo "      Name:   ${WEBXDC_LABEL}           (i.e. ${host})"
        echo "      Value:  ${domain}"
        echo ""
        echo "  Behind Cloudflare, proxied (orange cloud) is fine — this is ordinary HTTPS on 443."
        echo "  DNS can take a few minutes to propagate. If you use split-horizon DNS and know the"
        echo "  record is right, re-run with  WEBXDC_SKIP_DNS=1 ./install.sh --webxdc"
        return 1
    fi
    if [ "${WEBXDC_SKIP_DNS:-0}" != "1" ]; then
        local a_host a_dom
        a_host="$(_wx_addrs "$host")"; a_dom="$(_wx_addrs "$domain")"
        _wx_ok "DNS: ${host} → ${a_host:-(resolved)}"
        if [ -n "$a_host" ] && [ -n "$a_dom" ] && [ "$a_host" != "$a_dom" ]; then
            _wx_warn "${host} and ${domain} resolve to different addresses (${a_host} vs ${a_dom})."
            echo "    Fine behind a CDN or a second front end; wrong if you meant them to be one server."
        fi
    fi

    # --- 3. HTTP-only vhost, so ACME can be answered --------------------------------------------
    local conf_dir dest state
    conf_dir="$(_wx_conf_dir)"
    dest="$conf_dir/webxdc-sandbox.conf"
    if [ ! -d "$conf_dir" ]; then
        _wx_err "$conf_dir does not exist — set WEBXDC_NGINX_DIR to wherever this box keeps vhosts."
        return 1
    fi

    local certdir
    certdir="$(_wx_cert_dir "$host" || true)"
    if [ -z "$certdir" ]; then certdir="$(_wx_cert_dir_in_use "$dest" || true)"; fi

    if [ -z "$certdir" ]; then
        _wx_say "Installing a temporary HTTP-only vhost so the ACME challenge can be answered…"
        state="$(_wx_install_file "$dest" "$(_wx_http_only_conf "$host" "$upstream")")"
        _wx_enable_site "$conf_dir" "$dest"
        if ! _wx_nginx_test; then
            _wx_err "nginx -t failed with the temporary vhost in place. Rolling back."
            _wx_rollback "$dest"
            return 1
        fi
        # An unchanged file is the common case on a re-run, and a reload that fails is worth saying
        # out loud rather than swallowing into an `&&` — hence `if`, not `[ … ] && …`.
        if [ "$state" = changed ]; then _wx_nginx_reload || _wx_warn "could not reload nginx"; fi
        _wx_ok "http://${host}/ is served by this nginx"
    fi

    # --- 4. the certificate ----------------------------------------------------------------------
    if [ -z "$certdir" ] && [ "${WEBXDC_SKIP_CERTBOT:-0}" != "1" ]; then
        if ! command -v certbot >/dev/null 2>&1; then
            _wx_warn "certbot is not installed — skipping the certificate step."
            echo "    Install certbot + its nginx plugin, then re-run ./install.sh --webxdc"
            echo "    (Debian/Ubuntu: sudo apt-get install -y certbot python3-certbot-nginx)"
        else
            # Interactive only unless asked for: certbot without a tty needs --agree-tos and an email
            # it has no way to obtain, so running it blind produces a failure that reads like a bug
            # in this installer. Say the command instead.
            local ans="${WEBXDC_CERTBOT:-n}"
            if [ -t 0 ]; then
                echo ""
                echo "  Get a Let's Encrypt certificate for ${host} now?"
                echo "  This runs:  sudo certbot --nginx -d ${host}"
                echo "  It issues a certificate of its OWN and registers its renewal, exactly like every"
                echo "  other subdomain here. It does NOT touch ${domain}'s certificate (--expand would,"
                echo "  and a failure there would take the whole instance off TLS for a game feature)."
                read -r -p "  Run certbot? [Y/n]: " ans
                ans="${ans:-y}"
            fi
            case "$ans" in
                [Yy1]*) _wx_sudo certbot --nginx -d "$host" || _wx_warn "certbot did not complete";;
                *)      _wx_warn "No certificate yet. Run:  sudo certbot --nginx -d ${host}"
                        echo "    then re-run ./install.sh --webxdc  (or WEBXDC_CERTBOT=1 to do it here)";;
            esac
            certdir="$(_wx_cert_dir "$host" || true)"
        fi
    fi

    if [ -z "$certdir" ] && [ "${WEBXDC_DRY_RUN:-0}" = "1" ]; then
        certdir="$host"     # dry run: render the config certbot would have made possible
        _wx_say "    [dry-run] assuming a certificate at ${WEBXDC_LE_DIR:-/etc/letsencrypt}/live/${host}/"
    fi

    if [ -z "$certdir" ]; then
        echo ""
        _wx_warn "No certificate for ${host} yet, so the HTTP-only vhost stays in place."
        echo "    Mini apps CANNOT run over http:// — a service worker needs a secure context, so the"
        echo "    app window will stay blank. Get a certificate (any way you like), then re-run"
        echo "    ./install.sh --webxdc to install the real vhost. Details: docs/WEBXDC.md"
        return 1
    fi

    # --- 5. the real vhost -----------------------------------------------------------------------
    _wx_say "Installing the TLS vhost (from nginx/webxdc-sandbox.conf.example)…"
    if [ "${WEBXDC_DRY_RUN:-0}" = "1" ]; then
        echo "    [dry-run] would write $dest :"
        _wx_render_conf "$example" "$domain" "$upstream" "$certdir" | sed 's/^/    | /'
    fi
    state="$(_wx_install_file "$dest" "$(_wx_render_conf "$example" "$domain" "$upstream" "$certdir")")"
    _wx_enable_site "$conf_dir" "$dest"
    if ! _wx_nginx_test; then
        _wx_err "nginx -t failed. Rolling back to the previous config — nothing is broken."
        _wx_rollback "$dest"
        _wx_nginx_test || _wx_err "nginx -t STILL fails; run 'sudo nginx -t' and read it."
        return 1
    fi
    if [ "$state" = changed ]; then
        if _wx_nginx_reload; then _wx_ok "nginx reloaded"; else _wx_warn "reload nginx yourself: sudo systemctl reload nginx"; fi
    else
        _wx_ok "$dest already current"
    fi

    # --- 6. does it actually answer? --------------------------------------------------------------
    if [ "${WEBXDC_DRY_RUN:-0}" != "1" ] && command -v curl >/dev/null 2>&1; then
        local code
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://${host}/__sandbox__/" 2>/dev/null || echo 000)"
        case "$code" in
            200) _wx_ok "https://${host}/__sandbox__/ → 200. Mini apps will run.";;
            000) _wx_warn "Could not reach https://${host}/__sandbox__/ from this host (DNS/firewall/CDN?).";;
            404) _wx_warn "https://${host}/__sandbox__/ → 404. The request is not reaching the app on ${upstream}"
                 echo "    — check WEBXDC_UPSTREAM, and that nginx is passing the Host header through.";;
            *)   _wx_warn "https://${host}/__sandbox__/ → ${code} (expected 200).";;
        esac
    fi

    cat <<EOF

Done. Mini apps now run on https://${host}, which is a different ORIGIN from https://${domain} —
that separation is the whole security model: an app is untrusted code and must not share the origin
holding the reader's Nostr key and session.

  • Post one: compose → 📎 → "🎮 Mini app (.xdc)". Apps: https://webxdc.org/apps/
  • Nothing to enable in Admin; nothing to restart. The client derives the sandbox origin itself.
  • Renewal: certbot registered ${host} as its own lineage; \`certbot renew\` covers it.
  • Full picture, including what is deliberately NOT done (wildcard-per-app, a port): docs/WEBXDC.md
EOF
    if [ "${WEBXDC_DRY_RUN:-0}" = "1" ]; then
        _wx_warn "DRY RUN — nothing above was actually done. Re-run without WEBXDC_DRY_RUN."
    fi
}

# Debian's sites-available needs the symlink; conf.d does not. Separate so both phases share it.
_wx_enable_site() {
    local conf_dir="$1" dest="$2"
    case "$conf_dir" in
        */sites-available)
            local link="/etc/nginx/sites-enabled/$(basename "$dest")"
            [ -L "$link" ] || _wx_sudo ln -sf "$dest" "$link"
            ;;
    esac
}

# Put back the .bak if there is one; otherwise remove what we just wrote (and its symlink), which is
# the correct undo for a vhost that did not exist before this run.
_wx_rollback() {
    local dest="$1"
    if [ -f "$dest.bak" ]; then
        _wx_sudo mv "$dest.bak" "$dest"
    else
        _wx_sudo rm -f "$dest"
        _wx_sudo rm -f "/etc/nginx/sites-enabled/$(basename "$dest")"
    fi
}
