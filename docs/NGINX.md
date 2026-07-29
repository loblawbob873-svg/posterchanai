# Production deployment with nginx + TLS

PosterChan AI (the app on `:3051` and the built-in Nostr relay on `:3052`) speaks plain HTTP/WS
by design — put **nginx** in front to terminate TLS and serve it on `443` like a real product.
This applies to **both** a full (AI) deployment and a **Nostr-only** deployment.

A ready-to-edit template lives at [`nginx/posterchanai.conf.example`](../nginx/posterchanai.conf.example).

## Quick start

```bash
# 1. DNS: point example.com (an A/AAAA record) at this server's public IP.

# 2. Copy + edit the template — replace every "example.com" with your domain.
sudo cp nginx/posterchanai.conf.example /etc/nginx/sites-available/posterchanai.conf
sudo $EDITOR /etc/nginx/sites-available/posterchanai.conf

# 3. Enable it and get a free Let's Encrypt cert.
sudo ln -s /etc/nginx/sites-available/posterchanai.conf /etc/nginx/sites-enabled/
sudo certbot --nginx -d example.com        # fills in / manages the ssl_certificate paths

# 4. Test + reload.
sudo nginx -t && sudo systemctl reload nginx
```

Then:

- **Web client:** `https://example.com/client`
- **Nostr relay:** `wss://example.com/relay` (the app's own client uses it automatically; add it
  to any Nostr client). Relay info page (NIP-11): `https://example.com/relay-info`.
- **OpenAI-compatible API** (full installs): `https://example.com/v1/chat/completions`.

## What the config handles for you

- **HTTP → HTTPS** redirect (with an ACME-challenge carve-out so certbot renewals keep working).
- **WebSockets** for chat (`/ws/`) and the relay (`/relay`) via the `Upgrade`/`Connection` map.
- **`X-Forwarded-Proto $scheme`** on every location — the app reads this to know it's behind
  HTTPS (so it emits the right CSP and secure cookies). Without it the page can mis-render over
  a bare-HTTP origin.
- **Large uploads / Blossom blobs / generated media:** `client_max_body_size 0` (set a cap if you
  want) and long (`3600s`) proxy timeouts for slow LLM/image/video generations.

## Git-over-Nostr (GRASP) smart-HTTP

If you enable the built-in git host (**Admin → Git → `git_server_enabled`**; see
[GIT_OVER_NOSTR.md](GIT_OVER_NOSTR.md)), it runs as its **own subprocess on `127.0.0.1:3053`** so all
pack work stays off the app's event loop. The template's `location ^~ /git/` block exposes it as
`https://example.com/git/<npub>/<id>.git`, with `proxy_buffering off` (clones stream) and
`client_max_body_size 0` (pushes can be large). The git host tolerates the `/git/` prefix, so no URI
rewrite is needed.

**Multi-node (host on one box, reach it from another).** `git_server_bind`, `git_server_port`, and
`git_server_proxy_url` are **per-node** settings (each node's own Admin → Git), exactly like the
relay's bind/port. Two roles:

- **Hosting node** (`git_server_proxy_url` empty): holds the repos + hooks + auth. Set
  `git_server_bind = 0.0.0.0` so peers can reach `:3053` over the LAN, and keep nginx's `/git/` block
  pointing at `127.0.0.1:3053`.
- **Proxy node** (`git_server_proxy_url = http://<host>:3053`): runs **no** local git host — its app
  thin-reverse-proxies smart-HTTP (info/refs, upload-pack, receive-pack) to the hosting node,
  forwarding the client's NIP-98 header untouched (no server-to-server key). Point that node's nginx
  `/git/` block at the **app** instead: `proxy_pass http://127.0.0.1:3051;`.

All authorization, storage, and the pre/post-receive hooks stay on the hosting node; the proxy is a
dumb pass-through, the same shape as the Blossom storage proxy.

## Notes

- **Relay on its own hostname:** prefer `wss://relay.example.com/`? Copy the `/relay` block into a
  second `server { server_name relay.example.com; ... }` with `proxy_pass http://127.0.0.1:3052/relay;`
  at `location /`. Get a cert for that name too. See [RELAY.md](RELAY.md).
- **Blossom media** is served by the app at `/blossom` (same origin), so it's already covered by the
  catch-all `location /`. For a dedicated media host, proxy that subdomain to `:3051` as well — see
  [BLOSSOM.md](BLOSSOM.md).
- **Docker:** the compose stack publishes `3051` and `3052` on the host, so the exact same nginx
  config works — just keep nginx on the host (or another container) pointing at `127.0.0.1:3051/3052`.
- **WAF / Cloudflare:** the chat + relay WebSockets use **token auth, not cookies** — don't filter
  `/ws/`, `/api/`, `/v1`, or `/relay` by User-Agent, or they'll 403.
- **Firewall:** once nginx fronts everything, you usually want only `80`/`443` open to the world and
  `3051`/`3052` bound to localhost.
