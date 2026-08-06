# Production deployment with nginx + TLS

PosterChan AI (the app on `:3051` and the built-in Nostr relay on `:3052`) speaks plain HTTP/WS
by design — put **nginx** in front to terminate TLS and serve it on `443` like a real product.
This applies to **both** a full (AI) deployment and a **Nostr-only** deployment.

> **In Docker you don't have to do any of this.** `docker compose --profile <backend> --profile tls
> up -d` brings up a proxy container (nginx + certbot) already configured for the stack, on a
> self-signed certificate you replace with `certbot --nginx` when you have a domain — see
> [DOCKER.md](DOCKER.md#production-https--tls). This page is for running nginx yourself, on the host
> or on bare metal.

One ready-to-edit template covers every deployment:
[`nginx/posterchanai.conf.example`](../nginx/posterchanai.conf.example). A Nostr-only node uses the
same paths as a full one — the relay, NIP-05, Blossom and the web client don't move — so the only
parts a Nostr-only install ignores are the `/v1` block (no AI) and the on-disk `/static` block
(nothing on the host to serve when the app is in a container). Its header covers the Docker
specifics: which upstream address to use with nginx on the host vs in a container, and which
published ports to close afterwards.

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
- **`X-Real-IP`** — the relay trusts this one for the client address (it prefers it over the
  client-forgeable `X-Forwarded-For`). Without it every connection is attributed to the proxy's own
  address, which in Docker means all of them collapse onto one bridge-gateway IP.

**If you edit a `location`, don't add a `proxy_set_header` to it.** All of the above are declared
once at the `server` level, *including* `Upgrade`/`Connection`, because `proxy_set_header` is an
nginx **array** directive: a location that sets one header inherits **none** of the outer ones. The
usual shape — common headers at the server level, `Upgrade`/`Connection` down in the WebSocket
locations — silently drops `Host`, `X-Real-IP` and `X-Forwarded-Proto` on exactly the locations that
need them most, and nothing errors; the relay just starts publishing the nginx *upstream name* as
its NIP-11 host. Declaring the WebSocket pair at the server level is safe for ordinary requests, as
the `map` yields `close` whenever a client didn't ask to upgrade.

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

- **Relay on its own hostname:** prefer `wss://relay.example.com/`? The template ends with that second
  `server { server_name relay.example.com; ... }` block, commented out and ready to uncomment — add the
  name to DNS and to the cert (`certbot --nginx -d example.com -d relay.example.com`). Note its
  `proxy_pass http://posterchanai_relay;` carries **no URI**: the relay takes the WebSocket upgrade on
  any path, and passing `/` straight through is what makes its welcome page and NIP-11 document
  advertise `wss://relay.example.com/` — rewriting to `/relay` hands visitors a URL that hostname
  doesn't serve. See [RELAY.md](RELAY.md).
- **Blossom media** is served by the app at `/blossom` (same origin), so it's already covered by the
  catch-all `location /`. For a dedicated media host, proxy that subdomain to `:3051` as well — see
  [BLOSSOM.md](BLOSSOM.md).
- **Docker:** the compose stack publishes `3051` and `3052` on the host, so an nginx **on that host**
  proxies to `127.0.0.1:3051/3052` unchanged. nginx **in a container** must use the compose service
  name instead (`nostr:3051`) — `127.0.0.1` there is nginx itself, and every request fails with
  `connect() failed (111: Connection refused)`.
  The template's header covers both, plus how to move the two HTTP ports onto loopback so
  `http://<public-ip>:3051` stops answering around your TLS — and which ports must **stay** public
  (TURN and the streaming ingest ports aren't HTTP and nginx never fronts them).
- **WAF / Cloudflare:** the chat + relay WebSockets use **token auth, not cookies** — don't filter
  `/ws/`, `/api/`, `/v1`, or `/relay` by User-Agent, or they'll 403.
- **Firewall:** once nginx fronts everything, you usually want only `80`/`443` open to the world and
  `3051`/`3052` bound to localhost.
