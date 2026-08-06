# Built-in Blossom media server

PosterChanAI ships a [Blossom](https://github.com/hzrd149/blossom) media server
(BUD-01/02/06): content-addressed blob storage authenticated with signed Nostr events.
It's served by the app itself at **`/blossom`** (no separate process/port) and is **off by
default**.

## What it does

| Method | Path | Purpose |
|--------|------|---------|
| `PUT`  | `/blossom/upload` | Upload a blob (auth required) |
| `HEAD` | `/blossom/upload` | BUD-06 upload pre-flight |
| `GET`/`HEAD` | `/blossom/<sha256>` | Fetch a blob (public, by hash) |
| `GET`  | `/blossom/list/<pubkey>` | List a pubkey's blobs |
| `DELETE` | `/blossom/<sha256>` | Delete a blob (owner/admin) |

Blobs are **public by hash** — anyone with the sha256 can fetch them. Only **uploading**
and **deleting** are gated.

## Access control

A pubkey may upload (and delete) if **any** of these hold — `is_pubkey_allowed()` in
`app/services/blossom_service.py`:

1. It belongs to a **web user with the 🌸 Blossom privilege**: they've linked a Nostr key (User
   Settings → Nostr — we store their `npub`) and an admin has ticked **🌸 Blossom** in
   **Admin → Users → Access** (`can_blossom`). Admins are always allowed.
2. It's in the **`blossom_whitelist` setting** — an npub/hex allowlist in **Admin → Blossom**, for
   granting upload rights without creating an account at all.
3. It's one of the **node's own operator or bot keys**, so the bots can post effect media.
4. It's a configured **DVM peer** npub. Cluster peers upload their image/music/video job results to
   the shared Blossom, so no per-node grant is needed.

Upload auth is the standard Blossom flow: `Authorization: Nostr <base64 kind-24242 event>`
with a `t upload` tag, a future `expiration` tag, and an `x <sha256>` tag matching the body.
The server verifies the BIP340 signature, then checks the signer's pubkey maps to a
privileged user.

## Storage backend

Set in **Admin → Blossom → Storage**:

* **`local`** (default) — blobs on this node's disk, default `data/blossom` (or
  `$POSTERCHANAI_BLOSSOM_PATH`; `/app/data/blossom` on the Docker volume). Right for a single node.
* **`proxy`** — blobs are stored on the shared PosterChanAI **storage server** (the one under
  Services → Storage), under the `_blossom` system user — for a multi-node setup with a central
  store. Falls back to **local** automatically if no storage server is configured.
  In Docker this is on the `pc-rag:/app/data` volume, so it persists.

Blob **metadata** (sha256 → owner, size, mime, timestamps, TTL, backend, path) always lives
in the local `blossom_blobs` table. Identical bytes uploaded by several users are stored once.

## Expiration

**Admin → Blossom → Expiration → Blob TTL (days)**. `0` = never expire. Otherwise each blob
is deleted (bytes **and** record) that many days after upload; a low-frequency background
thread sweeps every 10 minutes. Re-uploading a blob refreshes its window.

## Performance / scale

Signature verification and full-body hashing are CPU-bound, so they run **off the event
loop** (`asyncio.to_thread`) — concurrent uploads from many users never block the request
loop. `coincurve` (libsecp256k1, already in `requirements.txt`) makes each verify ~0.03 ms
instead of ~67 ms; keep it installed for high upload volume. The expiry sweep is one idle
daemon thread, not a per-request cost.

## Enabling

Blossom is **on by default** — it's core, so there's no enable toggle in the admin UI.

* **Admin UI:** Admin → Blossom. Set a **Public base URL** only if behind a reverse proxy that
  rewrites the host (e.g. `https://media.yourdomain/blossom`) — blank auto-derives from the request.
  Then grant users the 🌸 Blossom privilege (Admin → Users) or add their npub to the whitelist.
* A node can be pinned **off** by setting `blossom_enabled=false` (e.g. a keyless storage backend).
* **Docker:** on by default; `POSTERCHANAI_BLOSSOM=1` also seeds the blob path on the data volume.
* **Reverse proxy:** front `/blossom` with TLS. Example nginx:

  ```nginx
  location /blossom/ {
      proxy_pass http://127.0.0.1:3051;
      client_max_body_size 200m;   # >= blossom_max_upload_mb
      proxy_request_buffering off;
  }
  ```

## Migrating an existing blossom-server

To import blobs from an existing (hzrd149) blossom-server, run on a node that has both the
old blob files and the PosterChanAI DB:

```bash
python scripts/migrate_blossom.py --source-dir /path/to/blossom/data \
    --sqlite /path/to/blossom/data/sqlite.db
# or, without the old DB, give a fallback owner:
python scripts/migrate_blossom.py --source-dir /path/to/blobs --owner npub1...
```

Imported blobs are re-stored through the configured backend (local by default, or the storage
server if set to proxy), so they land wherever this node keeps its blobs. Add `--dry-run` to preview.
