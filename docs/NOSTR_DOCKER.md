# Run a Nostr-only instance with Docker

A self-hosted **Nostr relay + web client + Blossom media server**, with no AI stack — a ~2 GB image
instead of ~70 GB, on hardware that costs a few dollars a month. This page is the whole setup, in
order, from an empty VPS to an instance other people can use.

Everything here assumes the `nostr` profile. If you want the AI features too, use
[DOCKER.md](DOCKER.md) instead — the steps below still apply, you just pick a different profile.

**What you end up with:**

| | |
|---|---|
| Web client | `https://your-domain/client` |
| Relay | `wss://your-domain/relay` |
| NIP-05 identities | `you@your-domain` |
| Blossom media | `https://your-domain/blossom` |

---

## Before you start

- **A server.** 2 GB RAM and ~20 GB disk is a reasonable floor; the relay's storage grows with the
  events you keep (`nostr_relay_retention_days`, default 30). Any Linux distro Docker runs on.
- **A domain name**, with an `A` record (and `AAAA` if you have IPv6) pointing at the server's public
  IP. You can start without one and add it later, but you'll be clicking through a browser warning
  until you do.
- **Ports 80 and 443 open** to the internet. That's all — a Nostr-only node needs nothing else
  inbound.

---

## Step 1 — Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

That gets you the engine plus the `docker compose` plugin, which this project needs (the old
`docker-compose` script is not enough). Check it:

```bash
docker compose version
```

---

## Step 2 — Get the code

```bash
git clone https://github.com/loblawbob873-svg/posterchanai.git
cd posterchanai
```

---

## Step 3 — Write your `.env`

Create a file called `.env` next to `docker-compose.yml`:

```bash
cat > .env <<EOF
# Which parts of the stack to run: nostr = app + relay + Blossom, tls = the HTTPS proxy.
# Both are needed. Setting them here lets you drop the --profile flags from day-to-day
# commands (\`docker compose logs -f\`, \`docker compose ps\`); the steps that set the stack
# up spell them out anyway, so a typo in this line can't leave you with a half-started node.
COMPOSE_PROFILES=nostr,tls

# Database password. Change it — this is the only place you set it.
POSTGRES_PASSWORD=$(openssl rand -hex 24)

# Your domain. Used for the proxy's server_name and its self-signed certificate.
POSTERCHANAI_DOMAIN=your-domain.com

# Keep the app and relay ports on loopback: the proxy reaches them from inside Docker,
# and nobody should reach them over plain HTTP from outside.
POSTERCHANAI_PORT=127.0.0.1:3051
POSTERCHANAI_NOSTR_RELAY_PORT=127.0.0.1:3052
EOF
```

---

## Step 4 — Start it

```bash
docker compose --profile nostr --profile tls up -d --build
```

Both profiles are required and do different jobs: **`nostr`** is the app + relay + Blossom (the
Nostr-only image, no AI), and **`tls`** is the nginx+certbot proxy that puts HTTPS in front of it.

The profiles are spelled out here on purpose. `COMPOSE_PROFILES` in your `.env` means you can drop
the flags from every later command — but if that line is missing or misspelled, a bare
`docker compose up -d` silently starts **only the `postgres` service** (everything else is behind a
profile) and you get no app, no relay, and no obvious error. Passing them explicitly once removes
that failure mode; after this, `docker compose ps` should list `postgres`, `nostr` and `proxy`.

First run builds the image and starts all three. Watch it come up with `docker compose logs -f`.

Open **`https://your-domain.com/client`**. Your browser will warn about the certificate — it's
self-signed until Step 6. Click through; the connection is encrypted, it just isn't vouched for by
anyone yet.

> **Why not plain HTTP?** Browsers only expose `crypto.subtle` — which is every key operation this
> client performs — on a secure origin. Over `http://` on a public address, the client cannot sign
> an event at all. `http://localhost` is the one exception, which is why a laptop test never shows
> this.

---

## Step 5 — Claim admin, before anyone else does

**Do this now, not later.** There is no default admin account and no password. The **first Nostr key
to sign in becomes the admin** (`app/routers/auth.py`), and until you do that, the instance will
hand admin to whoever signs in first.

1. Open `https://your-domain.com/client`.
2. Click sign in and use your **NIP-07 browser extension** (Alby, nos2x…) or a **remote signer**
   (Amber via NIP-46). Pasting an `nsec` works but is the least safe option, and "Create a new
   identity" is there if you don't have a key yet — back up the `nsec` it shows you.
3. That key is now the admin, and gets Blossom upload rights and a seat in the relay's web-of-trust
   automatically.

Confirm you see the **Admin** entry in the menu. If someone beat you to it, the fastest fix is to
start over: `docker compose --profile nostr --profile tls down -v` wipes the volumes (including all
data) and you can claim it on the next boot.

---

## Step 6 — Get a real certificate

Your DNS must already point at this server, and ports 80 and 443 must be reachable.

```bash
docker compose --profile nostr --profile tls exec proxy certbot --nginx -d your-domain.com
docker compose --profile nostr --profile tls exec proxy nginx -s reload
```

certbot edits the proxy's config for you and the change persists on a volume. Reload the client —
the browser warning is gone.

**Renewal is yours to schedule.** Certificates last 90 days:

```bash
# crontab -e   (twice a day is the conventional cadence; it's a no-op until renewal is due)
# Profiles spelled out because cron runs unattended — a silent "no such service" here means the
# certificate quietly expires 90 days later.
0 3,15 * * * cd /path/to/posterchanai && docker compose --profile nostr --profile tls exec -T proxy certbot renew --quiet && docker compose --profile nostr --profile tls exec -T proxy nginx -s reload
```

---

## Step 7 — Make the relay yours

Everything here is **Admin → Relay** in the web UI.

**Replace the NIP-05 identities — do not skip this.** A fresh node ships with two identities baked
in (`verita84`, `posterchan`) that belong to the upstream project's keys. Left alone, your domain
answers `verita84@your-domain.com` with someone else's pubkey. Under **NIP-05 identity server**,
replace the **Names** list with your own, one `name pubkey-hex` pair per line:

```
alice 3bf0c63f...
```

Set **Relay name**, **Description**, **Admin pubkey** (your npub — it goes in the NIP-11 document so
people know who runs this) and **Contact**. These are what a client shows when someone adds your
relay, and the defaults are generic placeholders.

Worth a look while you're there:

| Setting | Default | Why you might change it |
|---|---|---|
| **WoT depth** | 1 (your follows) | 2 adds friends-of-friends — a much larger, more useful relay, and more storage |
| **Seed npubs** | 10 well-known accounts | These bootstrap the trust graph. Your admin npub was added automatically |
| **Retention days** | 30 | How long events are kept |
| **Upstream relays** | 17 public relays | Where this node syncs from and broadcasts to |

The relay is a **web-of-trust** relay: it accepts events from accounts within your trust graph
rather than from the whole internet. That's what keeps a self-hosted relay's storage finite.

---

## Step 8 — Let people upload media

Blossom is on by default, but uploading is a privilege, not a default. For each user, either:

- **Admin → Users** → grant the 🌸 Blossom privilege, or
- **Admin → Blossom** → add their npub to the **upload whitelist**.

Also in **Admin → Blossom**: `Max upload MB` (default 100) and a per-user quota (default unlimited).
On a public node, set the quota — blobs are kept forever unless you set a blob TTL.

> If you set a `client_max_body_size` in the proxy config, keep it at or above `Max upload MB`, or
> uploads fail at nginx with a 413 the client can't explain. The shipped config sets no limit.

---

## Step 9 — Tell people how to connect

- **Relay:** `wss://your-domain.com/relay` — paste into any Nostr client (Damus, Amethyst, nostrudel…).
  Opening that URL in a browser shows a page confirming it's live, and clients fetch the NIP-11
  document from the same address.
- **Web client:** `https://your-domain.com/client` — works on phones; installable as a PWA.
- **NIP-05:** whatever names you configured in Step 7, as `name@your-domain.com`.

---

## Step 10 — Back up the three things that matter

There is no backup command in the app, and these are not interchangeable.

**1. The operator key** — the single most important file. It identifies your instance, and the
encrypted settings/account backups the relay publishes to upstream relays can only be restored with
it. Losing it means you cannot rebuild this instance's identity, ever.

```bash
docker compose exec -T nostr cat /app/data/keys.json > keys.json.backup   # keep this SECRET
```

**2. The database** — every event the relay holds, plus all accounts and settings.

```bash
docker compose exec -T postgres pg_dump -U posterchan posterchan_relay | gzip > relay-$(date +%F).sql.gz
```

**3. Blossom blobs** — the media your users uploaded, at `/app/data/blossom` in the container (the
`pc-rag` volume). Nothing regenerates these.

---

## Updating

```bash
git pull
docker compose --profile nostr --profile tls up -d --build
```

Your data lives on volumes and survives. One exception: the proxy's nginx config is seeded on first
boot and never overwritten (that's what protects certbot's edits), so a newer shipped config does
not reach an existing install — see [DOCKER.md](DOCKER.md#production-https--tls) if you want it.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `docker compose ps` shows only `postgres` | The profiles weren't passed. Every other service is behind `nostr` or `tls` — re-run the Step 4 command with both `--profile` flags, or fix the `COMPOSE_PROFILES` line in `.env` |
| `no such service: proxy` / `nostr` | Same cause, for `exec`/`logs`/`down`. Add the two `--profile` flags to that command |
| Browser still warns about the certificate | Step 6 hasn't run, or ran against a domain that doesn't resolve to this box yet |
| `502 Bad Gateway` right after `up -d` | The backend is still starting. The proxy retries on its own — no restart needed |
| Timeline is empty | Almost always a wrong system clock (the relay's queries are time-windowed) or DNS. Check `date` on the host |
| Can't sign in / no signing prompt | You're on `http://`, not `https://` — the client can't reach `crypto.subtle` |
| `certbot` fails | Port 80 blocked, or DNS not pointing here yet. Both are required for the HTTP-01 challenge |
| Uploads rejected | The user lacks the Blossom privilege (Step 8) |
| Relay unreachable from other clients | Test `wss://your-domain.com/relay` in a browser first; if that works, the client is probably caching an old relay list |

Logs: `docker compose logs -f` for everything, `docker compose logs -f proxy` for TLS problems.

---

## Optional extras

None of these are on by default; all are per-instance choices.

- **[Git over Nostr](GIT.md)** — host repositories on your own instance (`POSTERCHANAI_GIT=1`).
- **[Live streaming](DOCKER.md)** and **voice/video calls** — these need extra published ports
  (RTMP, WHIP, TURN) that a Nostr-only node otherwise doesn't use.
- **[Tor](TOR.md)** — publish the instance as a `.onion` as well.
- **[Relay internals](RELAY.md)** — web-of-trust tuning, storage, sync.
- **[Blossom](BLOSSOM.md)** — storage backends, expiry, migrating from another Blossom server.
- **[nginx](NGINX.md)** — if you'd rather run your own proxy than use the `tls` profile.
