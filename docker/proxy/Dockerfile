# PosterChanAI TLS proxy — nginx + certbot, ready to go on a self-signed cert.
#
# The point of this image is that HTTPS works the moment the stack comes up, with no domain, no
# DNS and no certificate authority involved: the entrypoint mints a self-signed cert on first boot
# and the shipped config uses it. That's enough to make the web client usable, because browsers
# only expose crypto.subtle, the microphone/camera and service workers on a secure origin — and a
# plain http:// VPS address is not one.
#
# Getting a REAL certificate is then a two-command job the operator does when they're ready, from
# inside this container. certbot and its nginx plugin are installed for exactly that:
#
#   docker compose exec proxy certbot --nginx -d your-domain.com
#   docker compose exec proxy nginx -s reload
#
# certbot rewrites the ssl_certificate lines in /etc/nginx/conf.d/posterchanai.conf in place, and
# that file lives on a volume, so the change survives restarts. Renewals are the operator's call
# too — `certbot renew` (add it to the host's cron/systemd timer, or re-run it by hand).
FROM nginx:1.29-alpine

# certbot-nginx is the plugin behind `certbot --nginx`; openssl mints the self-signed cert.
RUN apk add --no-cache certbot certbot-nginx openssl

# The config is a SEED, copied to /etc/nginx/conf.d on first boot only (see entrypoint.sh) —
# after that it belongs to the operator and to certbot, and this image never overwrites it.
COPY posterchanai.conf /usr/share/posterchanai/posterchanai.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80 443
ENTRYPOINT ["/entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
