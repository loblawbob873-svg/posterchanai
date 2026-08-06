#!/bin/sh
# Two jobs, both first-boot only: put a config in place and mint a self-signed certificate.
# Everything after that belongs to the operator (and to certbot) — this script never overwrites
# either one, or it would undo `certbot --nginx` on the next restart.
set -e

DOMAIN="${POSTERCHANAI_DOMAIN:-example.com}"
CONF=/etc/nginx/conf.d/posterchanai.conf
CERTS=/etc/letsencrypt/selfsigned

# nginx:alpine ships its own default.conf, and a named volume mounted at conf.d gets seeded with it.
# It owns `server_name _` on port 80, so left in place it answers before our redirect does.
rm -f /etc/nginx/conf.d/default.conf

if [ ! -f "$CONF" ]; then
    # POSTERCHANAI_DOMAIN is a convenience for the FIRST boot only — after that the file is yours,
    # so changing the variable later does nothing. Edit the config (and re-run certbot) instead.
    sed "s/example\.com/${DOMAIN}/g" /usr/share/posterchanai/posterchanai.conf > "$CONF"
    echo "[proxy] seeded $CONF for ${DOMAIN}"
fi

if [ ! -f "$CERTS/fullchain.pem" ]; then
    mkdir -p "$CERTS"
    # 10 years: this cert is untrusted by definition, so a short life buys nothing and an expired
    # self-signed cert is a worse error message than an untrusted one. SANs cover the loopback
    # names too, so `curl -k https://localhost` from the host works for a smoke test.
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout "$CERTS/privkey.pem" -out "$CERTS/fullchain.pem" \
        -subj "/CN=${DOMAIN}" \
        -addext "subjectAltName=DNS:${DOMAIN},DNS:localhost,IP:127.0.0.1" 2>/dev/null
    chmod 600 "$CERTS/privkey.pem"
    echo "[proxy] minted a self-signed certificate for ${DOMAIN} in $CERTS"
    echo "[proxy] browsers WILL warn until you run: certbot --nginx -d <your-domain>"
fi

# Fail loudly and early on a broken config rather than dying inside nginx's own startup, where the
# error is easy to miss in a compose log with five other services scrolling past.
nginx -t

exec "$@"
